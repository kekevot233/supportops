"""Authorized full public-development-set run. No business DB or online Embedding."""
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

from .answering import DemoQuoteLLM
from .document_index import LocalDocumentIndex
from .errors import SupportOpsError
from .live_pilot import (PilotSpendGuard, _wire_body, build_isolated_sdk,
                         isolated_environment, load_pilot_credentials, worker_isolation_valid)
from .provider_adapters import CallLimits, ChatSDKConfig
from .provider_runtime import compatible_runtime
from .qa_cases import canonical_sha256, load_answer_dataset
from .qa_evaluation import evaluate_answers, resolve_answer_report
from .qa_runtime import Completion, EvaluationRuntime, fake_runtime
from .seed import load_seed_corpus
from .spend_ledger import SpendLedger

RUN_ID = "supportops-20261018-full32-total-cny5"
EXPECTED_CORPUS = "313784b1615fbe04088eeeac9440bee586593bf69253215a257250ad8f19dd47"
EXPECTED_DATASET = "0888087108668eeda4181a4795790a3200e7c4c12add56a7fd0755005da64585"
AVAILABLE_AFTER_LEGACY = Decimal("3.771192")


def paths(root):
    root = Path(root).resolve()
    output = root / "outputs" / "supportops"
    if output.resolve() != output:
        raise SupportOpsError("PREFLIGHT_INVALID")
    return output, output / "cumulative_chat_budget.sqlite3", output / (RUN_ID + ".started")


def prepare_regression():
    corpus = load_seed_corpus()
    dataset = load_answer_dataset(corpus)
    if corpus.sha256 != EXPECTED_CORPUS or canonical_sha256(dataset.model_dump(mode="json")) != EXPECTED_DATASET or len(dataset.cases) != 32:
        raise SupportOpsError("PREFLIGHT_INVALID")
    bodies = []
    class Recorder:
        def complete(self, messages, *, max_tokens, temperature=None):
            bodies.append(_wire_body(messages, max_tokens, temperature))
            return Completion(DemoQuoteLLM().invoke(messages, max_tokens=max_tokens))
    spec = fake_runtime().spec.model_copy(update={"temperature": 0.0})
    fake = evaluate_answers(corpus, dataset, runtime=EvaluationRuntime(spec, LocalDocumentIndex, Recorder))
    if len(bodies) != 32 or fake["summary"]["error_case_count"]:
        raise SupportOpsError("PREFLIGHT_INVALID")
    guard = PilotSpendGuard(bodies, max_requests=32, budget_cny=AVAILABLE_AFTER_LEGACY)
    return corpus, dataset, fake, guard


def execute_regression(root, *, authorized=False, sdk_factory=None):
    if authorized is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    if sdk_factory is None and not worker_isolation_valid():
        raise SupportOpsError("EVALUATION_SETUP_FAILED")
    corpus, dataset, fake, guard = prepare_regression()
    output, ledger_path, started = paths(root)
    ledger = SpendLedger(ledger_path)
    # Permanent marker + transactional claim: neither direct worker nor parent can reuse this run.
    try:
        with started.open("x", encoding="ascii") as handle:
            handle.write(RUN_ID)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise SupportOpsError("PILOT_ALREADY_RESERVED") from None
    ledger.claim(RUN_ID, guard.plan)
    def event_sink(event):
        ledger.event(RUN_ID, event)
    guard.event_sink = event_sink
    credentials = load_pilot_credentials(Path(root), authorized=True)
    runtime_spec = fake_runtime().spec.model_copy(update={"runtime_id": RUN_ID, "execution": "external",
        "model_id": "deepseek-flash", "model_revision": "alias-not-pinned", "temperature": 0.0})
    chat = ChatSDKConfig(model="deepseek-flash", token_parameter="max_tokens", json_mode=True, timeout_seconds=30)
    runtime = compatible_runtime(runtime_spec, chat,
        chat_sdk_factory=lambda: (sdk_factory or build_isolated_sdk)(credentials, guard),
        # Shared CallLimits requires a positive number, but no embedding factory
        # exists in this runtime: online Embedding is structurally unreachable.
        limits=CallLimits(max_chat_requests=32, max_embedding_requests=1,
                          max_input_chars=650000, max_reserved_output_tokens=32768))
    report = evaluate_answers(corpus, dataset, runtime=runtime, allow_external_calls=True)
    ledger.finish(RUN_ID)
    report.update(scope="authorized_full32_development_comparison", authorization_id=RUN_ID,
        external_embedding_calls=0, financial_guard={**guard.snapshot(), "pricing_checked_on": "2026-10-18"},
        cumulative_budget=ledger.snapshot(), fake_comparison={"summary": fake["summary"],
            "cases": [{"id": row["id"], "passed": row["passed"], "observed_status": row["observed_status"]} for row in fake["cases"]]},
        transport_policy={"automatic_retries": 0, "trust_env": False, "follow_redirects": False,
            "tls_verify": True, "credential_source": "isolated_explicit_dotenv_supportops", "thinking": "disabled"})
    rendered = json.dumps(report, ensure_ascii=True, allow_nan=False)
    return json.loads(rendered.replace(credentials.api_key, "[REDACTED]"))


def run_regression(root, output, *, authorized=False):
    if authorized is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    root = Path(root).resolve()
    target = resolve_answer_report(output, project_root=root)
    directory, ledger_path, started = paths(root)
    recovery = directory / (RUN_ID + ".worker-report.json")
    if target.exists() or recovery.exists() or started.exists():
        raise SupportOpsError("PILOT_ALREADY_RESERVED")
    _, _, _, guard = prepare_regression()  # No credentials/network.
    ledger = SpendLedger(ledger_path)
    ledger.reserve(RUN_ID, guard.plan)  # Atomic full-run reservation before spawning worker.
    command = [sys.executable, "-I", str(root / "scripts" / "supportops_live_regression_worker.py"), RUN_ID]
    try:
        result = subprocess.run(command, cwd=root, env=isolated_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1200,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if recovery.is_file() and recovery.stat().st_size <= 1_000_000:
            payload = json.loads(recovery.read_text(encoding="utf-8"))
        elif len(result.stdout) <= 1_000_000:
            payload = json.loads(result.stdout.decode("ascii"))
        else:
            raise ValueError()
        if result.returncode or payload.get("authorization_id") != RUN_ID:
            if payload.get("code") == "PILOT_CONFIG_INVALID":
                raise SupportOpsError("PILOT_CONFIG_INVALID")
            raise ValueError()
        attempts = payload.get("financial_guard", {}).get("sdk_create_attempts")
        if type(attempts) is not int or not 0 <= attempts <= 32:
            raise ValueError()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        return payload
    except SupportOpsError:
        raise
    except Exception:
        # Keep all reservations, started markers and per-call events; never automatically retry.
        raise SupportOpsError("PILOT_PROCESS_FAILED") from None
