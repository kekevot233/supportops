"""HelloAgents 工具的注册、发现与统一执行。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from ..core.exceptions import ToolError
from .base import JsonType, Tool, ToolParameter


ToolFunction = Callable[[Any], str]


class _FunctionTool(Tool):
    """把接收单个参数的普通 Python 函数适配为 Tool。"""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        function: ToolFunction,
        parameter: ToolParameter,
    ) -> None:
        super().__init__(name=name, description=description)
        self._function = function
        self._parameter = parameter.model_copy(deep=True)

    def get_parameters(self) -> list[ToolParameter]:
        return [self._parameter.model_copy(deep=True)]

    def _run(self, parameters: dict[str, Any]) -> str:
        return self._function(parameters[self._parameter.name])


class ToolRegistry:
    """集中管理 Tool，并为 Agent 提供统一的发现和执行入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        """注册 Tool；同名工具只有显式允许时才能被替换。"""
        if not isinstance(tool, Tool):
            raise ToolError("只能注册 Tool 实例。")
        if tool.name in self._tools and not replace:
            raise ToolError(f"工具 '{tool.name}' 已经注册。")
        self._tools[tool.name] = tool

    def register_function(
        self,
        name: str,
        description: str,
        function: ToolFunction,
        *,
        parameter_name: str = "input",
        parameter_type: JsonType = "string",
        parameter_description: str = "传给工具函数的输入",
        replace: bool = False,
    ) -> None:
        """把接收一个参数、返回字符串的普通函数包装并注册为 Tool。"""
        if not callable(function):
            raise ToolError("function 必须是可调用对象。")

        try:
            parameter = ToolParameter(
                name=parameter_name,
                type=parameter_type,
                description=parameter_description,
            )
        except ValidationError as error:
            raise ToolError("函数工具的参数定义无效。") from error
        tool = _FunctionTool(
            name=name,
            description=description,
            function=function,
            parameter=parameter,
        )
        self.register_tool(tool, replace=replace)

    def get_tool(self, name: str) -> Tool | None:
        """按名称查找工具；不存在时返回 None。"""
        return self._tools.get(name)

    def require_tool(self, name: str) -> Tool:
        """按名称取得工具；不存在时抛出框架异常。"""
        tool = self.get_tool(name)
        if tool is None:
            raise ToolError(f"工具 '{name}' 未注册。")
        return tool

    def unregister_tool(self, name: str) -> Tool:
        """移除并返回指定工具。"""
        tool = self.require_tool(name)
        del self._tools[name]
        return tool

    def clear(self) -> None:
        """移除注册表中的全部工具。"""
        self._tools.clear()

    def list_tools(self) -> list[Tool]:
        """按注册顺序返回工具列表的浅拷贝。"""
        return list(self._tools.values())

    def execute_tool(
        self,
        name: str,
        parameters: Any,
    ) -> str:
        """查找并执行工具；单参数工具允许直接传入原始值。"""
        tool = self.require_tool(name)
        normalized_parameters = self._normalize_parameters(tool, parameters)
        return tool.run(normalized_parameters)

    def get_tools_description(self) -> str:
        """生成可直接放入提示词的工具名称和描述。"""
        if not self._tools:
            return "暂无可用工具"
        return "\n".join(
            f"- {tool.name}: {tool.description}"
            for tool in self._tools.values()
        )

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        """按注册顺序生成全部工具的 Function Calling Schema。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    @staticmethod
    def _normalize_parameters(
        tool: Tool,
        parameters: Any,
    ) -> Mapping[str, Any]:
        if isinstance(parameters, Mapping):
            return parameters

        definitions = tool.get_parameters()
        if len(definitions) != 1 or not isinstance(definitions[0], ToolParameter):
            raise ToolError(
                f"工具 '{tool.name}' 不能使用单个原始值调用，请传入参数映射。"
            )
        return {definitions[0].name: parameters}

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


__all__ = ["ToolFunction", "ToolRegistry"]
