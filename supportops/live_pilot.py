"""单次 DeepSeek 小样本试跑；凭据只在净化环境的子进程中读取，不接网页。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from .answering import DemoQuoteLLM
from .document_index import LocalDocumentIndex
from .errors import SupportOpsError
from .provider_adapters import ChatSDKConfig
from .provider_runtime import compatible_runtime
from .qa_cases import canonical_sha256, load_answer_dataset
from .qa_evaluation import evaluate_answers, resolve_answer_report
from .qa_preflight import PilotConfig, load_pilot_config, preflight_pilot, select_pilot_dataset
from .qa_runtime import Completion, EvaluationRuntime, ProviderUsage, fake_runtime
from .seed import load_seed_corpus

AUTHORIZATION_ID = "supportops-20261017-round2-total-cny5"
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# 2026-10-17 官方 Flash 高峰、未缓存输入价格；不依赖命中缓存或周末优惠。
INPUT_CNY_PER_MILLION = Decimal("2")
OUTPUT_CNY_PER_MILLION = Decimal("8")
# 新授权为累计 5 元；上一轮结果未知，先按整轮 1 元占用，剩余 4 元可预留。
TOTAL_BUDGET_CNY = Decimal("5")
PRIOR_UNKNOWN_RESERVE_CNY = Decimal("1")
BUDGET_CNY = TOTAL_BUDGET_CNY - PRIOR_UNKNOWN_RESERVE_CNY
MAX_REQUEST_BYTES = 20000
CASE_IDS = ["RET-02", "REF-01", "INJ-01"]


@dataclass(frozen=True)
class PilotCredentials:
    model: str
    base_url: str
    api_key: str = field(repr=False)


def load_pilot_credentials(project_root: Path, *, authorized=False) -> PilotCredentials:
    if authorized is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    root = project_root.resolve()
    target = root / ".env.supportops"
    try:
        if target.is_symlink() or target.resolve() != target or not target.is_file():
            raise ValueError()
        with target.open("rb") as handle:
            raw = handle.read(16385)
        if not 0 < len(raw) <= 16384:
            raise ValueError()
        values = {}
        for line in raw.decode("utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if not separator or name in values or name not in {"LLM_MODEL_ID", "LLM_BASE_URL", "LLM_API_KEY"}:
                raise ValueError()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[name] = value
        if (set(values) != {"LLM_MODEL_ID", "LLM_BASE_URL", "LLM_API_KEY"}
                or values["LLM_MODEL_ID"] != "deepseek-flash"
                or values["LLM_BASE_URL"] not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
                or not re.fullmatch(r"[A-Za-z0-9._-]{16,256}", values["LLM_API_KEY"])):
            raise ValueError()
        return PilotCredentials(values["LLM_MODEL_ID"], values["LLM_BASE_URL"], values["LLM_API_KEY"])
    except (ValueError, OSError):
        raise SupportOpsError("PILOT_CONFIG_INVALID") from None


def isolated_environment() -> dict:
    # 仅按名字读取 Windows 运行所需的非凭据变量；不遍历或复制当前环境。
    result = {"PYTHON_DOTENV_DISABLED": "1", "PYTHONIOENCODING": "utf-8"}
    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            result[name] = value
    return result


def worker_isolation_valid():
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHON_DOTENV_DISABLED", "PYTHONIOENCODING"}
    return bool(sys.flags.isolated) and not ({key.upper() for key in os.environ} - allowed)


def _wire_body(messages, max_tokens, temperature=None):
    body = {"model": "deepseek-flash", "messages": deepcopy(messages), "n": 1, "stream": False,
            "max_tokens": max_tokens, "response_format": {"type": "json_object"}, "reasoning_effort": "none"}
    if temperature is not None:
        body["temperature"] = temperature
    return body


def _reservation(body):
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    if not 1 <= size <= MAX_REQUEST_BYTES or type(body["max_tokens"]) is not int or not 1 <= body["max_tokens"] <= 1024:
        raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
    # 保守启发式，不冒充供应商 tokenizer 或财务硬上限；发送前与完整计划再复核。
    assumed_input_tokens = size * 4 + 4096
    reserve = (assumed_input_tokens * INPUT_CNY_PER_MILLION + body["max_tokens"] * OUTPUT_CNY_PER_MILLION) / 1_000_000
    return {"request_sha256": canonical_sha256(body), "request_utf8_bytes": size,
            "assumed_input_tokens": assumed_input_tokens, "reserved_output_tokens": body["max_tokens"],
            "reserved_cny": str(reserve)}


class PilotSpendGuard:
    """只允许本地干跑确定的消息序列；失败/未知用量后不发送剩余请求。"""

    def __init__(self, bodies, *, max_requests=3, budget_cny=BUDGET_CNY):
        if type(max_requests) is not int or not 1 <= max_requests <= 32 or not 1 <= len(bodies) <= max_requests:
            raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
        self.budget_cny = Decimal(budget_cny)
        if not self.budget_cny.is_finite() or not 0 < self.budget_cny <= BUDGET_CNY:
            raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
        self.plan = [_reservation(body) for body in bodies]
        self.reserved = sum((Decimal(row["reserved_cny"]) for row in self.plan), Decimal("0"))
        if self.reserved > self.budget_cny:
            raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
        self.records, self.stopped, self._lock = [], False, RLock()
        self.event_sink = None

    def _event(self, kind, slot):
        if self.event_sink is not None:
            self.event_sink({"event": kind, "request_number": slot + 1,
                             "record": deepcopy(self.records[slot])})

    def invoke(self, create, body):
        with self._lock:
            slot = len(self.records)
            if (self.stopped or slot >= len(self.plan)
                    or _reservation(body)["request_sha256"] != self.plan[slot]["request_sha256"]):
                raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
            row = {"state": "attempted", "http_status": None, "usage": None, "provider_model": None}
            self.records.append(row)
            try:
                # 先持久化再调用；中断窗口按“可能已发送”保守处理，不能自动补发。
                self._event("before_sdk_create", slot)
                response = create(**body)
                receipt = getattr(response, "usage", None)
                if isinstance(response, dict):
                    receipt = response.get("usage")
                def read(name):
                    return receipt.get(name) if isinstance(receipt, dict) else getattr(receipt, name, None)
                usage = ProviderUsage(input_tokens=read("prompt_tokens"), output_tokens=read("completion_tokens"), total_tokens=read("total_tokens"))
                row.update(state="received", http_status=200, usage=usage.model_dump())
                model = response.get("model") if isinstance(response, dict) else getattr(response, "model", None)
                if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model):
                    row["provider_model"] = model
                if (usage.input_tokens > self.plan[slot]["assumed_input_tokens"]
                        or usage.output_tokens > self.plan[slot]["reserved_output_tokens"]):
                    self.stopped = True
                self._event("response_usage_received", slot)
                return response
            except Exception as error:
                status = getattr(error, "status_code", None)
                row.update(state="failed_or_usage_unknown", http_status=status if type(status) is int and 100 <= status <= 599 else None)
                self.stopped = True
                try:
                    self._event("failure_or_usage_unknown", slot)
                except Exception:
                    pass
                # 不把 SDK 异常/响应/请求头写入日志、返回值或 traceback。
                raise SupportOpsError("MODEL_UNAVAILABLE") from None

    def snapshot(self):
        known = [row["usage"] for row in self.records if row["usage"] is not None]
        subtotal = sum((Decimal(row["input_tokens"]) * INPUT_CNY_PER_MILLION +
                        Decimal(row["output_tokens"]) * OUTPUT_CNY_PER_MILLION for row in known), Decimal("0")) / 1_000_000
        return {"authorized_budget_cny": str(TOTAL_BUDGET_CNY),
                "prior_unknown_cost_reserved_cny": str(PRIOR_UNKNOWN_RESERVE_CNY),
                "remaining_budget_for_this_round_cny": str(self.budget_cny),
                "pricing_checked_on": "2026-10-17", "pricing_url": PRICING_URL,
                "input_cny_per_million": str(INPUT_CNY_PER_MILLION), "output_cny_per_million": str(OUTPUT_CNY_PER_MILLION),
                "reservation_method": "four_times_request_utf8_bytes_plus_4096_template_tokens",
                "provider_tokenizer_verified": False, "provider_financial_hard_cap": False,
                "reserved_plan_cny": str(self.reserved), "plan": deepcopy(self.plan),
                "sdk_create_attempts": len(self.records), "calls": deepcopy(self.records),
                "receipt_priced_upper_estimate_cny": str(subtotal) if self.records and len(known) == len(self.records) else None,
                "known_receipt_priced_subtotal_cny": str(subtotal) if known else None,
                "provider_invoice_cost_cny": None, "stopped_after_failure_or_bound_violation": self.stopped}


class GuardedPilotSDK:
    """SDK root 的独占包装，统一非思考模式和发送前预算；只用于隔离 worker。"""

    def __init__(self, root, guard):
        self._root, self._guard = root, guard

    def with_options(self, *, max_retries, timeout):
        if max_retries != 0 or not 0 < timeout <= 40:
            raise SupportOpsError("INVALID_REQUEST")
        view = self._root.with_options(max_retries=0, timeout=timeout)
        def create(**kwargs):
            body = _wire_body(kwargs["messages"], kwargs["max_tokens"], kwargs.get("temperature"))
            if kwargs != {key: value for key, value in body.items() if key != "reasoning_effort"}:
                raise SupportOpsError("INVALID_REQUEST")
            return self._guard.invoke(view.chat.completions.create, body)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def close(self):
        self._root.close()


def build_isolated_sdk(credentials, guard):
    # 仅在净化环境的子进程中调用。此处不通过默认 OpenAI() 查找凭据。
    if not worker_isolation_valid():
        raise SupportOpsError("EVALUATION_SETUP_FAILED")
    import openai
    import httpx2
    class OfficialOnlyTransport(httpx2.BaseTransport):
        def __init__(self):
            self.inner = httpx2.HTTPTransport(trust_env=False, retries=0, verify=True)
        def handle_request(self, request):
            if (request.method != "POST" or request.url.scheme != "https" or request.url.host != "api.deepseek.com"
                    or request.url.port not in (None, 443) or request.url.query
                    or request.url.path not in {"/chat/completions", "/v1/chat/completions"}
                    or request.headers.get("Authorization") != "Bearer " + credentials.api_key):
                raise SupportOpsError("INVALID_REQUEST")
            return self.inner.handle_request(request)
        def close(self):
            self.inner.close()
    transport = httpx2.Client(transport=OfficialOnlyTransport(), trust_env=False, follow_redirects=False, timeout=30)
    try:
        root = openai.OpenAI(api_key=credentials.api_key, admin_api_key="", organization="", project="",
            webhook_secret="", base_url=credentials.base_url, http_client=transport, max_retries=0, timeout=30)
        return GuardedPilotSDK(root, guard)
    except Exception:
        transport.close()
        raise SupportOpsError("EVALUATION_SETUP_FAILED") from None


def prepare_pilot(project_root: Path):
    corpus = load_seed_corpus()
    dataset = load_answer_dataset(corpus)
    data = load_pilot_config(Path("configs/supportops/pilot.example.json"), project_root=project_root).model_dump()
    data.update(plan_id=AUTHORIZATION_ID,
                chat={"model": "deepseek-flash", "token_parameter": "max_tokens", "json_mode": True, "timeout_seconds": 30.0},
                chat_endpoint={"provider_id": "deepseek", "region": "not-attested", "base_url": "https://api.deepseek.com", "allowed_host": "api.deepseek.com"})
    data["runtime"].update(runtime_id=AUTHORIZATION_ID, model_id="deepseek-flash", model_revision="alias-not-pinned",
                           temperature=0.0, output_tokens=1024)
    if (data.get("embedding") is not None or data.get("embedding_endpoint") is not None
            or data["scope"]["case_ids"] != CASE_IDS):
        raise SupportOpsError("PREFLIGHT_INVALID")
    config = PilotConfig.model_validate(data)
    preflight = preflight_pilot(config, corpus, dataset)
    if not preflight["offline_checks_passed"]:
        raise SupportOpsError("PREFLIGHT_INVALID")
    selected = select_pilot_dataset(config, corpus, dataset)
    bodies = []
    class Recorder:
        def complete(self, messages, *, max_tokens, temperature=None):
            bodies.append(_wire_body(messages, max_tokens, temperature))
            return Completion(DemoQuoteLLM().invoke(messages, max_tokens=max_tokens))
    spec = fake_runtime().spec.model_copy(update={"temperature": 0.0})
    fake = evaluate_answers(corpus, selected, runtime=EvaluationRuntime(spec, LocalDocumentIndex, Recorder))
    if len(bodies) != 3 or fake["summary"]["error_case_count"]:
        raise SupportOpsError("PREFLIGHT_INVALID")
    return config, corpus, selected, preflight, fake, PilotSpendGuard(bodies)


def execute_worker(project_root: Path, *, authorized=False, sdk_factory=None, event_sink=None):
    if authorized is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    config, corpus, selected, preflight, fake, guard = prepare_pilot(project_root)
    guard.event_sink = event_sink
    credentials = load_pilot_credentials(project_root, authorized=True)
    factory = sdk_factory or build_isolated_sdk
    runtime = compatible_runtime(config.runtime, config.chat,
        chat_sdk_factory=lambda: factory(credentials, guard), limits=config.limits)
    report = evaluate_answers(corpus, selected, runtime=runtime, allow_external_calls=True)
    report.update(scope="authorized_three_case_live_pilot", authorization_id=AUTHORIZATION_ID,
                  external_embedding_calls=0, financial_guard=guard.snapshot(), preflight=preflight,
                  fake_comparison={"summary": fake["summary"], "cases": [{"id": row["id"], "passed": row["passed"],
                      "observed_status": row["observed_status"]} for row in fake["cases"]]},
                  transport_policy={"environment": "minimal_child_allowlist", "automatic_retries": 0,
                                    "trust_env": False, "follow_redirects": False, "tls_verify": True,
                                    "credential_source": "explicit_dotenv_supportops_only", "thinking": "disabled"})
    # 即使未来报表字段扩充，也不把密钥带出 worker。
    rendered = json.dumps(report, ensure_ascii=False, allow_nan=False)
    return json.loads(rendered.replace(credentials.api_key, "[REDACTED]"))


def run_live_pilot(project_root: Path, output: Path, *, authorized=False):
    if authorized is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    root = project_root.resolve()
    target = resolve_answer_report(output, project_root=root)
    output_root = (root / "outputs" / "supportops").resolve()
    if not output_root.is_relative_to(root) or target.exists():
        raise SupportOpsError("PREFLIGHT_INVALID")
    # 先走无凭据干跑；不满足时不会预留授权或启动子进程。
    prepare_pilot(root)
    output_root.mkdir(parents=True, exist_ok=True)
    grant = output_root / (AUTHORIZATION_ID + ".grant.json")
    try:
        with grant.open("x", encoding="utf-8") as handle:
            json.dump({"authorization_id": AUTHORIZATION_ID, "reserved_chat_attempts": 3,
                       "budget_cny": str(TOTAL_BUDGET_CNY), "prior_unknown_reserved_cny": str(PRIOR_UNKNOWN_RESERVE_CNY),
                       "state": "reserved_no_automatic_retry"}, handle)
    except FileExistsError:
        raise SupportOpsError("PILOT_ALREADY_RESERVED") from None
    command = [sys.executable, "-I", str(root / "scripts" / "supportops_live_pilot_worker.py"), AUTHORIZATION_ID]
    try:
        result = subprocess.run(command, cwd=root, env=isolated_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=150,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if len(result.stdout) > 1_000_000:
            raise ValueError()
        # worker 的独立 UTF-8 报告优先；标准输出不再是唯一结果来源。
        recovery = grant.with_suffix(".worker-report.json")
        if recovery.is_file() and recovery.stat().st_size <= 1_000_000:
            payload = json.loads(recovery.read_text(encoding="utf-8"))
        else:
            payload = json.loads(result.stdout.decode("utf-8"))
        if result.returncode != 0 or payload.get("authorization_id") != AUTHORIZATION_ID:
            if payload.get("code") == "PILOT_CONFIG_INVALID":
                raise SupportOpsError("PILOT_CONFIG_INVALID")
            raise ValueError()
        attempts = payload.get("financial_guard", {}).get("sdk_create_attempts")
        if type(attempts) is not int or not 0 <= attempts <= 3:
            raise ValueError()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        grant.write_text(json.dumps({"authorization_id": AUTHORIZATION_ID, "reserved_chat_attempts": 3,
            "budget_cny": str(TOTAL_BUDGET_CNY), "prior_unknown_reserved_cny": str(PRIOR_UNKNOWN_RESERVE_CNY),
            "state": "completed_no_reuse", "sdk_create_attempts": attempts,
            "report": str(target.relative_to(root))}, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    except SupportOpsError:
        raise
    except Exception:
        # timeout/中断/解析失败时保留预留文件，不退款额度或自动重发。
        raise SupportOpsError("PILOT_PROCESS_FAILED") from None


__all__ = ["run_live_pilot"]
