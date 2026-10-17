"""HelloAgents 的 OpenAI-compatible 大语言模型统一接口。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from numbers import Real
from typing import Annotated, Any, Literal

from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .config import Config
from .exceptions import ConfigurationError, LLMError
from .message import Message


MessageInput = Message | Mapping[str, Any]
ToolChoice = Literal["auto", "none", "required"] | Mapping[str, Any]
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LLMToolCall(BaseModel):
    """一次经过校验的原生函数调用请求。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    arguments: dict[str, Any]

    @field_validator("id", "name", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _TOOL_NAME_PATTERN.fullmatch(value):
            raise ValueError("工具名称格式无效。")
        return value

    def to_api_dict(self) -> dict[str, Any]:
        """转换为后续 Chat Completions 请求需要的 tool_calls 项。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(
                    self.arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        }


class LLMResponse(BaseModel):
    """同时表示模型文本和原生工具调用的结构化响应。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    finish_reason: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "LLMResponse":
        if self.content is not None and not self.content.strip():
            self.content = None
        if self.content is None and not self.tool_calls:
            raise ValueError("结构化响应必须包含文本或工具调用。")
        return self

    def to_assistant_message(self) -> dict[str, Any]:
        """构造下一轮原生工具调用所需的 assistant 消息。"""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content,
        }
        if self.tool_calls:
            message["tool_calls"] = [
                tool_call.to_api_dict() for tool_call in self.tool_calls
            ]
        return message


