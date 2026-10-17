"""先生成严格计划，再按顺序执行计划的 Agent。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from string import Formatter
from typing import Any

from ..core.agent import Agent
from ..core.config import Config
from ..core.context import BaseContextProvider
from ..core.exceptions import AgentError, ConfigurationError
from ..core.llm import HelloAgentsLLM
from ..core.message import Message
from ..core.request_budget import BaseRequestBudget


DEFAULT_PLAN_SOLVE_PROMPTS = {
    "planner": """请把问题分解为有序、独立、可执行的步骤。
最后一个步骤必须综合前面结果并直接回答原始问题。

问题：
{question}

只输出严格 JSON 字符串数组，不要添加代码块或解释。例如：
["步骤一", "步骤二", "综合并回答原始问题"]""",
    "executor": """请严格执行当前步骤，只输出该步骤的结果。

原始问题：
{question}

完整计划：
{plan}

已经完成的步骤与结果：
{history}

当前步骤：
{current_step}

如果这是最后一步，请综合已有结果并直接回答原始问题。""",
}


class PlanAndSolveAgent(Agent):
    """把规划和逐步执行组织成带安全边界的统一 Agent。"""

    MAX_PLAN_STEPS_LIMIT = 20
    _PROMPT_FIELDS = {
        "planner": {"question"},
        "executor": {"question", "plan", "history", "current_step"},
    }

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: str | None = None,
        config: Config | None = None,
        *,
        max_plan_steps: int = 8,
        prompts: Mapping[str, str] | None = None,
        context_provider: BaseContextProvider | None = None,
        request_budget: BaseRequestBudget | None = None,
    ) -> None:
        if (
            isinstance(max_plan_steps, bool)
            or not isinstance(max_plan_steps, int)
            or not 1 <= max_plan_steps <= self.MAX_PLAN_STEPS_LIMIT
        ):
            raise ConfigurationError(
                "max_plan_steps 必须是 1 到 "
                f"{self.MAX_PLAN_STEPS_LIMIT} 之间的整数。"
            )
        normalized_prompts = self._validate_prompts(
            DEFAULT_PLAN_SOLVE_PROMPTS if prompts is None else prompts
        )

        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            context_provider=context_provider,
            request_budget=request_budget,
        )
        self.max_plan_steps = max_plan_steps
        self.prompts = normalized_prompts

    def _execute(self, input_text: str, **kwargs: Any) -> str:
        raw_plan = self._invoke_text(
            self.prompts["planner"].format(question=input_text),
            phase="planner",
            **kwargs,
        )
        plan = self._parse_plan(raw_plan)

        results: list[tuple[str, str]] = []
        final_answer = ""
        formatted_plan = self._format_plan(plan)
        for current_step in plan:
            final_answer = self._invoke_text(
                self.prompts["executor"].format(
                    question=input_text,
                    plan=formatted_plan,
                    history=self._format_history(results),
                    current_step=current_step,
                ),
                phase="executor",
                **kwargs,
            )
            results.append((current_step, final_answer))

        self._save_turn(input_text, final_answer)
        return final_answer

    def _invoke_text(
        self,
        prompt: str,
        *,
        phase: str,
        **kwargs: Any,
    ) -> str:
        default_system_prompts = {
            "planner": "你是一个只输出严格 JSON 计划的任务规划专家。",
            "executor": "你是一个严格按计划逐步执行任务的助手。",
        }
        messages = self._build_system_messages(
            self.system_prompt or default_system_prompts[phase]
        )
        messages.extend(self._history_for_request())
        messages.append(Message(role="user", content=prompt))

        response = self._invoke_llm(messages, **kwargs)
        if not isinstance(response, str) or not response.strip():
            raise AgentError(f"PlanAndSolve 的 {phase} 响应必须是非空字符串。")
        return response.strip()

    def _parse_plan(self, raw_plan: str) -> list[str]:
        try:
            parsed = json.loads(raw_plan)
        except json.JSONDecodeError as error:
            raise AgentError("规划器必须返回严格 JSON 数组。") from error
        if not isinstance(parsed, list) or not parsed:
            raise AgentError("规划器必须返回非空 JSON 数组。")
        if len(parsed) > self.max_plan_steps:
            raise AgentError(
                f"计划步骤不能超过 {self.max_plan_steps} 个。"
            )

        plan: list[str] = []
        for index, step in enumerate(parsed, start=1):
            if not isinstance(step, str) or not step.strip():
                raise AgentError(f"计划的第 {index} 个步骤必须是非空字符串。")
            plan.append(step.strip())
        return plan

    @staticmethod
    def _format_plan(plan: list[str]) -> str:
        return "\n".join(
            f"{index}. {step}" for index, step in enumerate(plan, start=1)
        )

    @staticmethod
    def _format_history(results: list[tuple[str, str]]) -> str:
        if not results:
            return "暂无历史结果"
        lines: list[str] = []
        for index, (step, result) in enumerate(results, start=1):
            lines.extend(
                [
                    f"步骤 {index}：{step}",
                    f"结果：{result}",
                ]
            )
        return "\n".join(lines)

    @classmethod
    def _validate_prompts(
        cls,
        prompts: Mapping[str, str],
    ) -> dict[str, str]:
        if not isinstance(prompts, Mapping):
            raise ConfigurationError("prompts 必须是提示词映射。")
        if set(prompts) != set(cls._PROMPT_FIELDS):
            raise ConfigurationError("prompts 必须包含 planner 和 executor。")

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


__all__ = ["DEFAULT_PLAN_SOLVE_PROMPTS", "PlanAndSolveAgent"]
