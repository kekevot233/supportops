"""Versioned completeness development and explicitly opened, frozen holdout.

CLI callers use Fake only. Gold is passed to scoring, never to the model client.
Opening a holdout writes a durable receipt BEFORE evaluation, even on failure.
"""
import hashlib
import json
import os
from pathlib import Path

from .answering import (COMPLETENESS_PROMPT_PROFILE, DEFAULT_PROMPT_PROFILE, system_rules_for)
from .errors import SupportOpsError
from .qa_cases import AnswerDataset, canonical_sha256, load_answer_dataset, validate_answer_dataset
from .qa_evaluation import evaluate_answers
from .seed import DEFAULT_DATA_ROOT, PROJECT_ROOT, load_seed_corpus, read_small_text

QUALITY_ROOT = DEFAULT_DATA_ROOT / "quality"
HOLDOUT_FILES = (
    "holdout/corpus/manifest.json", "holdout/corpus/release.md", "holdout/corpus/access.md",
    "holdout/corpus/diagnostics.md", "holdout/cases.json",
)
ORIGINAL_CORPUS_SHA = "313784b1615fbe04088eeeac9440bee586593bf69253215a257250ad8f19dd47"
ORIGINAL_DATASET_SHA = "0888087108668eeda4181a4795790a3200e7c4c12add56a7fd0755005da64585"
FROZEN_MANIFEST_SHA = "12e698aa38a0150535a7b4ba684f88201289d7e4e09dfb0820fe5f9c414e9ceb"


def _read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Quality artifacts must not be symbolic links")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate quality artifact key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("Nonfinite quality artifact value")
    return json.loads(read_small_text(path), object_pairs_hook=unique, parse_constant=invalid)


def _file_digest(path):
    # Stable across Git CRLF/LF checkouts; not a hash of platform-specific bytes.
    return hashlib.sha256(read_small_text(path).encode("utf-8")).hexdigest()


def load_development(*, quality_root=QUALITY_ROOT):
    corpus = load_seed_corpus()
    if corpus.sha256 != ORIGINAL_CORPUS_SHA or canonical_sha256(load_answer_dataset(corpus).model_dump(mode="json")) != ORIGINAL_DATASET_SHA:
        raise ValueError("Original baseline changed; review instead of silently rebaselining")
    dataset = AnswerDataset.model_validate(_read_json(Path(quality_root) / "development.json"))
    if dataset.split != "development_smoke":
        raise ValueError("Expected separate development cases")
    validate_answer_dataset(corpus, dataset)
    return corpus, dataset


def inspect_frozen_holdout(*, quality_root=QUALITY_ROOT):
    root = Path(quality_root).resolve()
    record = _read_json(root / "holdout.freeze.json")
    if canonical_sha256(record) != FROZEN_MANIFEST_SHA or set(record.get("files", {})) != set(HOLDOUT_FILES):
        raise ValueError("Holdout freeze manifest changed")
    for relative in HOLDOUT_FILES:
        target = root / relative
        if not target.resolve().is_relative_to(root) or target.is_symlink() or _file_digest(target) != record["files"][relative]:
            raise ValueError("Frozen holdout data changed or escaped its directory")
    for profile in (DEFAULT_PROMPT_PROFILE, COMPLETENESS_PROMPT_PROFILE):
        if hashlib.sha256(system_rules_for(profile).encode("utf-8")).hexdigest() != record["prompt_profiles"][profile]:
            raise ValueError("Frozen prompt profile changed; create a new version")
    corpus = load_seed_corpus(root / "holdout/corpus", source_prefix="data/supportops/quality/holdout/corpus")
    dataset = AnswerDataset.model_validate(_read_json(root / "holdout/cases.json"))
    validate_answer_dataset(corpus, dataset)
    if (dataset.split != "holdout" or corpus.sha256 != record["corpus_sha256"]
            or canonical_sha256(dataset.model_dump(mode="json")) != record["dataset_sha256"]
            or len(dataset.cases) != record["case_count"]):
        raise ValueError("Holdout identity does not match its freeze")
    # Validating structure does not execute, tune or score the holdout.
    return corpus, dataset, record


def quality_plan():
    _, development = load_development()
    corpus, held, freeze = inspect_frozen_holdout()
    return {"scope": "offline_quality_preparation", "external_model_calls": 0,
            "original_baseline": {"passed": 30, "total": 32, "unchanged": True},
            "development_case_count": len(development.cases), "holdout_case_count": len(held.cases),
            "holdout_corpus_documents": len(corpus.documents), "holdout_corpus_sha256": corpus.sha256,
            "holdout_dataset_sha256": freeze["dataset_sha256"], "freeze_sha256": FROZEN_MANIFEST_SHA,
            "prompt_profiles": freeze["prompt_profiles"], "semantic_improvement_verified": False,
            "independent_human_blind_test": False, "holdout_evaluated_by_this_command": False,
            "notes": ["AI-assisted synthetic fixtures, not production data or independent human gold.",
                      "Development may be tuned; holdout must not be used for tuning.",
                      "First explicit evaluation writes a non-reusable opening receipt."]}


def evaluate_quality_suite(suite="development", *, prompt_profile=COMPLETENESS_PROMPT_PROFILE,
                           open_holdout=False, runtime=None, project_root=PROJECT_ROOT, quality_root=QUALITY_ROOT):
    system_rules_for(prompt_profile)
    if suite not in {"development", "holdout"}:
        raise SupportOpsError("INVALID_REQUEST")
    if suite == "holdout" and open_holdout is not True:
        raise SupportOpsError("HOLDOUT_OPEN_REQUIRED")
    if runtime is not None and runtime.spec.execution == "external":
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    corpus, dataset = load_development(quality_root=quality_root) if suite == "development" else (None, None)
    if suite == "holdout":
        corpus, dataset, _ = inspect_frozen_holdout(quality_root=quality_root)
        root = Path(project_root).resolve()
        folder = root / "outputs" / "supportops"
        if folder.resolve() != folder:
            raise ValueError("Holdout opening receipt directory escaped")
        folder.mkdir(parents=True, exist_ok=True)
        receipt = folder / ("quality-holdout-" + FROZEN_MANIFEST_SHA + ".opened.json")
        if receipt.is_symlink():
            raise ValueError("Invalid holdout receipt")
        try:
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump({"freeze_sha256": FROZEN_MANIFEST_SHA, "prompt_profile": prompt_profile,
                    "state": "opened_no_reuse_even_after_failure", "not_an_external_call_authorization": True}, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise SupportOpsError("HOLDOUT_ALREADY_OPENED") from None
    report = evaluate_answers(corpus, dataset, runtime=runtime, prompt_profile=prompt_profile)
    report.update(quality_suite=suite, semantic_improvement_verified=False, independent_human_blind_test=False,
                  holdout_opened_for_this_run=suite == "holdout",
                  annotation_provenance="AI-assisted synthetic/test author; not independent human adjudication")
    if suite == "holdout":
        report["holdout_freeze_sha256"] = FROZEN_MANIFEST_SHA
        report["holdout_policy"] = "opened; do not use results to tune and then call this an independent test"
    return report
