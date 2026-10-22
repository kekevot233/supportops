"""回答 smoke 标注：复用 P0 事实，但不把问题、gold 或判分规则送入索引。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .evaluation import load_acceptance_dataset
from .models import AppModel
from .seed import DEFAULT_DATA_ROOT, SeedCorpus, read_small_text


def canonical_sha256(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PoisonSpec(AppModel):
    document_id: str = Field(pattern=r"^kb-[a-z0-9-]{1,60}$")
    prefix: str = Field(min_length=1, max_length=4000)


class AnswerCase(AppModel):
    id: str = Field(pattern=r"^(RET|REF|INJ)-[0-9]{2}$")
    category: Literal["answerable", "refusal", "injection"]
    question: str = Field(min_length=1, max_length=2000)
    expected_status: Literal["answered", "insufficient_evidence"]
    expected_document_ids: list[str] = Field(max_length=10)
    expected_fragments: list[str] = Field(max_length=30)
    forbidden_output: list[str] = Field(default_factory=list, max_length=10)
    poison: PoisonSpec | None = None

    @model_validator(mode="after")
    def consistent_case(self):
        if self.category == "refusal":
            if self.expected_status != "insufficient_evidence" or self.expected_document_ids or self.expected_fragments or self.poison:
                raise ValueError("拒答案例不能伪造 gold 事实。")
        elif self.expected_status != "answered" or not self.expected_document_ids or not self.expected_fragments:
            raise ValueError("可回答案例必须有来源和事实。")
        if (self.category == "injection") != (self.poison is not None):
            raise ValueError("只有注入案例可包含污染资料。")
        if self.poison and (not self.forbidden_output or self.poison.document_id not in self.expected_document_ids):
            raise ValueError("注入案例必须标记目标来源与禁止输出。")
        for values in (self.expected_document_ids, self.expected_fragments, self.forbidden_output):
            if len(set(values)) != len(values) or any(not value.strip() or len(value) > 2000 for value in values):
                raise ValueError("标注不能空白、重复或过长。")
        return self


class AnswerDataset(AppModel):
    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$")
    split: Literal["development_smoke", "holdout"]
    cases: list[AnswerCase] = Field(min_length=1, max_length=100)


class DatasetFile(AppModel):
    schema_version: Literal[1]
    dataset_id: str
    split: Literal["development_smoke"]
    retrieval_case_ids: list[str] = Field(max_length=100)
    challenge_cases: list[AnswerCase] = Field(max_length=100)


def validate_answer_dataset(corpus: SeedCorpus, dataset: AnswerDataset) -> None:
    if not isinstance(dataset, AnswerDataset):
        raise ValueError("需要 AnswerDataset。")
    # 防止调用方用 model_copy(update=...) 或可变列表绕过字段/跨字段校验。
    AnswerDataset.model_validate(dataset.model_dump())
    ids = [case.id for case in dataset.cases]
    if len(set(ids)) != len(ids):
        raise ValueError("用例 ID 不能重复。")
    documents = {doc.id: doc for doc in corpus.documents}
    for case in dataset.cases:
        if any(source not in documents for source in case.expected_document_ids):
            raise ValueError("gold 必须来自清单资料。")
        gold = "\n".join(documents[source].content for source in case.expected_document_ids)
        if any(fragment not in gold for fragment in case.expected_fragments):
            raise ValueError("事实必须存在于未污染的标注来源中。")
        if case.poison:
            if any(marker not in case.poison.prefix for marker in case.forbidden_output):
                raise ValueError("禁止输出必须来自测试污染资料。")
            if any(marker in doc.content for marker in case.forbidden_output for doc in corpus.documents):
                raise ValueError("canary 不应出现在干净语料中。")


def load_answer_dataset(corpus: SeedCorpus, path: Path | None = None) -> AnswerDataset:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON 字段。")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("非法 JSON 数值。")
    source = DatasetFile.model_validate(json.loads(read_small_text(path or DEFAULT_DATA_ROOT / "evaluation" / "answers.json"),
        object_pairs_hook=unique, parse_constant=invalid))
    base = {case.id: case for case in load_acceptance_dataset(corpus).retrieval_cases}
    if len(set(source.retrieval_case_ids)) != len(source.retrieval_case_ids) or any(key not in base for key in source.retrieval_case_ids):
        raise ValueError("引用的 P0 用例不存在或重复。")
    cases = [AnswerCase(**base[key].model_dump(), category="answerable", expected_status="answered")
             for key in source.retrieval_case_ids]
    dataset = AnswerDataset(schema_version=1, dataset_id=source.dataset_id, split=source.split,
                            cases=cases + source.challenge_cases)
    validate_answer_dataset(corpus, dataset)
    return dataset


__all__ = ["AnswerCase", "AnswerDataset", "PoisonSpec", "load_answer_dataset", "validate_answer_dataset", "canonical_sha256"]
