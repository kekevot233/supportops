"""确定性顺序执行多个已注册工具的工具链。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from string import Formatter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.exceptions import ToolError
from .registry import ToolRegistry


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXACT_FIELD_PATTERN = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class ToolChainStep:
    """工具链中的一个不可变步骤定义。"""

    tool_name: str
    parameter_template: Any
    output_key: str


class ToolChainExecution(BaseModel):
    """包含最终输出和完整上下文的工具链执行结果。"""

    model_config = ConfigDict(extra="forbid")

    final_output: str
    context: dict[str, Any] = Field(default_factory=dict)


class ToolChain:
    """按注册顺序执行工具，并允许后续步骤引用先前输出。"""

    MAX_STEPS = 50

    def __init__(self, name: str, description: str) -> None:
        self.name = self._validate_identifier(name, "工具链名称")
        if not isinstance(description, str) or not description.strip():
            raise ToolError("工具链描述不能为空。")
        self.description = description.strip()
        self._steps: list[ToolChainStep] = []

    def add_step(
        self,
        tool_name: str,
        parameter_template: Any,
        *,
        output_key: str | None = None,
    ) -> None:
        """添加步骤；模板中的占位符只能是简单标识符。"""
        if len(self._steps) >= self.MAX_STEPS:
            raise ToolError(f"工具链最多包含 {self.MAX_STEPS} 个步骤。")
        normalized_tool_name = self._validate_identifier(tool_name, "工具名称")
        normalized_output_key = self._validate_identifier(
            output_key or f"step_{len(self._steps) + 1}_result",
            "输出键",
        )
        if normalized_output_key == "input":
            raise ToolError("输出键不能使用保留名称 'input'。")
        if normalized_output_key in {
            step.output_key for step in self._steps
        }:
            raise ToolError(f"输出键 '{normalized_output_key}' 已经存在。")

        self._collect_template_fields(parameter_template)
        self._steps.append(
            ToolChainStep(
                tool_name=normalized_tool_name,
                parameter_template=deepcopy(parameter_template),
                output_key=normalized_output_key,
            )
        )

    def list_steps(self) -> list[ToolChainStep]:
        """返回步骤定义的深拷贝，避免外部修改链路。"""
        return deepcopy(self._steps)

    def execute(
        self,
        registry: ToolRegistry,
        initial_input: Any,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        """执行工具链并只返回最后一步输出。"""
        return self.execute_with_context(
            registry,
            initial_input,
            context,
        ).final_output

    def execute_with_context(
        self,
        registry: ToolRegistry,
        initial_input: Any,
        context: Mapping[str, Any] | None = None,
    ) -> ToolChainExecution:
        """执行工具链，并返回最终输出与隔离后的上下文。"""
        if not isinstance(registry, ToolRegistry):
            raise ToolError("registry 必须是 ToolRegistry。")
        if not self._steps:
            raise ToolError(f"工具链 '{self.name}' 没有可执行步骤。")

        execution_context = self._prepare_context(initial_input, context)
        self._preflight(registry, execution_context)

        final_output = ""
        for step in self._steps:
            parameters = self._render_template(
                step.parameter_template,
                execution_context,
            )
            final_output = registry.execute_tool(step.tool_name, parameters)
            execution_context[step.output_key] = final_output

        return ToolChainExecution(
            final_output=final_output,
            context=deepcopy(execution_context),
        )

    def _prepare_context(
        self,
        initial_input: Any,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if context is None:
            execution_context: dict[str, Any] = {}
        elif isinstance(context, Mapping):
            execution_context = deepcopy(dict(context))
        else:
            raise ToolError("工具链 context 必须是映射或 None。")

        invalid_keys = [
            key
            for key in execution_context
            if not isinstance(key, str) or not _IDENTIFIER_PATTERN.fullmatch(key)
        ]
        if invalid_keys:
            raise ToolError("工具链 context 的键必须是合法标识符字符串。")
        if "input" in execution_context:
            raise ToolError("工具链 context 不能覆盖保留键 'input'。")

        output_keys = {step.output_key for step in self._steps}
        collisions = sorted(output_keys.intersection(execution_context))
        if collisions:
            raise ToolError(
                "工具链 context 不能预先包含输出键："
                f"{', '.join(collisions)}。"
            )
        execution_context["input"] = deepcopy(initial_input)
        return execution_context

    def _preflight(
        self,
        registry: ToolRegistry,
        context: Mapping[str, Any],
    ) -> None:
        available_fields = set(context)
        for index, step in enumerate(self._steps, start=1):
            registry.require_tool(step.tool_name)
            required_fields = self._collect_template_fields(
                step.parameter_template
            )
            missing = sorted(required_fields - available_fields)
            if missing:
                raise ToolError(
                    f"工具链第 {index} 步缺少模板变量：{', '.join(missing)}。"
                )
            available_fields.add(step.output_key)

    @classmethod
    def _collect_template_fields(cls, template: Any) -> set[str]:
        if isinstance(template, str):
            fields: set[str] = set()
            try:
                parsed_parts = Formatter().parse(template)
                for _, field_name, format_spec, conversion in parsed_parts:
                    if field_name is None:
                        continue
                    if (
                        not _IDENTIFIER_PATTERN.fullmatch(field_name)
                        or format_spec
                        or conversion
                    ):
                        raise ToolError(
                            "工具链模板只允许简单标识符占位符。"
                        )
                    fields.add(field_name)
            except ValueError as error:
                raise ToolError("工具链模板格式无效。") from error
            return fields

        if isinstance(template, Mapping):
            fields: set[str] = set()
            for key, value in template.items():
                if not isinstance(key, str):
                    raise ToolError("工具链参数模板的键必须是字符串。")
                fields.update(cls._collect_template_fields(value))
            return fields

        if isinstance(template, (list, tuple)):
            fields: set[str] = set()
            for item in template:
                fields.update(cls._collect_template_fields(item))
            return fields

        if template is None or isinstance(template, (bool, int, float)):
            return set()
        raise ToolError("工具链参数模板包含不支持的值类型。")

    @classmethod
    def _render_template(
        cls,
        template: Any,
        context: Mapping[str, Any],
    ) -> Any:
        if isinstance(template, str):
            exact_match = _EXACT_FIELD_PATTERN.fullmatch(template)
            if exact_match is not None:
                return deepcopy(context[exact_match.group(1)])
            try:
                return template.format_map(context)
            except KeyError as error:
                raise ToolError("工具链模板变量在执行时缺失。") from error
        if isinstance(template, Mapping):
            return {
                key: cls._render_template(value, context)
                for key, value in template.items()
            }
        if isinstance(template, list):
            return [cls._render_template(item, context) for item in template]
        if isinstance(template, tuple):
            return [cls._render_template(item, context) for item in template]
        return deepcopy(template)

    @staticmethod
    def _validate_identifier(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(
            value.strip()
        ):
            raise ToolError(f"{label}必须是合法标识符。")
        return value.strip()

    def __len__(self) -> int:
        return len(self._steps)


class ToolChainManager:
    """集中注册和执行 ToolChain。"""

    def __init__(self, registry: ToolRegistry) -> None:
        if not isinstance(registry, ToolRegistry):
            raise ToolError("registry 必须是 ToolRegistry。")
        self.registry = registry
        self._chains: dict[str, ToolChain] = {}

    def register_chain(
        self,
        chain: ToolChain,
        *,
        replace: bool = False,
    ) -> None:
        if not isinstance(chain, ToolChain):
            raise ToolError("只能注册 ToolChain 实例。")
        if chain.name in self._chains and not replace:
            raise ToolError(f"工具链 '{chain.name}' 已经注册。")
        self._chains[chain.name] = chain

    def require_chain(self, name: str) -> ToolChain:
        chain = self._chains.get(name)
        if chain is None:
            raise ToolError(f"工具链 '{name}' 未注册。")
        return chain

    def unregister_chain(self, name: str) -> ToolChain:
        chain = self.require_chain(name)
        del self._chains[name]
        return chain

    def list_chains(self) -> list[ToolChain]:
        return list(self._chains.values())

    def execute_chain(
        self,
        name: str,
        initial_input: Any,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        return self.require_chain(name).execute(
            self.registry,
            initial_input,
            context,
        )

    def __len__(self) -> int:
        return len(self._chains)


__all__ = [
    "ToolChain",
    "ToolChainExecution",
    "ToolChainManager",
    "ToolChainStep",
]
