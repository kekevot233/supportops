"""系统、历史、检索资料与工具载荷共享的完整请求预算。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.exceptions import ContextEngineeringError
from ..core.message import Message
from ..core.request_budget import BaseRequestBudget, BudgetedRequest, RequestBudgetReport
from .tokens import BaseTokenCounter, HeuristicTokenCounter


class RequestBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    context_window: int = Field(default=8192, strict=True, ge=128, le=10_000_000)
    output_tokens: int = Field(default=1024, strict=True, gt=0)
    safety_margin_tokens: int = Field(default=128, strict=True, ge=0)
    per_message_overhead: int = Field(default=4, strict=True, ge=0)
    request_overhead: int = Field(default=3, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_reserve(self) -> "RequestBudgetConfig":
        if self.output_tokens + self.safety_margin_tokens >= self.context_window:
            raise ValueError("输出预留和安全余量必须小于上下文窗口。")
        return self


class RequestBudgetManager(BaseRequestBudget):
    """按完整历史轮次裁剪副本，当前任务和本轮工具链保持原样。

    计数覆盖消息与工具 Schema 的紧凑 JSON 载荷、请求选项及可配置开销；这是一种
    可复现的本地估算，具体服务的隐藏聊天模板仍需通过安全余量校准。
    """

    def __init__(
        self,
        config: RequestBudgetConfig | None = None,
        *,
        token_counter: BaseTokenCounter | None = None,
    ) -> None:
        if config is not None and not isinstance(config, RequestBudgetConfig):
            raise ContextEngineeringError("config 必须是 RequestBudgetConfig。")
        if token_counter is not None and not isinstance(token_counter, BaseTokenCounter):
            raise ContextEngineeringError("token_counter 必须实现 BaseTokenCounter。")
        self.config = (config or RequestBudgetConfig()).model_copy(deep=True)
        self.token_counter = token_counter or HeuristicTokenCounter()

    @property
    def default_output_tokens(self) -> int:
        return self.config.output_tokens

    def prepare(
        self,
        messages: Sequence[Message | Mapping[str, Any]],
        *,
        history_indices: Sequence[int] = (),
        context_indices: Sequence[int] = (),
        tools: Sequence[Mapping[str, Any]] | None = None,
        output_tokens: int | None = None,
        request_options: Mapping[str, Any] | None = None,
    ) -> BudgetedRequest:
        reserve = self.default_output_tokens if output_tokens is None else output_tokens
        if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve <= 0:
            raise ContextEngineeringError("输出 Token 预留必须是正整数。")
        if reserve + self.config.safety_margin_tokens >= self.config.context_window:
            raise ContextEngineeringError("输出 Token 预留与安全余量已占满上下文窗口。")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
            raise ContextEngineeringError("请求预算需要非空消息序列。")
        copied = deepcopy(list(messages))
        rows = [self._message_dict(message) for message in copied]
        self._validate_tool_chain(rows)
        history = self._indices(history_indices, len(rows))
        contexts = self._indices(context_indices, len(rows))
        if history & contexts:
            raise ContextEngineeringError("历史和临时上下文索引不能重叠。")
        if any(rows[index]["role"] != "system" for index in contexts):
            raise ContextEngineeringError("可选上下文必须标记为独立的 system 资料消息。")
        # 至少保留一个当前 user 消息，禁止调用方把整个任务错误地标为历史。
        if not any(row["role"] == "user" and index not in history for index, row in enumerate(rows)):
            raise ContextEngineeringError("请求必须保留当前用户任务。")
        options = dict(request_options or {})
        if {"messages", "tools"} & options.keys():
            raise ContextEngineeringError("请求选项不能覆盖预算管理的 messages/tools。")
        options["max_tokens"] = reserve
        schema_tokens = self._count(self._json(tools)) if tools else 0
        removed: set[int] = set()
        dropped_turns = 0
        dropped_history = 0
        dropped_context = 0

        def count_request() -> int:
            current = [row for index, row in enumerate(rows) if index not in removed]
            payload = {**options, "messages": current}
            if tools is not None:
                payload["tools"] = tools
            return (
                self._count(self._json(payload))
                + len(current) * self.config.per_message_overhead
                + self.config.request_overhead
            )

        def fits(count: int) -> bool:
            return count + reserve + self.config.safety_margin_tokens <= self.config.context_window

        estimated = count_request()
        for group in self._history_turns(rows, history):
            if fits(estimated):
                break
            # 历史中手工添加的系统规则也保持完整。
            if any(rows[index]["role"] == "system" for index in group):
                continue
            removed.update(group)
            dropped_turns += 1
            dropped_history += len(group)
            estimated = count_request()
        for index in sorted(contexts):
            if fits(estimated):
                break
            removed.add(index)
            dropped_context += 1
            estimated = count_request()
        if not fits(estimated):
            raise ContextEngineeringError(
                "完整模型请求超过预算：系统规则、当前任务、工具定义或本轮工具结果无法容纳。"
            )
        final_rows = [row for index, row in enumerate(rows) if index not in removed]
        self._validate_tool_chain(final_rows)
        return BudgetedRequest(
            messages=[message for index, message in enumerate(copied) if index not in removed],
            report=RequestBudgetReport(
                token_counter=self.token_counter.name,
                context_window=self.config.context_window,
                estimated_input_tokens=estimated,
                reserved_output_tokens=reserve,
                safety_margin_tokens=self.config.safety_margin_tokens,
                tool_schema_tokens=schema_tokens,
                dropped_history_turns=dropped_turns,
                dropped_history_messages=dropped_history,
                dropped_context_messages=dropped_context,
            ),
        )

    def _count(self, text: str) -> int:
        try:
            result = self.token_counter.count_tokens(text)
        except Exception as error:
            raise ContextEngineeringError("请求 Token 计数失败。") from error
        if isinstance(result, bool) or not isinstance(result, int) or result < 0 or (text and result == 0):
            raise ContextEngineeringError("请求 Token 计数器返回无效计数。")
        return result

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ContextEngineeringError("请求预算载荷必须可以序列化为 JSON。") from error

    @staticmethod
    def _message_dict(message: Message | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(message, Message):
            return message.to_dict()
        if not isinstance(message, Mapping) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ContextEngineeringError("请求消息必须是 Message 或有效角色的映射。")
        return dict(message)

    @staticmethod
    def _indices(indices: Sequence[int], length: int) -> set[int]:
        if isinstance(indices, (str, bytes)) or not isinstance(indices, Sequence):
            raise ContextEngineeringError("可移除消息索引必须是整数序列。")
        if any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < length for index in indices):
            raise ContextEngineeringError("可移除消息索引越界或类型无效。")
        return set(indices)

    @staticmethod
    def _history_turns(rows: list[dict[str, Any]], history: set[int]) -> list[list[int]]:
        groups: list[list[int]] = []
        for index in sorted(history):
            if not groups or index != groups[-1][-1] + 1 or rows[index]["role"] in {"user", "system"}:
                groups.append([])
            groups[-1].append(index)
        return groups

    @staticmethod
    def _validate_tool_chain(rows: list[dict[str, Any]]) -> None:
        pending: set[str] = set()
        seen: set[str] = set()
        for row in rows:
            role = row["role"]
            if role == "tool":
                call_id = row.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ContextEngineeringError("工具结果缺少对应的 assistant 调用。")
                pending.remove(call_id)
                continue
            if pending:
                raise ContextEngineeringError("assistant 工具调用缺少完整结果。")
            calls = row.get("tool_calls")
            if calls is not None:
                if role != "assistant" or not isinstance(calls, list) or not calls:
                    raise ContextEngineeringError("tool_calls 必须位于 assistant 消息中。")
                for call in calls:
                    call_id = call.get("id") if isinstance(call, Mapping) else None
                    if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
                        raise ContextEngineeringError("工具调用 ID 无效或重复。")
                    pending.add(call_id)
                    seen.add(call_id)
        if pending:
            raise ContextEngineeringError("assistant 工具调用缺少完整结果。")


__all__ = ["RequestBudgetConfig", "RequestBudgetManager"]
