"""HelloAgents 中所有 Agent 的抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from .config import Config
from .context import BaseContextProvider
from .exceptions import AgentError, ConfigurationError, HelloAgentsError
from .llm import HelloAgentsLLM, LLMResponse, MessageInput
from .message import Message
from .request_budget import BaseRequestBudget, RequestBudgetReport


class Agent(ABC):
    """定义 Agent 的统一入口、依赖和历史记录管理。"""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: str | None = None,
        config: Config | None = None,
        context_provider: BaseContextProvider | None = None,
        request_budget: BaseRequestBudget | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("Agent 名称不能为空。")
        if llm is None:
            raise ConfigurationError("Agent 必须提供 LLM 实例。")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ConfigurationError("system_prompt 必须是字符串或 None。")
        if context_provider is not None and not isinstance(
            context_provider, BaseContextProvider
        ):
            raise ConfigurationError(
                "context_provider 必须实现 BaseContextProvider。"
            )
        if request_budget is not None and not isinstance(request_budget, BaseRequestBudget):
            raise ConfigurationError("request_budget 必须实现 BaseRequestBudget。")

        self.name = name.strip()
        self.llm = llm
        self.system_prompt = system_prompt
        self.config = config or Config()
        self.context_provider = context_provider
        self._active_context = ""
        self._history: list[Message] = []
        self.request_budget = request_budget
        self._last_budget_report: RequestBudgetReport | None = None
        self._request_history_messages: list[Message] = []
        self._request_context_messages: list[Message] = []

    def run(self, input_text: str, **kwargs: Any) -> str:
        """校验公共约定，然后把具体任务交给子类执行。"""
        if not isinstance(input_text, str) or not input_text.strip():
            raise AgentError("Agent 输入必须是非空字符串。")

        with self._context_scope(input_text):
            try:
                result = self._execute(input_text, **kwargs)
            except HelloAgentsError:
                raise
            except Exception as error:
                raise AgentError(f"Agent '{self.name}' 运行失败。") from error

        if not isinstance(result, str) or not result.strip():
            raise AgentError(f"Agent '{self.name}' 必须返回非空字符串。")
        return result

    @abstractmethod
    def _execute(self, input_text: str, **kwargs: Any) -> str:
        """执行具体 Agent 策略，由每个子类实现。"""
        raise NotImplementedError

    def add_message(self, message: Message) -> None:
        """保存一条消息，并按照配置限制历史记录长度。"""
        if not isinstance(message, Message):
            raise AgentError("历史记录只能添加 Message 实例。")

        self._history.append(message.model_copy(deep=True))
        overflow = len(self._history) - self.config.max_history_length
        if overflow > 0:
            del self._history[:overflow]

    def clear_history(self) -> None:
        """清空当前 Agent 的全部历史消息。"""
        self._history.clear()

    def get_history(self) -> list[Message]:
        """返回历史记录的深拷贝，避免调用方修改内部状态。"""
        return [message.model_copy(deep=True) for message in self._history]

    def _build_messages(self, input_text: str) -> list[Message]:
        """按照系统提示词、历史、当前输入的顺序构造模型上下文。"""
        messages = self._build_system_messages(self.system_prompt)
        messages.extend(self._history_for_request())
        messages.append(Message(role="user", content=input_text))
        return messages

    def _build_system_messages(
        self,
        system_prompt: str | None,
    ) -> list[Message]:
        messages: list[Message] = []
        self._request_history_messages = []
        self._request_context_messages = []
        if system_prompt:
            messages.append(Message(role="system", content=system_prompt))
        if self._active_context:
            context_message = Message(role="system", content=self._active_context)
            messages.append(context_message)
            self._request_context_messages.append(context_message)
        return messages

    def _history_for_request(self) -> list[Message]:
        """标识本次组装中的历史副本，避免把本轮工具结果误当成旧历史删除。"""
        history = self.get_history()
        self._request_history_messages = history
        return history

    @property
    def last_budget_report(self) -> RequestBudgetReport | None:
        return self._last_budget_report.model_copy(deep=True) if self._last_budget_report else None

    def _prepare_llm_request(
        self,
        messages: Sequence[MessageInput],
        kwargs: Mapping[str, Any],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        stream: bool = False,
    ) -> tuple[Sequence[MessageInput], Mapping[str, Any]]:
        if self.request_budget is None:
            return messages, kwargs
        self._last_budget_report = None
        if {"max_completion_tokens", "max_output_tokens"} & kwargs.keys():
            raise ConfigurationError("启用请求预算时请统一使用 max_tokens 设置输出上限。")
        options = dict(kwargs)
        output_tokens = options.get("max_tokens", getattr(self.llm, "max_tokens", None))
        if output_tokens is None:
            output_tokens = self.request_budget.default_output_tokens
        options["max_tokens"] = output_tokens
        budget_options = dict(options)
        budget_options["stream"] = stream
        if isinstance(getattr(self.llm, "model", None), str):
            budget_options.setdefault("model", self.llm.model)
        if isinstance(getattr(self.llm, "temperature", None), (int, float)):
            budget_options.setdefault("temperature", self.llm.temperature)
        if tools is not None:
            budget_options.setdefault("tool_choice", "auto")
        history_ids = {id(message) for message in self._request_history_messages}
        context_ids = {id(message) for message in self._request_context_messages}
        prepared = self.request_budget.prepare(
            messages,
            history_indices=[index for index, message in enumerate(messages) if id(message) in history_ids],
            context_indices=[index for index, message in enumerate(messages) if id(message) in context_ids],
            tools=tools,
            output_tokens=output_tokens,
            request_options=budget_options,
        )
        self._last_budget_report = prepared.report.model_copy(deep=True)
        return prepared.messages, options

    def _invoke_llm(self, messages: Sequence[MessageInput], **kwargs: Any) -> str:
        """所有内置范式的文本调用入口，在实际调用前协调预算。"""
        prepared, options = self._prepare_llm_request(messages, kwargs)
        return self.llm.invoke(prepared, **options)

    def _invoke_llm_with_tools(
        self,
        messages: Sequence[MessageInput],
        tools: Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        prepared, options = self._prepare_llm_request(messages, kwargs, tools=tools)
        return self.llm.invoke_with_tools(prepared, tools, **options)

    def _stream_llm(
        self, messages: Sequence[MessageInput], **kwargs: Any
    ) -> Iterator[str]:
        prepared, options = self._prepare_llm_request(messages, kwargs, stream=True)
        yield from self.llm.stream_invoke(prepared, **options)

    @contextmanager
    def _context_scope(self, input_text: str) -> Iterator[None]:
        previous = self._active_context
        self._last_budget_report = None
        context = ""
        if self.context_provider is not None:
            context = self.context_provider.build_context(input_text)
            if not isinstance(context, str):
                raise AgentError("上下文提供器必须返回字符串。")
        self._active_context = context.strip()
        try:
            yield
        finally:
            self._active_context = previous
            self._request_history_messages = []
            self._request_context_messages = []

    def __str__(self) -> str:
        provider = getattr(self.llm, "provider", "unknown")
        return f"Agent(name={self.name}, provider={provider})"


__all__ = ["Agent"]
