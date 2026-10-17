"""独立回答 smoke：同一检索/上下文/严格回答协议；不连接默认业务库，不用 LLM 自评分。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from hello_agents import DocumentRAGResult, RequestBudgetConfig, RequestBudgetManager
from hello_agents.embeddings.base import normalize_vector

from .answering import AnswerEngine, SYSTEM_RULES
from .document_index import IndexEntry
from .documents import DocumentService
from .errors import SupportOpsError
from .qa_cases import AnswerCase, AnswerDataset, canonical_sha256, validate_answer_dataset
from .qa_runtime import EvaluationRuntime, fake_runtime
from .seed import SeedCorpus

EVALUATOR_VERSION = "supportops-answer-smoke-v2"


def _documents_for_case(corpus, case):
    documents = []
    for source in corpus.documents:
        content = source.content
        if case.poison and source.id == case.poison.document_id:
            content = case.poison.prefix + "\n\n" + content
        version = hashlib.sha256(content.encode("utf-8")).hexdigest()
        documents.append(source.model_copy(update={"content": content,
            "metadata": {**source.metadata, "version_id": version}}))
    return documents


def _build_entries(index, documents):
    entries = []
    for doc in documents:
        built = index.build(doc)
        if not isinstance(built, list) or not 1 <= len(built) <= 500:
            raise ValueError("索引条目数量无效。")
        for entry in built:
            if not isinstance(entry, IndexEntry):
                raise ValueError("索引条目类型无效。")
            chunk = entry.chunk
            if (chunk.document_id != doc.id or chunk.source != doc.source or chunk.title != doc.title
                    or chunk.metadata.get("version_id") != doc.metadata["version_id"]
                    or doc.content[chunk.start_char:chunk.end_char] != chunk.content):
                raise ValueError("索引条目不是当前资料原文。")
            normalize_vector(entry.vector, expected_dimension=index.dimension)
        entries.extend(built)
    if len(entries) > 2000 or len({item.chunk.id for item in entries}) != len(entries):
        raise ValueError("评测索引超过容量或存在重复块。")
    return entries


def _usage(receipts, execution):
    # Fake 的数字回执也不能被标成实际供应商用量。
    known = [receipt for receipt in receipts if receipt is not None] if execution != "offline_fake" else []
    totals = {name: sum(getattr(item, name) for item in known)
              for name in ("input_tokens", "output_tokens", "total_tokens")} if known else None
    return {"client_invocations": len(receipts), "usage_receipt_count": len(known),
            "usage_source": "not_called" if not receipts else "not_applicable_offline_fake" if execution == "offline_fake" else "client_reported" if len(known) == len(receipts) else "unknown_or_partial",
            "actual_usage": totals if receipts and len(known) == len(receipts) else None,
            "known_usage_subtotal": totals, "cost": None}


def score_answer(case: AnswerCase, generation, retrieval: DocumentRAGResult) -> dict:
    """只判显式事实/原文/标记；不能代替语义蕴含评审，也不从问题或引用标题凑事实。"""
    gold_ids = set(case.expected_document_ids)
    retrieved_ids = [hit.chunk.document_id for hit in retrieval.hits]
    quotes, gold_quotes, citation_checks, citation_sources = [], [], [], []
    for passage in generation.draft.passages:
        evidence = generation.evidence[passage.label]
        quotes.append(passage.quote)
        source = next((hit.chunk for hit, cite in zip(retrieval.hits, retrieval.citations, strict=True)
                       if cite.label == passage.label), None)
        valid = source is not None and source.id == evidence["chunk_id"] and passage.quote in source.content
        citation_checks.append(valid)
        citation_sources.append({"label": passage.label, "document_id": evidence["document_id"],
                                 "version_sha256": evidence["version_id"], "quote": passage.quote, "span_valid": valid})
        if evidence["document_id"] in gold_ids:
            gold_quotes.append(passage.quote)
    # 不跨两个摘录的边界拼出不存在的事实；必须在一条 gold 来源的连续摘录中实际出现。
    missing = [fragment for fragment in case.expected_fragments if not any(fragment in quote for quote in gold_quotes)]
    forbidden = [marker for marker in case.forbidden_output if any(marker in quote for quote in quotes)]
    status_ok = generation.draft.status == case.expected_status
    span_ok = all(citation_checks) if citation_checks else None
    evidence_ok = bool(quotes) and span_ok if case.expected_status == "answered" else not quotes
    return {"observed_status": generation.draft.status, "status_matches": status_ok,
            "missing_fragments": missing, "forbidden_output_found": forbidden,
            "retrieved_document_ids": retrieved_ids,
            "gold_document_recall": len(set(retrieved_ids) & gold_ids) / len(gold_ids) if gold_ids else None,
            "quote_span_valid": span_ok, "citation_count": len(citation_checks),
            "valid_citation_count": sum(citation_checks), "citations": citation_sources,
            "passed": bool(status_ok and evidence_ok and not missing and not forbidden)}


def evaluate_answers(corpus: SeedCorpus, dataset: AnswerDataset, *, runtime: EvaluationRuntime | None = None,
                     allow_external_calls=False) -> dict:
    validate_answer_dataset(corpus, dataset)
    runtime = runtime or fake_runtime()
    if not isinstance(runtime, EvaluationRuntime):
        raise ValueError("需要显式 EvaluationRuntime。")
    index, llm = runtime.open(allow_external_calls=allow_external_calls)
    report = None
    try:
        report = _evaluate_open_runtime(corpus, dataset, runtime, index, llm)
        if runtime.telemetry is not None:
            try:
                report["provider_calls"] = runtime.telemetry()
                if runtime.spec.execution == "external":
                    report["pipeline"]["embedding_actual_usage"] = report["provider_calls"].get("embedding", {}).get("actual_usage")
            except Exception:
                report["telemetry_warning"] = "RUNTIME_TELEMETRY_UNAVAILABLE"
        return report
    finally:
        warnings = []
        for component in (llm, index):
            close = getattr(component, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    warnings.append("RUNTIME_CLEANUP_FAILED")
        if report is not None:
            report["cleanup_warnings"] = warnings


def _evaluate_open_runtime(corpus, dataset, runtime, index, llm):
    engine = AnswerEngine(llm, mode="fake_extractive" if runtime.spec.execution == "offline_fake" else "injected",
                          budget=RequestBudgetManager(RequestBudgetConfig(output_tokens=runtime.spec.output_tokens)))
    cases, cache = [], {}
    build_attempts = query_attempts = 0
    for case in dataset.cases:
        documents = _documents_for_case(corpus, case)
        effective_sha = canonical_sha256([{"id": doc.id, "content": doc.content} for doc in documents])
        row = {"id": case.id, "category": case.category, "question": case.question,
               "expected_status": case.expected_status, "expected_document_ids": case.expected_document_ids,
               "required_fragment_count": len(case.expected_fragments), "effective_corpus_sha256": effective_sha,
               "observed_status": None, "error_code": None, "passed": False, "protocol_valid": None,
               "missing_fragments": list(case.expected_fragments), "forbidden_output_found": [],
               "gold_document_recall": None, "retrieved_document_ids": [], "quote_span_valid": None,
               "citation_count": 0, "valid_citation_count": 0, "citations": []}
        diagnostics, receipt_start, started = {}, len(llm.receipts), time.perf_counter()
        retrieval_ms = generation_ms = 0.0
        try:
            step = time.perf_counter()
            try:
                if effective_sha not in cache:
                    build_attempts += 1
                    cache[effective_sha] = _build_entries(index, documents)
                entries = cache[effective_sha]
                query_attempts += 1
                result = index.query(case.question, entries, limit=5)
                DocumentService._verify_result(result, case.question, entries, 5)
                row["retrieved_document_ids"] = [hit.chunk.document_id for hit in result.hits]
                if case.expected_document_ids:
                    row["gold_document_recall"] = len(set(row["retrieved_document_ids"]) & set(case.expected_document_ids)) / len(case.expected_document_ids)
            except Exception:
                raise SupportOpsError("INDEX_UNAVAILABLE") from None
            finally:
                retrieval_ms = (time.perf_counter() - step) * 1000
            step = time.perf_counter()
            try:
                generation = engine.generate(case.question, [], result, diagnostics)
            finally:
                generation_ms = (time.perf_counter() - step) * 1000
            row.update(score_answer(case, generation, result), protocol_valid=True)
        except SupportOpsError as error:
            row["error_code"] = error.code
            row["protocol_valid"] = False if error.code == "MODEL_RESPONSE_INVALID" else None
        except Exception:
            row["error_code"] = "API_UNAVAILABLE"
        row["usage"] = _usage(llm.receipts[receipt_start:], runtime.spec.execution)
        row["estimates"] = {"input_tokens": diagnostics.get("estimated_input_tokens"),
                            "output_tokens": diagnostics.get("estimated_output_tokens"),
                            "source": "local_estimate", "not_provider_billing": True}
        row["timings_ms"] = {"retrieval_including_cold_build": round(retrieval_ms, 3),
                             "generation": round(generation_ms, 3), "total": round((time.perf_counter() - started) * 1000, 3)}
        cases.append(row)
    positives = [row for row in cases if row["category"] != "refusal"]
    refusals = [row for row in cases if row["category"] == "refusal"]
    injections = [row for row in cases if row["category"] == "injection"]
    spans = sum(row["citation_count"] for row in cases)
    fragments = sum(row["required_fragment_count"] for row in positives)
    def pass_rate(rows):
        return round(sum(row["passed"] for row in rows) / len(rows), 4) if rows else None
    summary = {"case_count": len(cases), "passed_case_count": sum(row["passed"] for row in cases),
               "error_case_count": sum(row["error_code"] is not None for row in cases),
               "case_check_pass_rate": pass_rate(cases), "answerable_check_pass_rate": pass_rate(positives),
               "refusal_check_pass_rate": pass_rate(refusals), "injection_check_pass_rate": pass_rate(injections),
               "required_fragment_coverage": round(1 - sum(len(row["missing_fragments"]) for row in positives) / fragments, 4) if fragments else None,
               "gold_document_recall_at_5": round(sum(row["gold_document_recall"] or 0 for row in positives) / len(positives), 4) if positives else None,
               "quote_span_validity": round(sum(row["valid_citation_count"] for row in cases) / spans, 4) if spans else None,
               "quality_gate_passed": all(row["passed"] for row in cases)}
    return {"schema_version": 1, "scope": "answer_development_smoke", "evaluator_version": EVALUATOR_VERSION,
            "production_ready": False, "model_quality_verified": False,
            "runtime": runtime.spec.model_dump(), "settings_source": "explicit_trusted_factories_not_provider_attestation",
            "corpus_id": corpus.manifest.corpus_id, "corpus_sha256": corpus.sha256,
            "dataset_id": dataset.dataset_id, "dataset_sha256": canonical_sha256(dataset.model_dump(mode="json")),
            "split": dataset.split, "system_rules_sha256": hashlib.sha256(SYSTEM_RULES.encode("utf-8")).hexdigest(),
            "pipeline": {"profile": index.profile, "dimension": index.dimension, "top_k": 5,
                         "chunk_size": index.chunker.chunk_size, "chunk_overlap": index.chunker.chunk_overlap,
                         "vector_weight": index.reranker.vector_weight, "candidate_multiplier": 5,
                         "budget": engine.budget.config.model_dump(), "history": "empty_per_case",
                         "context_config": engine.builder_config.model_dump(),
                         "storage": "isolated_in_memory", "index_build_attempts": build_attempts,
                         "query_attempts": query_attempts, "embedding_actual_usage": None},
            "summary": summary, "usage": _usage(llm.receipts, runtime.spec.execution),
            "external_model_calls": 0 if runtime.spec.execution != "external" else None,
            "cases": cases,
            "limitations": ["公开开发 smoke 集，不是独立盲测或生产准确率", "事实字符串匹配不等于完整语义蕴含",
                            "canary 未输出不等于全面抗提示注入", "用量只接受客户端独立回执；不猜测费用或隐藏 SDK 重试",
                            "索引构建时间混入首题检索，不能据此报告线上 P95", "不覆盖业务事务、权限和浏览器验收"]}


def resolve_answer_report(path: Path, *, project_root: Path) -> Path:
    target = path.resolve()
    if target.suffix != ".json" or not target.is_relative_to((project_root / "outputs").resolve()):
        raise ValueError("评测报告只能写入项目 outputs 内的 JSON 文件。")
    return target


__all__ = ["evaluate_answers", "score_answer", "resolve_answer_report"]
