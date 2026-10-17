"""Agent 请求预算的依赖契约，不依赖上下文组件的具体实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .message import Message


class RequestBudgetReport(BaseModel):
    """只包含数量的请求预算诊断，不保存提示词或工具正文。"""

    model_config = ConfigDict(extra="forbid")

    token_counter: str
    context_window: int = Field(gt=0)
    estimated_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(gt=0)
    safety_margin_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    dropped_history_turns: int = Field(ge=0)
    dropped_history_messages: int = Field(ge=0)
    dropped_context_messages: int = Field(ge=0)


@dataclass(frozen=True)
class BudgetedRequest:
    messages: list[Message | Mapping[str, Any]]
    report: RequestBudgetReport


class BaseRequestBudget(ABC):
    """返回经过预算调整的消息副本；只允许移除明确标记为可选的消息。"""

    @property
    @abstractmethod
    def default_output_tokens(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def prepare(
        self,
        messages: Sequence[Message | Mapping[str, Any]],
        *,
        history_indices: Sequence[int],
        context_indices: Sequence[int],
        tools: Sequence[Mapping[str, Any]] | None,
        output_tokens: int,
        request_options: Mapping[str, Any],
    ) -> BudgetedRequest:
        raise NotImplementedError


__all__ = ["BaseRequestBudget", "BudgetedRequest", "RequestBudgetReport"]
