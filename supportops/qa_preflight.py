"""P5c 第一部分：无凭据、无 SDK、无 DNS/网络的有限样本预检，不是运行授权。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from hello_agents import DocumentChunker

from .answering import SYSTEM_RULES
from .document_index import LocalDocumentIndex
from .errors import SupportOpsError
from .models import AppModel
from .provider_adapters import (
    CHAT_MAX_INPUT_CHARS, CallLimits, ChatSDKConfig, EmbeddingSDKConfig, plan_embedding_batches,
)
from .qa_cases import AnswerDataset, canonical_sha256, validate_answer_dataset
from .qa_evaluation import EVALUATOR_VERSION, _documents_for_case
from .qa_runtime import Identifier, RuntimeSpec
from .seed import SeedCorpus

PREFLIGHT_VERSION = "supportops-pilot-preflight-v1"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CaseId = Annotated[str, Field(pattern=r"^(RET|REF|INJ)-[0-9]{2}$")]


def _host(value: str) -> str:
    # 不解析 DNS；限定为规范 ASCII FQDN。公共外观的名称仍可能解析到私网。
    if (value != value.lower() or len(value) > 253 or not value.isascii()
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value)
            or value.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".arpa"))):
        raise ValueError("端点需要规范的公开域名，不支持本地地址。")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("端点不允许 IP 地址。")


class EndpointConfig(AppModel):
    provider_id: Identifier
    region: Identifier
    base_url: str = Field(min_length=1, max_length=400)
    allowed_host: str = Field(min_length=1, max_length=253)

    @field_validator("allowed_host")
    @classmethod
    def valid_host(cls, value):
        return _host(value)

    @model_validator(mode="after")
    def exact_https_endpoint(self):
        value = self.base_url
        parts = urlsplit(value)
        # URL 不含用户信息、查询、片段、转义、非 443 端口或任意附加请求头。
        if (not value.isascii() or parts.scheme != "https" or parts.netloc != self.allowed_host
                or value != f"https://{self.allowed_host}{parts.path}"
                or "?" in value or "#" in value or "\\" in value or "%" in value
                or not (parts.path in {"", "/"} or re.fullmatch(r"/(?:[a-z0-9_-]+/)*v1", parts.path))):
            raise ValueError("端点仅允许白名单 HTTPS 域名，以及根路径或显式 v1 路径。")
        return self


class PilotScope(AppModel):
    data_policy: Literal["public_project_seed_only"]
    corpus_sha256: Digest
    dataset_sha256: Digest
    case_ids: list[CaseId] = Field(min_length=1, max_length=3)

    @field_validator("case_ids")
    @classmethod
    def unique_cases(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("试跑样本不能重复。")
        return values


class PilotConfig(AppModel):
    schema_version: Literal[1]
    plan_id: Identifier
    runtime: RuntimeSpec
    chat: ChatSDKConfig
    chat_endpoint: EndpointConfig
    embedding: EmbeddingSDKConfig | None = None
    embedding_endpoint: EndpointConfig | None = None
    limits: CallLimits
    scope: PilotScope

    @model_validator(mode="after")
    def coherent_pilot(self):
        if self.runtime.execution != "external" or self.chat.model != self.runtime.model_id:
            raise ValueError("试跑必须显式声明 external，且模型身份一致。")
        if self.limits.max_chat_requests > 3:
            raise ValueError("首轮试跑最多允许三次 Chat 尝试。")
        if self.embedding is None:
            if self.embedding_endpoint is not None or (self.runtime.embedding_id, self.runtime.embedding_revision) != (
                    LocalDocumentIndex.embedding_id, LocalDocumentIndex.embedding_revision):
                raise ValueError("未配置在线编码器时只能使用 Hashing 基线。")
        elif self.embedding_endpoint is None or self.embedding.model != self.runtime.embedding_id:
            raise ValueError("在线 Embedding 必须有独立端点并与模型身份一致。")
        return self


def load_pilot_config(path: Path, *, project_root: Path) -> PilotConfig:
    """只读取显式 configs/supportops/*.json；不按配置引用再读取密钥或其他文件。"""
    root = project_root.resolve()
    target = (root / path).resolve()
    if (target.suffix != ".json" or not target.is_relative_to(root / "configs" / "supportops")
            or not target.is_file() or target.stat().st_size > 65536):
        raise ValueError("试跑配置必须是 configs/supportops 下 64 KiB 以内的 JSON 文件。")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("配置存在重复字段。")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("非法 JSON 数值。")

    # 有界读取也覆盖 stat 后文件增长，不读取任意大小配置。
    with target.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("配置过大。")
    return PilotConfig.model_validate(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                                               parse_constant=invalid))


def select_pilot_dataset(config: PilotConfig, corpus: SeedCorpus, dataset: AnswerDataset) -> AnswerDataset:
    """重校验可变列表/复制模型，摘要绑定全题集，返回按显式顺序选择的深拷贝。"""
    config = PilotConfig.model_validate(config.model_dump())
    validate_answer_dataset(corpus, dataset)
    actual_corpus = canonical_sha256({"manifest": corpus.manifest.model_dump(mode="json"),
        "documents": [{"id": doc.id, "content": doc.content} for doc in corpus.documents]})
    if (actual_corpus != corpus.sha256 or actual_corpus != config.scope.corpus_sha256
            or canonical_sha256(dataset.model_dump(mode="json")) != config.scope.dataset_sha256):
        raise ValueError("资料或题集已改变，需重新审核试跑范围。")
    by_id = {case.id: case for case in dataset.cases}
    if any(key not in by_id for key in config.scope.case_ids):
        raise ValueError("试跑样本不在已绑定题集中。")
    return AnswerDataset(schema_version=dataset.schema_version, dataset_id=dataset.dataset_id, split=dataset.split,
                         cases=[by_id[key].model_copy(deep=True) for key in config.scope.case_ids])


def preflight_pilot(config: PilotConfig, corpus: SeedCorpus, dataset: AnswerDataset) -> dict:
    """只模拟切块和分批；不构造向量/SDK/运行时，不调用模型，也不读取环境或 .env。"""
    config = PilotConfig.model_validate(config.model_dump())
    selected = select_pilot_dataset(config, corpus, dataset)
    chunker = DocumentChunker(chunk_size=700, chunk_overlap=80)
    variants, case_rows = {}, []
    embedding_calls = embedding_chars = 0
    embedding_inputs_valid = True
    capacity_valid = True

    def count_embedding(texts):
        nonlocal embedding_calls, embedding_chars, embedding_inputs_valid
        if config.embedding is not None:
            try:
                batches = plan_embedding_batches(texts, config.embedding)
                embedding_calls += len(batches)
                embedding_chars += sum(len(text) for batch in batches for text in batch)
            except SupportOpsError:
                embedding_inputs_valid = False

    for case in selected.cases:
        # 共用评测的污染副本/版本规则；按有效语料缓存，不遗漏注入导致的整库重建。
        documents = _documents_for_case(corpus, case)
        effective = canonical_sha256([{"id": doc.id, "content": doc.content} for doc in documents])
        if effective not in variants:
            docs = []
            for doc in documents:
                chunks = chunker.chunk(doc)
                capacity_valid = capacity_valid and 1 <= len(chunks) <= 500
                count_embedding([chunk.content for chunk in chunks])
                docs.append({"id": doc.id, "version_sha256": doc.metadata["version_id"],
                             "content_chars": len(doc.content), "chunk_count": len(chunks),
                             "chunk_text_chars": sum(len(chunk.content) for chunk in chunks)})
            capacity_valid = capacity_valid and sum(row["chunk_count"] for row in docs) <= 2000
            variants[effective] = {"effective_corpus_sha256": effective, "documents": docs}
        count_embedding([case.question])
        case_rows.append({"id": case.id, "category": case.category, "question_chars": len(case.question),
                          "effective_corpus_sha256": effective, "has_test_pollution": case.poison is not None})

    chat_calls = len(selected.cases)
    chat_chars_ceiling = chat_calls * CHAT_MAX_INPUT_CHARS
    output_reservation = chat_calls * config.runtime.output_tokens
    checks = {
        "index_capacity": capacity_valid,
        "embedding_input_lengths": embedding_inputs_valid,
        "chat_request_limit": chat_calls <= config.limits.max_chat_requests,
        "embedding_request_limit": embedding_calls <= config.limits.max_embedding_requests,
        "combined_input_char_limit": embedding_chars + chat_chars_ceiling <= config.limits.max_input_chars,
        "reserved_output_token_limit": output_reservation <= config.limits.max_reserved_output_tokens,
    }
    passed = all(checks.values())
    binding = {"preflight_version": PREFLIGHT_VERSION, "evaluator_version": EVALUATOR_VERSION,
               "config_sha256": canonical_sha256(config.model_dump(mode="json")),
               "corpus_sha256": corpus.sha256, "dataset_sha256": config.scope.dataset_sha256,
               "selected_dataset_sha256": canonical_sha256(selected.model_dump(mode="json")),
               "system_rules_sha256": hashlib.sha256(SYSTEM_RULES.encode("utf-8")).hexdigest(),
               "pipeline": {"chunk_size": 700, "chunk_overlap": 80, "top_k": 5,
                            "chat_input_char_ceiling_per_request": CHAT_MAX_INPUT_CHARS}}
    return {
        "schema_version": 1, "scope": "offline_pilot_preflight", "preflight_version": PREFLIGHT_VERSION,
        "plan_id": config.plan_id, "configuration_validated": True, "offline_checks_passed": passed,
        "status": "awaiting_review" if passed else "preflight_rejected",
        "execution_authorized": False, "live_execution_supported": False,
        "production_ready": False, "external_model_calls": 0, "credentials_accessed": False,
        "binding": binding, "review_sha256": canonical_sha256(binding),
        "runtime": config.runtime.model_dump(mode="json"),
        "endpoints": {"chat": config.chat_endpoint.model_dump(mode="json"),
                      "embedding": config.embedding_endpoint.model_dump(mode="json") if config.embedding_endpoint else None},
        "selected_cases": case_rows, "corpus_variants": list(variants.values()), "checks": checks,
        "request_plan": {
            "assumption": "successful_pipeline_no_retries_cache_per_effective_corpus_per_document_batching",
            "failure_rebuild_attempts_included": False,
            "chat_request_upper_bound": chat_calls,
            "embedding_requests_if_all_stages_reached": embedding_calls if embedding_inputs_valid else None,
            "embedding_input_chars_if_all_stages_reached": embedding_chars if embedding_inputs_valid else None,
            "chat_input_chars_safety_ceiling": chat_chars_ceiling,
            "combined_input_chars_safety_ceiling": embedding_chars + chat_chars_ceiling if embedding_inputs_valid else None,
            "reserved_output_token_upper_bound": output_reservation,
            "limits": config.limits.model_dump(), "actual_usage": None, "cost": None,
            "monetary_cap_enforced": False,
        },
        "required_before_live": ["explicit_provider_model_region_data_and_network_approval",
                                 "isolated_sdk_factory_without_ambient_headers_proxies_or_redirects",
                                 "credential_injection_review_without_env_or_cli_secrets",
                                 "dns_tls_and_endpoint_ownership_review",
                                 "provider_pricing_and_financial_limit_review"],
        "limitations": [
            "预检通过不是联网授权、凭据验证、端点可用或供应商兼容性验证",
            "HTTPS/域名白名单仅做语法检查；未解析 DNS，不能作为 SSRF 或数据地域保证",
            "问题不在报告展开；gold 不进入请求计划，污染仍只存在于内存副本",
            "字符数不是 Token 或费用；Chat 使用适配器单次字符上界保守预留，不预测召回",
            "请求计划只描述成功路径；失败重建可增加尝试，运行时仍需调用账本硬限额；输入非法时计划量未知",
            "摘要用于复核配置和资料变化，不是签名、授权令牌或代码完整性证明",
            "没有真实模型调用或质量提升；工作台和 evaluate-answers 仍使用 Fake",
        ],
    }


__all__ = ["EndpointConfig", "PilotScope", "PilotConfig", "load_pilot_config", "select_pilot_dataset", "preflight_pilot"]
