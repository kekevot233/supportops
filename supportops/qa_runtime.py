"""评测运行时：惰性工厂、外部调用显式许可、与模型正文分离的用量回执。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Callable, Literal, Protocol

from pydantic import Field, model_validator

from .answering import DemoQuoteLLM
from .document_index import DocumentIndex, LocalDocumentIndex
from .errors import SupportOpsError
from .models import AppModel

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]


class RuntimeSpec(AppModel):
    runtime_id: Identifier
    execution: Literal["offline_fake", "offline_local", "external"]
    model_id: Identifier
    model_revision: Identifier
    embedding_id: Identifier
    embedding_revision: Identifier
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    output_tokens: int = Field(default=1024, ge=1, le=4096)


class ProviderUsage(AppModel):
    """由可信客户端单独提供，不从模型 JSON、正文或本地估算中猜测。"""

    input_tokens: int = Field(ge=0, le=100_000_000)
    output_tokens: int = Field(ge=0, le=100_000_000)
    total_tokens: int = Field(ge=0, le=200_000_000)

    @model_validator(mode="after")
    def consistent_total(self):
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("用量回执总数不一致。")
        return self


@dataclass(frozen=True)
class Completion:
    text: str
    usage: ProviderUsage | None = None
    error_code: Literal["MODEL_RESPONSE_INVALID"] | None = None


class CompletionClient(Protocol):
    def complete(self, messages: list[dict], *, max_tokens: int, temperature: float | None = None) -> Completion: ...


class DemoCompletionClient:
    def complete(self, messages, *, max_tokens, temperature=None):
        return Completion(DemoQuoteLLM().invoke(messages, max_tokens=max_tokens))


class MeteredLLM:
    """把 complete 回执适配到既有 invoke；每次尝试先记录未知，不自动重试。"""

    def __init__(self, client: CompletionClient, *, temperature=None):
        self._client = client
        self._temperature = temperature
        self.receipts: list[ProviderUsage | None] = []

    def invoke(self, messages, *, max_tokens):
        slot = len(self.receipts)
        self.receipts.append(None)
        options = {"max_tokens": max_tokens}
        if self._temperature is not None:
            options["temperature"] = self._temperature
        result = self._client.complete(messages, **options)
        if not isinstance(result, Completion) or not isinstance(result.text, str):
            raise ValueError("客户端必须返回 Completion。")
        if result.usage is not None and not isinstance(result.usage, ProviderUsage):
            raise ValueError("用量必须是独立的结构化回执。")
        self.receipts[slot] = ProviderUsage.model_validate(result.usage.model_dump()) if result.usage is not None else None
        if result.error_code is not None:
            if result.error_code != "MODEL_RESPONSE_INVALID":
                raise ValueError("非法客户端响应错误。")
            raise SupportOpsError(result.error_code)
        return result.text

    def close(self):
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class EvaluationRuntime:
    spec: RuntimeSpec
    index_factory: Callable[[], DocumentIndex] = field(repr=False)
    client_factory: Callable[[], CompletionClient] = field(repr=False)
    telemetry: Callable[[], dict] | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.spec, RuntimeSpec) or not callable(self.index_factory) or not callable(self.client_factory):
            raise ValueError("运行时需要严格配置和显式惰性工厂。")
        if self.telemetry is not None and not callable(self.telemetry):
            raise ValueError("运行诊断必须是显式回调。")
        object.__setattr__(self, "spec", RuntimeSpec.model_validate(self.spec.model_dump()))

    def open(self, *, allow_external_calls=False):
        # 先检查许可，再运行任何可能构造 SDK、加载模型或联网的工厂。
        if self.spec.execution == "external" and allow_external_calls is not True:
            raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
        index = client = None
        try:
            index = self.index_factory()
            if (index.embedding_id != self.spec.embedding_id or
                    index.embedding_revision != self.spec.embedding_revision):
                raise ValueError("索引模型元数据不匹配。")
            if type(index.dimension) is not int or not 1 <= index.dimension <= 16384:
                raise ValueError("索引维度无效。")
            client = self.client_factory()
            if not callable(getattr(client, "complete", None)):
                raise ValueError("客户端契约无效。")
            return index, MeteredLLM(client, temperature=self.spec.temperature)
        except Exception:
            for component in (client, index):
                close = getattr(component, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            raise SupportOpsError("EVALUATION_SETUP_FAILED") from None


def fake_runtime() -> EvaluationRuntime:
    return EvaluationRuntime(RuntimeSpec(runtime_id="hashing-demo-quote-v1", execution="offline_fake",
        model_id="demo-quote", model_revision="v1", embedding_id="hashing-blake2b", embedding_revision="v1"),
        LocalDocumentIndex, DemoCompletionClient)


__all__ = ["RuntimeSpec", "ProviderUsage", "Completion", "CompletionClient", "MeteredLLM", "EvaluationRuntime", "fake_runtime"]
