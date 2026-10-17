"""区分已执行的检索 smoke 检查与尚未执行的应用验收。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hello_agents import DocumentKnowledgeBase
from .seed import DEFAULT_DATA_ROOT, SeedCorpus, read_small_text


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^RET-[0-9]{2}$")
    question: str = Field(min_length=1)
    expected_document_ids: list[str] = Field(min_length=1)
    expected_fragments: list[str] = Field(min_length=1)


class ApplicationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^APP-[0-9]{2}$")
    category: str = Field(min_length=1)
    question: str = Field(min_length=1)
    setup: str = Field(min_length=1)
    expected_behavior: list[str] = Field(min_length=1)
    status: Literal["pending_application"]


class AcceptanceDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dataset_id: str
    split: Literal["development_smoke"]
    retrieval_cases: list[RetrievalCase] = Field(min_length=1, max_length=1000)
    application_cases: list[ApplicationCase] = Field(min_length=1, max_length=1000)


def load_acceptance_dataset(corpus: SeedCorpus, path: Path | None = None) -> AcceptanceDataset:
    dataset = AcceptanceDataset.model_validate_json(
        read_small_text(path or DEFAULT_DATA_ROOT / "evaluation" / "cases.json")
    )
    case_ids = [case.id for case in [*dataset.retrieval_cases, *dataset.application_cases]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("验收用例 ID 不能重复。")
    documents = {document.id: document for document in corpus.documents}
    for case in dataset.retrieval_cases:
        if len(case.expected_document_ids) != len(set(case.expected_document_ids)):
            raise ValueError("期望来源不能重复。")
        if any(doc_id not in documents for doc_id in case.expected_document_ids):
            raise ValueError("期望来源必须存在于显式摄取清单。")
        gold_text = "\n".join(documents[doc_id].content for doc_id in case.expected_document_ids)
        if any(not text.strip() or text not in gold_text for text in case.expected_fragments):
            raise ValueError("期望事实片段必须能在标注来源中找到。")
    return dataset


def evaluate_retrieval(
    index: DocumentKnowledgeBase,
    corpus: SeedCorpus,
    dataset: AcceptanceDataset,
    *,
    top_k: int = 5,
) -> dict:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k 必须是 1 到 100 的整数。")
    if not dataset.retrieval_cases:
        raise ValueError("检索用例不能为空。")
    documents = {document.id: document for document in corpus.documents}
    cases = []
    citation_total = 0
    citation_valid = 0
    fragment_total = 0
    fragment_found = 0
    for case in dataset.retrieval_cases:
        result = index.query(case.question, limit=top_k)
        found_docs = {hit.chunk.document_id for hit in result.hits}
        gold_docs = set(case.expected_document_ids)
        recall = len(found_docs & gold_docs) / len(gold_docs)
        first_rank = next((rank for rank, hit in enumerate(result.hits, 1)
                           if hit.chunk.document_id in gold_docs), None)
        gold_evidence = "\n".join(hit.chunk.content for hit in result.hits
                                  if hit.chunk.document_id in gold_docs)
        missing_fragments = [text for text in case.expected_fragments if text not in gold_evidence]
        fragment_total += len(case.expected_fragments)
        fragment_found += len(case.expected_fragments) - len(missing_fragments)
        valid = 0
        for hit, citation in zip(result.hits, result.citations):
            source = documents.get(citation.document_id)
            if (source is not None and citation.document_id == hit.chunk.document_id
                and citation.chunk_id == hit.chunk.id and citation.source == source.source
                and citation.title == hit.chunk.title
                and citation.chunk_index == hit.chunk.chunk_index
                and citation.section == hit.chunk.section
                and citation.page_number == hit.chunk.page_number
                and citation.start_char == hit.chunk.start_char
                and citation.end_char == hit.chunk.end_char
                and source.content[citation.start_char:citation.end_char] == hit.chunk.content):
                valid += 1
        citation_count = max(len(result.hits), len(result.citations))
        citation_total += citation_count
        citation_valid += valid
        citations_ok = bool(citation_count) and valid == citation_count
        cases.append({
            "id": case.id, "question": case.question,
            "document_recall": recall,
            "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
            "retrieved_document_ids": [hit.chunk.document_id for hit in result.hits],
            "missing_fragments": missing_fragments,
            "citations_valid": citations_ok,
            "passed": recall == 1 and not missing_fragments and bool(result.hits)
                      and citations_ok,
        })
    count = len(cases)
    recall = sum(case["document_recall"] for case in cases) / count
    span_accuracy = citation_valid / citation_total if citation_total else 0.0
    dataset_json = json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return {
        "schema_version": 1,
        "scope": "retrieval_smoke_only",
        "application_ready": False,
        "model_calls": 0,
        "corpus_id": corpus.manifest.corpus_id,
        "corpus_sha256": corpus.sha256,
        "document_count": len(corpus.documents),
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": hashlib.sha256(dataset_json.encode("utf-8")).hexdigest(),
        "split": dataset.split,
        "pipeline": {"embedder": "HashingEmbedder", "dimension": index.embedder.dimension,
                     "vector_store": "InMemoryVectorStore", "reranker": "HybridReranker",
                     "vector_weight": index.reranker.vector_weight,
                     "chunk_size": index.chunker.chunk_size,
                     "chunk_overlap": index.chunker.chunk_overlap,
                     "top_k": top_k, "candidate_multiplier": index.candidate_multiplier},
        "retrieval_case_count": count,
        "passed_case_count": sum(case["passed"] for case in cases),
        "document_recall_at_k": round(recall, 4),
        "mrr_at_k": round(sum(case["reciprocal_rank"] for case in cases) / count, 4),
        "support_fragment_hit_rate": round(fragment_found / fragment_total, 4),
        "citation_span_accuracy": round(span_accuracy, 4),
        "retrieval_gate_passed": recall >= 0.9 and span_accuracy == 1.0,
        "cases": cases,
        "application_cases": [{"id": case.id, "category": case.category,
                               "status": case.status} for case in dataset.application_cases],
        "limitations": ["公开开发 smoke 集，未做独立盲测", "仅检查检索，不评分生成答案",
                        "未执行权限、拒答、文档应用生命周期或服务故障验收"],
    }


__all__ = ["AcceptanceDataset", "evaluate_retrieval", "load_acceptance_dataset"]
