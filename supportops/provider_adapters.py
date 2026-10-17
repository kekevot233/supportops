"""OpenAI-compatible SDK 同步适配：不构造 SDK、不加载环境配置、不自动重试。"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Literal

from pydantic import Field

from hello_agents.embeddings import BaseEmbedder
from hello_agents.embeddings.base import normalize_vector, validate_text_batch

from .errors import SupportOpsError
from .models import AppModel
from .qa_runtime import Completion, Identifier, ProviderUsage

CHAT_MAX_INPUT_CHARS = 200_000


class ChatSDKConfig(AppModel):
    model: Identifier
    token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    json_mode: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class EmbeddingSDKConfig(AppModel):
    model: Identifier
    dimension: int = Field(ge=1, le=16384)
    request_dimensions: bool = False
    batch_size: int = Field(default=16, ge=1, le=128)
    max_text_chars: int = Field(default=8000, ge=1, le=32000)
    max_batch_chars: int = Field(default=32000, ge=1, le=256000)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class CallLimits(AppModel):
    max_chat_requests: int = Field(default=32, ge=1, le=1000)
    max_embedding_requests: int = Field(default=128, ge=1, le=1000)
    max_input_chars: int = Field(default=2_000_000, ge=1, le=100_000_000)
    max_reserved_output_tokens: int = Field(default=32768, ge=1, le=4_096_000)


def plan_embedding_batches(texts, config: EmbeddingSDKConfig) -> list[list[str]]:
    """纯本地的输入校验与分批；适配器和离线预检共用，不构造 SDK。"""
    config = EmbeddingSDKConfig.model_validate(config.model_dump())
    try:
        values = validate_text_batch(texts)
    except Exception:
        raise SupportOpsError("INVALID_REQUEST") from None
    if any(len(text) > min(config.max_text_chars, config.max_batch_chars) for text in values):
        raise SupportOpsError("INVALID_REQUEST")
    batches, current, size = [], [], 0
    for text in values:
        if current and (len(current) >= config.batch_size or size + len(text) > config.max_batch_chars):
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append(current)
    return batches


class ProviderCallLedger:
    """仅记录整数、稳定状态与用量，不存问题/正文/URL/SDK 异常或密钥。"""

    def __init__(self, limits: CallLimits | None = None):
        self.limits = CallLimits.model_validate((limits or CallLimits()).model_dump())
        self._lock, self._records = RLock(), []
        self._input_chars = self._output_reserved = self._rejections = 0

    def begin(self, kind, *, input_chars, output_tokens=0):
        if kind not in {"chat", "embedding"} or type(input_chars) is not int or input_chars < 1 or type(output_tokens) is not int or output_tokens < 0:
            raise SupportOpsError("INVALID_REQUEST")
        with self._lock:
            count = sum(row["kind"] == kind for row in self._records)
            maximum = self.limits.max_chat_requests if kind == "chat" else self.limits.max_embedding_requests
            if (count >= maximum or self._input_chars + input_chars > self.limits.max_input_chars
                    or self._output_reserved + output_tokens > self.limits.max_reserved_output_tokens):
                self._rejections += 1
                raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
            self._input_chars += input_chars
            self._output_reserved += output_tokens
            slot = len(self._records)
            self._records.append({"kind": kind, "state": "started", "usage": None})
            return slot

    def set_usage(self, slot, usage):
        with self._lock:
            self._records[slot]["usage"] = usage

    def finish(self, slot, state):
        with self._lock:
            self._records[slot]["state"] = state

    def snapshot(self):
        with self._lock:
            result = {"source": "sdk_create_attempts_not_provider_confirmation", "limits": self.limits.model_dump(),
                      "input_chars_reserved": self._input_chars, "output_tokens_reserved": self._output_reserved,
                      "budget_rejections": self._rejections, "cost": None}
            for kind in ("chat", "embedding"):
                rows = [row for row in self._records if row["kind"] == kind]
                known = [row["usage"] for row in rows if row["usage"] is not None]
                total = {name: sum(getattr(usage, name) for usage in known)
                         for name in ProviderUsage.model_fields} if known else None
                result[kind] = {"request_attempts": len(rows), "usage_receipt_count": len(known),
                    "failed_requests": sum(row["state"] == "failed" for row in rows),
                    "usage_source": "not_called" if not rows else "client_reported" if len(known) == len(rows) else "unknown_or_partial",
                    "actual_usage": total if rows and len(known) == len(rows) else None,
                    "known_usage_subtotal": total}
            return result


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _usage(response, *, embedding=False):
    value = _field(response, "usage")
    if value is None:
        return None
    return ProviderUsage(input_tokens=_field(value, "prompt_tokens"),
                         output_tokens=0 if embedding else _field(value, "completion_tokens"),
                         total_tokens=_field(value, "total_tokens"))


class _SDKAdapter:
    def _setup(self, sdk_client, config, ledger, owns_client):
        if type(owns_client) is not bool:
            raise ValueError("客户端所有权必须显式布尔值。")
        self._root, self._closed, self._owns_client = sdk_client, False, owns_client
        self.ledger = ledger if ledger is not None else ProviderCallLedger()
        try:
            # SDK 的 with_options 可能共享底层连接，因此 close 只关闭拥有的 root 一次。
            self._sdk = sdk_client.with_options(max_retries=0, timeout=config.timeout_seconds)
        except Exception:
            if owns_client:
                try:
                    sdk_client.close()
                except Exception:
                    pass
            raise SupportOpsError("EVALUATION_SETUP_FAILED") from None

    def close(self):
        if not self._closed:
            self._closed = True
            if self._owns_client:
                try:
                    self._root.close()
                except Exception:
                    raise SupportOpsError("API_UNAVAILABLE") from None


class CompatibleChatClient(_SDKAdapter):
    def __init__(self, sdk_client, config: ChatSDKConfig, *, ledger=None, owns_client=False):
        self.config = ChatSDKConfig.model_validate(config.model_dump())
        self._setup(sdk_client, self.config, ledger, owns_client)

    def complete(self, messages, *, max_tokens, temperature=None):
        if self._closed:
            raise SupportOpsError("MODEL_UNAVAILABLE")
        if (type(max_tokens) is not int or not 1 <= max_tokens <= 4096 or not isinstance(messages, list)
                or not 1 <= len(messages) <= 100 or (temperature is not None and
                    (type(temperature) not in {int, float} or not 0 <= temperature <= 2))):
            raise SupportOpsError("INVALID_REQUEST")
        if any(not isinstance(row, dict) or set(row) != {"role", "content"} or not isinstance(row["role"], str) or
               row["role"] not in {"system", "user", "assistant"} or not isinstance(row["content"], str)
               for row in messages):
            raise SupportOpsError("INVALID_REQUEST")
        size = sum(len(row["content"]) for row in messages)
        if not 1 <= size <= CHAT_MAX_INPUT_CHARS:
            raise SupportOpsError("INVALID_REQUEST")
        kwargs = {"model": self.config.model, "messages": deepcopy(messages), "n": 1, "stream": False,
                  self.config.token_parameter: max_tokens}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if self.config.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        slot = self.ledger.begin("chat", input_chars=size, output_tokens=max_tokens)
        try:
            response = self._sdk.chat.completions.create(**kwargs)
        except Exception:
            self.ledger.finish(slot, "failed")
            raise SupportOpsError("MODEL_UNAVAILABLE") from None
        receipt = None
        try:
            receipt = _usage(response)
            self.ledger.set_usage(slot, receipt)
            choices = _field(response, "choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("one choice required")
            choice = choices[0]
            if type(_field(choice, "index")) is not int or _field(choice, "index") != 0 or _field(choice, "finish_reason") != "stop":
                raise ValueError("incomplete or unsupported finish")
            message = _field(choice, "message")
            text = _field(message, "content")
            if (_field(message, "role") != "assistant" or not isinstance(text, str) or not text.strip() or len(text) > 16000
                    or _field(message, "tool_calls") or _field(message, "function_call")
                    or _field(message, "refusal") not in (None, "")):
                raise ValueError("unsupported message")
            self.ledger.finish(slot, "succeeded")
            return Completion(text, receipt)
        except Exception:
            # 截断/安全拒绝不是资料不足；即使正文不可用，也保留已收到的独立用量。
            self.ledger.finish(slot, "failed")
            return Completion("", receipt, error_code="MODEL_RESPONSE_INVALID")


class CompatibleEmbedder(_SDKAdapter, BaseEmbedder):
    def __init__(self, sdk_client, config: EmbeddingSDKConfig, *, ledger=None, owns_client=False):
        self.config = EmbeddingSDKConfig.model_validate(config.model_dump())
        self._setup(sdk_client, self.config, ledger, owns_client)

    @property
    def dimension(self):
        return self.config.dimension

    def embed(self, text):
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):
        if self._closed:
            raise SupportOpsError("INDEX_UNAVAILABLE")
        # 在发出第一个请求前验证全部输入；空批次不发请求。
        batches = plan_embedding_batches(texts, self.config)
        vectors = []
        for batch in batches:
            slot = self.ledger.begin("embedding", input_chars=sum(map(len, batch)))
            kwargs = {"model": self.config.model, "input": batch, "encoding_format": "float"}
            if self.config.request_dimensions:
                kwargs["dimensions"] = self.dimension
            try:
                response = self._sdk.embeddings.create(**kwargs)
                self.ledger.set_usage(slot, _usage(response, embedding=True))
                data = _field(response, "data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("embedding count mismatch")
                ordered = {}
                for item in data:
                    index = _field(item, "index")
                    if type(index) is not int or not 0 <= index < len(batch) or index in ordered:
                        raise ValueError("invalid embedding index")
                    vector = normalize_vector(_field(item, "embedding"), expected_dimension=self.dimension)
                    if not any(vector):
                        raise ValueError("zero embedding")
                    ordered[index] = vector
                vectors.extend(ordered[number] for number in range(len(batch)))
                self.ledger.finish(slot, "succeeded")
            except Exception:
                self.ledger.finish(slot, "failed")
                raise SupportOpsError("INDEX_UNAVAILABLE") from None
        return vectors


__all__ = ["ChatSDKConfig", "EmbeddingSDKConfig", "CallLimits", "ProviderCallLedger", "CompatibleChatClient",
           "CompatibleEmbedder", "CHAT_MAX_INPUT_CHARS", "plan_embedding_batches"]
