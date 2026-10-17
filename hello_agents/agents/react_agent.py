"""基于严格文本协议和 ToolRegistry 的 ReAct Agent。"""

from __future__ import annotations

import json
import re
from string import Formatter
from typing import Any

from ..core.agent import Agent
from ..core.config import Config
from ..core.context import BaseContextProvider
from ..core.exceptions import AgentError, ConfigurationError
from ..core.llm import HelloAgentsLLM
from ..core.message import Message
from ..core.request_budget import BaseRequestBudget
from ..tools.registry import ToolRegistry


DEFAULT_REACT_PROMPT = """你需要使用 ReAct 流程解决任务。

## 可用工具
{tools}

## 工具参数 Schema
{schemas}

## 输出协议
每次响应只能执行一个步骤，并且必须完整使用以下两行格式：
Thought: 对当前步骤的简短分析
Action: <工具名>:<严格 JSON 对象>

当已经获得最终答案时，Action 改为：
Action: Finish[最终答案]

禁止在协议前后添加其他文本。工具参数必须符合对应 Schema。

## 当前任务
{question}

## 本轮执行历史
{history}
"""

_RESPONSE_PATTERN = re.compile(
    r"Thought:\s*(?P<thought>.+?)\r?\nAction:\s*(?P<action>.+)",
    re.DOTALL,
)
_FINISH_PATTERN = re.compile(r"Finish\[(?P<answer>.*)\]", re.DOTALL)
_TOOL_ACTION_PATTERN = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*):(?P<arguments>.+)",
    re.DOTALL,
)


class ReActAgent(Agent):
    """循环执行 Thought、Action、Observation，直到得到 Finish。"""

    MAX_STEPS_LIMIT = 20
    _PROMPT_FIELDS = {"tools", "schemas", "question", "history"}

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: ToolRegistry,
        system_prompt: str | None = None,
        config: Config | None = None,
        *,
        max_steps: int = 5,
        prompt_template: str = DEFAULT_REACT_PROMPT,
        context_provider: BaseContextProvider | None = None,
        request_budget: BaseRequestBudget | None = None,
    ) -> None:
        if not isinstance(tool_registry, ToolRegistry):
            raise ConfigurationError("tool_registry 必须是 ToolRegistry。")
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= self.MAX_STEPS_LIMIT
        ):
            raise ConfigurationError(
                f"max_steps 必须是 1 到 {self.MAX_STEPS_LIMIT} 之间的整数。"
            )
        self._validate_prompt_template(prompt_template)

        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            context_provider=context_provider,
            request_budget=request_budget,
        )
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        self.prompt_template = prompt_template

    def _execute(self, input_text: str, **kwargs: Any) -> str:
        scratchpad: list[str] = []

        for current_step in range(1, self.max_steps + 1):
            prompt = self._build_react_prompt(input_text, scratchpad)
            response = self._invoke_llm(
                self._build_react_messages(prompt),
                **kwargs,
            )
            thought, action = self._parse_response(response)

            final_answer = self._parse_finish(action)
            if final_answer is not None:
                self._save_turn(input_text, final_answer)
                return final_answer

            if current_step >= self.max_steps:
                raise AgentError(
                    f"Agent '{self.name}' 达到最大 ReAct 步数 "
                    f"{self.max_steps}，仍未生成最终回答。"
                )

            tool_name, parameters = self._parse_tool_action(action)
            observation = self.tool_registry.execute_tool(
                tool_name,
                parameters,
            )
            scratchpad.extend(
                [
                    f"Thought: {thought}",
                    f"Action: {action}",
                    f"Observation: {observation}",
                ]
            )

        raise AgentError(f"Agent '{self.name}' 的 ReAct 循环意外结束。")

    def _build_react_prompt(
        self,
        input_text: str,
        scratchpad: list[str],
    ) -> str:
        schemas = json.dumps(
            self.tool_registry.to_openai_schemas(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self.prompt_template.format(
            tools=self.tool_registry.get_tools_description(),
            schemas=schemas,
            question=input_text,
            history="\n".join(scratchpad) if scratchpad else "暂无执行历史",
        )

    def _build_react_messages(self, prompt: str) -> list[Message]:
        system_prompt = self.system_prompt or "你是一个严格遵循 ReAct 协议的助手。"
        messages = self._build_system_messages(system_prompt)
        messages.extend(self._history_for_request())
        messages.append(Message(role="user", content=prompt))
        return messages

    @staticmethod
    def _parse_response(response: Any) -> tuple[str, str]:
        if not isinstance(response, str) or not response.strip():
            raise AgentError("ReAct 模型响应必须是非空字符串。")

        match = _RESPONSE_PATTERN.fullmatch(response.strip())
        if match is None:
            raise AgentError("ReAct 响应必须严格包含 Thought 和 Action 两行。")

        thought = match.group("thought").strip()
        action = match.group("action").strip()
        if not thought or not action:
            raise AgentError("ReAct 响应中的 Thought 和 Action 不能为空。")
        return thought, action

    @staticmethod
    def _parse_finish(action: str) -> str | None:
        if not action.startswith("Finish"):
            return None

        match = _FINISH_PATTERN.fullmatch(action)
        if match is None or not match.group("answer").strip():
            raise AgentError("ReAct Finish 必须包含非空的最终答案。")
        return match.group("answer").strip()

    @staticmethod
    def _parse_tool_action(action: str) -> tuple[str, dict[str, Any]]:
        match = _TOOL_ACTION_PATTERN.fullmatch(action)
        if match is None:
            raise AgentError(
                "ReAct 工具 Action 必须使用 '<工具名>:<JSON对象>' 格式。"
            )

        try:
            parameters = json.loads(match.group("arguments"))
        except json.JSONDecodeError as error:
            raise AgentError("ReAct 工具参数不是有效 JSON。") from error
        if not isinstance(parameters, dict):
            raise AgentError("ReAct 工具参数必须是 JSON 对象。")
        return match.group("name"), parameters

    @classmethod
    def _validate_prompt_template(cls, prompt_template: Any) -> None:
        if not isinstance(prompt_template, str) or not prompt_template.strip():
            raise ConfigurationError("prompt_template 必须是非空字符串。")
        try:
            fields = {
                field_name
                for _, field_name, _, _ in Formatter().parse(prompt_template)
                if field_name
            }
        except ValueError as error:
            raise ConfigurationError("prompt_template 格式无效。") from error
        if fields != cls._PROMPT_FIELDS:
            expected = ", ".join(sorted(cls._PROMPT_FIELDS))
            raise ConfigurationError(
                f"prompt_template 必须且只能包含这些字段：{expected}。"
            )

    def _save_turn(self, input_text: str, response: str) -> None:
        self.add_message(Message(role="user", content=input_text))
        self.add_message(Message(role="assistant", content=response))


__all__ = ["DEFAULT_REACT_PROMPT", "ReActAgent"]