class HelloAgentsLLM:
    """把 OpenAI-compatible 模型封装成稳定的文本调用接口。"""

    _PROVIDER_API_KEY_ENV = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "modelscope": "MODELSCOPE_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
    }
    _PROVIDER_DEFAULT_BASE_URL = {
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "modelscope": "https://api-inference.modelscope.cn/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "ollama": "http://localhost:11434/v1",
        "vllm": "http://localhost:8000/v1",
    }

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
        *,
        config: Config | None = None,
        client: Any | None = None,
        environ: Mapping[str, str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """创建 LLM 客户端；注入 ``client`` 时不会解析或校验凭证。"""
        self.config = config or Config()
        environment = os.environ if environ is None else environ

        self.model = self._resolve_model(model, environment)
        self.temperature = self._resolve_temperature(temperature)
        self.max_tokens = self._resolve_max_tokens(max_tokens)
        self.timeout = self._resolve_timeout(timeout)
        self.provider = self._resolve_provider(
            provider or self.config.default_provider,
            api_key=api_key,
            base_url=base_url,
            environ=environment,
        )

        if client is not None:
            self._client = client
            self.base_url = base_url or environment.get("LLM_BASE_URL")
            return

        resolved_api_key, resolved_base_url = self._resolve_credentials(
            api_key=api_key,
            base_url=base_url,
            environ=environment,
        )
        self.base_url = resolved_base_url

        client_options: dict[str, Any] = {
            "api_key": resolved_api_key,
            "timeout": self.timeout,
        }
        if resolved_base_url is not None:
            client_options["base_url"] = resolved_base_url

        try:
            self._client = OpenAI(**client_options)
        except Exception as error:
            raise LLMError("大语言模型客户端初始化失败。") from error

    def invoke(
        self,
        messages: Sequence[MessageInput],
        **kwargs: Any,
    ) -> str:
        """执行一次非流式调用并返回完整文本。"""
        request = self._build_request(messages, stream=False, options=kwargs)

        try:
            response = self._client.chat.completions.create(**request)
        except Exception as error:
            raise LLMError("大语言模型调用失败。") from error

        return self._extract_response_text(response)

    def invoke_with_tools(
        self,
        messages: Sequence[MessageInput],
        tools: Sequence[Mapping[str, Any]],
        *,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> LLMResponse:
        """调用原生 Function Calling，并返回文本与工具调用结构。"""
        conflicts = {"tools", "tool_choice"}.intersection(kwargs)
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise LLMError(f"这些工具参数不能重复提供：{names}。")

        request = self._build_request(
            messages,
            stream=False,
            options=kwargs,
            message_normalizer=self._normalize_tool_messages,
        )
        request["tools"] = self._normalize_tools(tools)
        request["tool_choice"] = self._normalize_tool_choice(tool_choice)

        try:
            response = self._client.chat.completions.create(**request)
        except Exception as error:
            raise LLMError("大语言模型工具调用失败。") from error

        return self._extract_structured_response(response)

    def stream_invoke(
        self,
        messages: Sequence[MessageInput],
        **kwargs: Any,
    ) -> Iterator[str]:
        """执行一次流式调用，并逐段产出非空文本。"""
        request = self._build_request(messages, stream=True, options=kwargs)

        try:
            stream = self._client.chat.completions.create(**request)
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue

                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    yield content
        except LLMError:
            raise
        except Exception as error:
            raise LLMError("大语言模型流式调用失败。") from error

    def _build_request(
        self,
        messages: Sequence[MessageInput],
        *,
        stream: bool,
        options: Mapping[str, Any],
        message_normalizer: Any | None = None,
    ) -> dict[str, Any]:
        """组合公共配置、单次调用参数和标准消息。"""
        reserved_names = {"model", "messages", "stream"}
        conflicts = reserved_names.intersection(options)
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise LLMError(f"这些请求参数由 HelloAgentsLLM 管理，不能覆盖：{names}。")

        normalize_messages = message_normalizer or self._normalize_messages
        request: dict[str, Any] = {
            "model": self.model,
            "messages": normalize_messages(messages),
            "temperature": self.temperature,
            "stream": stream,
        }
        if self.max_tokens is not None:
            request["max_tokens"] = self.max_tokens

        request.update(options)
        return request

    @staticmethod
    def _normalize_messages(
        messages: Sequence[MessageInput],
    ) -> list[dict[str, str]]:
        """把 Message 或映射统一转换为 API 所需的 role/content 字典。"""
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
            raise LLMError("messages 必须是消息序列。")
        if not messages:
            raise LLMError("messages 不能为空。")

        normalized: list[dict[str, str]] = []
        for index, item in enumerate(messages, start=1):
            if isinstance(item, Message):
                normalized.append(item.to_dict())
                continue

            if not isinstance(item, Mapping):
                raise LLMError(f"第 {index} 条消息必须是 Message 或映射。")

            try:
                message = Message(
                    role=item["role"],
                    content=item["content"],
                )
            except (KeyError, TypeError, ValidationError) as error:
                raise LLMError(f"第 {index} 条消息格式无效。") from error
            normalized.append(message.to_dict())

        return normalized

    @classmethod
    def _normalize_tool_messages(
        cls,
        messages: Sequence[MessageInput],
    ) -> list[dict[str, Any]]:
        """校验原生工具循环消息，包括 assistant/tool 的关联字段。"""
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
            raise LLMError("messages 必须是消息序列。")
        if not messages:
            raise LLMError("messages 不能为空。")

        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(messages, start=1):
            if isinstance(item, Message):
                if item.role == "tool":
                    raise LLMError(
                        f"第 {index} 条 tool 消息必须提供 tool_call_id。"
                    )
                normalized.append(item.to_dict())
                continue
            if not isinstance(item, Mapping):
                raise LLMError(f"第 {index} 条消息必须是 Message 或映射。")

            role = item.get("role")
            content = item.get("content")
            if role in {"system", "user"}:
                if not isinstance(content, str):
                    raise LLMError(f"第 {index} 条消息格式无效。")
                normalized.append({"role": role, "content": content})
                continue

            if role == "assistant":
                raw_tool_calls = item.get("tool_calls")
                if content is not None and not isinstance(content, str):
                    raise LLMError(f"第 {index} 条消息格式无效。")
                if raw_tool_calls is None:
                    if not isinstance(content, str):
                        raise LLMError(f"第 {index} 条消息格式无效。")
                    normalized.append({"role": role, "content": content})
                    continue

                tool_calls = cls._normalize_outbound_tool_calls(
                    raw_tool_calls,
                    message_index=index,
                )
                normalized.append(
                    {
                        "role": role,
                        "content": content,
                        "tool_calls": tool_calls,
                    }
                )
                continue

            if role == "tool":
                tool_call_id = item.get("tool_call_id")
                if (
                    not isinstance(content, str)
                    or not isinstance(tool_call_id, str)
                    or not tool_call_id.strip()
                ):
                    raise LLMError(f"第 {index} 条 tool 消息格式无效。")
                normalized.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id.strip(),
                        "content": content,
                    }
                )
                continue

            raise LLMError(f"第 {index} 条消息格式无效。")
        return normalized

    @staticmethod
    def _normalize_outbound_tool_calls(
        tool_calls: Any,
        *,
        message_index: int,
    ) -> list[dict[str, Any]]:
        if isinstance(tool_calls, (str, bytes)) or not isinstance(
            tool_calls,
            Sequence,
        ):
            raise LLMError(f"第 {message_index} 条 assistant 工具调用格式无效。")

        normalized: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if (
                not isinstance(tool_call, Mapping)
                or tool_call.get("type") != "function"
            ):
                raise LLMError(
                    f"第 {message_index} 条 assistant 工具调用格式无效。"
                )
            function = tool_call.get("function")
            if not isinstance(function, Mapping):
                raise LLMError(
                    f"第 {message_index} 条 assistant 工具调用格式无效。"
                )
            try:
                parsed = LLMToolCall(
                    id=tool_call.get("id"),
                    name=function.get("name"),
                    arguments=json.loads(function.get("arguments")),
                )
            except (TypeError, json.JSONDecodeError, ValidationError) as error:
                raise LLMError(
                    f"第 {message_index} 条 assistant 工具调用格式无效。"
                ) from error
            normalized.append(parsed.to_api_dict())
        if not normalized:
            raise LLMError(
                f"第 {message_index} 条 assistant 工具调用不能为空。"
            )
        return normalized

    @staticmethod
    def _normalize_tools(
        tools: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """对工具 Schema 做最小但严格的结构校验。"""
        if isinstance(tools, (str, bytes)) or not isinstance(tools, Sequence):
            raise LLMError("tools 必须是工具 Schema 序列。")
        if not tools:
            raise LLMError("tools 不能为空。")

        normalized: list[dict[str, Any]] = []
        for index, tool in enumerate(tools, start=1):
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                raise LLMError(f"第 {index} 个工具 Schema 格式无效。")
            function = tool.get("function")
            if not isinstance(function, Mapping):
                raise LLMError(f"第 {index} 个工具 Schema 格式无效。")
            name = function.get("name")
            parameters = function.get("parameters")
            if (
                not isinstance(name, str)
                or not _TOOL_NAME_PATTERN.fullmatch(name)
                or not isinstance(parameters, Mapping)
            ):
                raise LLMError(f"第 {index} 个工具 Schema 格式无效。")
            normalized.append(deepcopy(dict(tool)))
        return normalized

    @staticmethod
    def _normalize_tool_choice(tool_choice: ToolChoice) -> str | dict[str, Any]:
        if isinstance(tool_choice, str):
            if tool_choice not in {"auto", "none", "required"}:
                raise LLMError("tool_choice 字符串必须是 auto、none 或 required。")
            return tool_choice

        if not isinstance(tool_choice, Mapping):
            raise LLMError("tool_choice 必须是合法字符串或函数选择映射。")
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if (
            tool_choice.get("type") != "function"
            or not isinstance(name, str)
            or not _TOOL_NAME_PATTERN.fullmatch(name)
        ):
            raise LLMError("tool_choice 函数选择映射格式无效。")
        return deepcopy(dict(tool_choice))

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """从非流式 Chat Completions 响应中提取文本。"""
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as error:
            raise LLMError("模型响应格式无效。") from error

        if not isinstance(content, str) or not content:
            raise LLMError("模型响应未包含文本内容。")
        return content

    @classmethod
    def _extract_structured_response(cls, response: Any) -> LLMResponse:
        """从 Chat Completions 响应中提取文本和原生工具调用。"""
        try:
            choices = cls._read_field(response, "choices")
            choice = choices[0]
            message = cls._read_field(choice, "message")
        except (AttributeError, IndexError, KeyError, TypeError) as error:
            raise LLMError("模型工具调用响应格式无效。") from error

        content = cls._read_field(message, "content", None)
        if content is not None and not isinstance(content, str):
            raise LLMError("模型工具调用响应格式无效。")

        raw_tool_calls = cls._read_field(message, "tool_calls", None) or []
        if isinstance(raw_tool_calls, (str, bytes)) or not isinstance(
            raw_tool_calls,
            Sequence,
        ):
            raise LLMError("模型工具调用响应格式无效。")

        tool_calls: list[LLMToolCall] = []
        tool_call_ids: set[str] = set()
        for raw_tool_call in raw_tool_calls:
            try:
                if cls._read_field(raw_tool_call, "type") != "function":
                    raise TypeError
                function = cls._read_field(raw_tool_call, "function")
                arguments_text = cls._read_field(function, "arguments")
                if not isinstance(arguments_text, str):
                    raise TypeError
                arguments = json.loads(arguments_text)
                if not isinstance(arguments, dict):
                    raise LLMError("模型返回的工具参数必须是 JSON 对象。")
                tool_call = LLMToolCall(
                    id=cls._read_field(raw_tool_call, "id"),
                    name=cls._read_field(function, "name"),
                    arguments=arguments,
                )
                if tool_call.id in tool_call_ids:
                    raise LLMError("模型返回了重复的工具调用 ID。")
            except LLMError:
                raise
            except json.JSONDecodeError as error:
                raise LLMError("模型返回的工具参数不是有效 JSON。") from error
            except (AttributeError, KeyError, TypeError, ValidationError) as error:
                raise LLMError("模型工具调用响应格式无效。") from error
            tool_call_ids.add(tool_call.id)
            tool_calls.append(tool_call)

        finish_reason = cls._read_field(choice, "finish_reason", None)
        if finish_reason is not None and not isinstance(finish_reason, str):
            finish_reason = None

        try:
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except ValidationError as error:
            raise LLMError("模型响应未包含文本或工具调用。") from error

    @staticmethod
    def _read_field(source: Any, name: str, default: Any = ...) -> Any:
        if isinstance(source, Mapping):
            if default is ...:
                return source[name]
            return source.get(name, default)
        if default is ...:
            return getattr(source, name)
        return getattr(source, name, default)

    def _resolve_model(
        self,
        model: str | None,
        environ: Mapping[str, str],
    ) -> str:
        value = model or environ.get("LLM_MODEL_ID") or self.config.default_model
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError("模型名称不能为空。")
        return value.strip()

    def _resolve_temperature(self, temperature: float | None) -> float:
        value = self.config.temperature if temperature is None else temperature
        if isinstance(value, bool) or not isinstance(value, Real) or not 0 <= value <= 2:
            raise ConfigurationError("temperature 必须是 0 到 2 之间的数字。")
        return float(value)

    def _resolve_max_tokens(self, max_tokens: int | None) -> int | None:
        value = self.config.max_tokens if max_tokens is None else max_tokens
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ConfigurationError("max_tokens 必须是正整数或 None。")
        return value

    def _resolve_timeout(self, timeout: float | None) -> float:
        value = self.config.request_timeout_seconds if timeout is None else timeout
        if isinstance(value, bool) or not isinstance(value, Real) or value <= 0:
            raise ConfigurationError("timeout 必须是正数。")
        return float(value)

    def _resolve_provider(
        self,
        provider: str,
        *,
        api_key: str | None,
        base_url: str | None,
        environ: Mapping[str, str],
    ) -> str:
        requested = provider.strip().lower()
        if not requested:
            raise ConfigurationError("provider 不能为空。")
        if requested != "auto":
            return requested
        return self._auto_detect_provider(api_key, base_url, environ)

    def _auto_detect_provider(
        self,
        api_key: str | None,
        base_url: str | None,
        environ: Mapping[str, str],
    ) -> str:
        """按专用密钥、URL、通用配置的顺序推断服务商。"""
        for provider, environment_name in self._PROVIDER_API_KEY_ENV.items():
            if environ.get(environment_name):
                return provider

        actual_base_url = base_url or environ.get("LLM_BASE_URL")
        if actual_base_url:
            normalized_url = actual_base_url.lower()
            url_markers = (
                ("api.deepseek.com", "deepseek"),
                ("api.openai.com", "openai"),
                ("api-inference.modelscope.cn", "modelscope"),
                ("open.bigmodel.cn", "zhipu"),
                (":11434", "ollama"),
                (":8000", "vllm"),
            )
            for marker, detected_provider in url_markers:
                if marker in normalized_url:
                    return detected_provider
            return "openai_compatible"

        if api_key or environ.get("LLM_API_KEY"):
            return "openai_compatible"
        return "openai_compatible"

    def _resolve_credentials(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        environ: Mapping[str, str],
    ) -> tuple[str, str | None]:
        """解析凭证但不保存密钥，降低意外泄露的风险。"""
        provider_key_name = self._PROVIDER_API_KEY_ENV.get(self.provider)
        provider_key = environ.get(provider_key_name) if provider_key_name else None
        resolved_api_key = api_key or provider_key or environ.get("LLM_API_KEY")

        resolved_base_url = (
            base_url
            or environ.get("LLM_BASE_URL")
            or self._PROVIDER_DEFAULT_BASE_URL.get(self.provider)
        )

        if self.provider in {"ollama", "vllm", "local"}:
            resolved_api_key = resolved_api_key or self.provider

        if not resolved_api_key:
            raise ConfigurationError(
                f"未找到 {self.provider} 的 API Key；请显式传入或配置相应环境变量。"
            )
        if self.provider == "openai_compatible" and not resolved_base_url:
            raise ConfigurationError(
                "使用通用 OpenAI-compatible 配置时必须提供 base_url 或 LLM_BASE_URL。"
            )

        return resolved_api_key, resolved_base_url


__all__ = [
    "HelloAgentsLLM",
    "LLMResponse",
    "LLMToolCall",
    "MessageInput",
    "ToolChoice",
]
