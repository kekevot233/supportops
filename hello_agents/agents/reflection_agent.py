"""通过生成、反思和改写循环提升回答质量的 Agent。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from string import Formatter
from typing import Any, Literal

from ..core.agent import Agent
from ..core.config import Config
from ..core.context import BaseContextProvider
from ..core.exceptions import AgentError, ConfigurationError
from ..core.llm import HelloAgentsLLM
from ..core.message import Message
from ..core.request_budget import BaseRequestBudget


ReflectionStatus = Literal["PASS", "NEEDS_IMPROVEMENT"]
DEFAULT_REFLECTION_PROMPTS = {
    "initial": """请根据以下要求完成任务：

任务：{task}

请提供完整、准确的回答。""",
    "reflect": """请严格审查当前回答。

原始任务：
{task}

当前回答：
{content}

请指出具体问题和改进建议。响应的最后一行必须且只能是以下状态之一：
STATUS: PASS
STATUS: NEEDS_IMPROVEMENT""",
    "refine": """请根据反馈改进回答。

原始任务：
{task}

上一版回答：
{last_attempt}

反馈：
{feedback}

请只提供完整的改进后回答。""",
}

_STATUS_PATTERN = re.compile(r"STATUS:\s*(PASS|NEEDS_IMPROVEMENT)")


class ReflectionAgent(Agent):
    """对初始回答进行有限轮反思，并返回最后一个成功版本。"""

    MAX_ITERATIONS_LIMIT = 10
    _PROMPT_FIELDS = {
        "initial": {"task"},
        "reflect": {"task", "content"},
        "refine": {"task", "last_attempt", "feedback"},
    }

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: str | None = None,
        config: Config | None = None,
        *,
        max_iterations: int = 2,
        prompts: Mapping[str, str] | None = None,
        context_provider: BaseContextProvider | None = None,
        request_budget: BaseRequestBudget | None = None,
    ) -> None:
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or not 1 <= max_iterations <= self.MAX_ITERATIONS_LIMIT
        ):
            raise ConfigurationError(
                "max_iterations 必须是 1 到 "
                f"{self.MAX_ITERATIONS_LIMIT} 之间的整数。"
            )
        normalized_prompts = self._validate_prompts(
            DEFAULT_REFLECTION_PROMPTS if prompts is None else prompts
        )

        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            context_provider=context_provider,
            request_budget=request_budget,
        )
        self.max_iterations = max_iterations
        self.prompts = normalized_prompts

    def _execute(self, input_text: str, **kwargs: Any) -> str:
        current_answer = self._invoke_text(
            self.prompts["initial"].format(task=input_text),
            **kwargs,
        )

        for _ in range(self.max_iterations):
            feedback = self._invoke_text(
                self.prompts["reflect"].format(
                    task=input_text,
                    content=current_answer,
                ),
                **kwargs,
            )
            status = self._parse_status(feedback)
            if status == "PASS":
                break

            current_answer = self._invoke_text(
                self.prompts["refine"].format(
                    task=input_text,
                    last_attempt=current_answer,
                    feedback=feedback,
                ),
                **kwargs,
            )

        self._save_turn(input_text, current_answer)
        return current_answer

    def _invoke_text(self, prompt: str, **kwargs: Any) -> str:
        system_prompt = self.system_prompt or "你是一个严谨的回答与评审助手。"
        messages = self._build_system_messages(system_prompt)
        messages.extend(self._history_for_request())
        messages.append(Message(role="user", content=prompt))

        response = self._invoke_llm(messages, **kwargs)
        if not isinstance(response, str) or not response.strip():
            raise AgentError("Reflection 模型响应必须是非空字符串。")
        return response.strip()

    @staticmethod
    def _parse_status(feedback: str) -> ReflectionStatus:
        lines = feedback.rstrip().splitlines()
        if not lines:
            raise AgentError("Reflection 反馈不能为空。")
        matches = [
            _STATUS_PATTERN.fullmatch(line.strip())
            for line in lines
            if _STATUS_PATTERN.fullmatch(line.strip()) is not None
        ]
        match = _STATUS_PATTERN.fullmatch(lines[-1].strip())
        if match is None or len(matches) != 1:
            raise AgentError(
                "Reflection 反馈最后一行必须是 STATUS: PASS 或 "
                "STATUS: NEEDS_IMPROVEMENT。"
            )
        return match.group(1)

    @classmethod
    def _validate_prompts(
        cls,
        prompts: Mapping[str, str],
    ) -> dict[str, str]:
        if not isinstance(prompts, Mapping):
            raise ConfigurationError("prompts 必须是提示词映射。")
        if set(prompts) != set(cls._PROMPT_FIELDS):
            raise ConfigurationError("prompts 必须包含 initial、reflect、refine。")

        normalized: dict[str, str] = {}
        for name, expected_fields in cls._PROMPT_FIELDS.items():
            template = prompts[name]
            if not isinstance(template, str) or not template.strip():
                raise ConfigurationError(f"prompts['{name}'] 必须是非空字符串。")
            try:
                fields = {
                    field_name
                    for _, field_name, _, _ in Formatter().parse(template)
                    if field_name
                }
            except ValueError as error:
                raise ConfigurationError(
                    f"prompts['{name}'] 格式无效。"
                ) from error
            if fields != expected_fields:
                expected = ", ".join(sorted(expected_fields))
                raise ConfigurationError(
                    f"prompts['{name}'] 必须且只能包含这些字段：{expected}。"
                )
            normalized[name] = template
        return normalized

    def _save_turn(self, input_text: str, response: str) -> None:
        self.add_message(Message(role="user", content=input_text))
        self.add_message(Message(role="assistant", content=response))


__all__ = [
    "DEFAULT_REFLECTION_PROMPTS",
    "ReflectionAgent",
    "ReflectionStatus",
]
