"""HelloAgents 工具系统的参数模型与抽象基类。"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from numbers import Real
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.exceptions import ToolError


JsonType = Literal["string", "number", "integer", "boolean", "array", "object"]
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _matches_json_type(value: Any, expected_type: JsonType) -> bool:
    """判断 Python 值是否符合声明的 JSON Schema 基础类型。"""
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return (
            isinstance(value, Real)
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    return isinstance(value, Mapping)


class ToolParameter(BaseModel):
    """描述一个工具参数的名称、类型、用途和默认行为。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: Annotated[str, Field(min_length=1)]
    type: JsonType
    description: Annotated[str, Field(min_length=1)]
    required: bool = True
    default: Any = None

    @field_validator("name", "description", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        """去除文本两端空格，让元数据保持规范。"""
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """参数名使用可安全放入 JSON Schema 的标识符格式。"""
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("参数名只能包含字母、数字和下划线，且不能以数字开头。")
        return value

    @model_validator(mode="after")
    def validate_default_type(self) -> "ToolParameter":
        """如果提供了非空默认值，它必须与声明类型一致。"""
        if self.required and self.default is not None:
            raise ValueError("带默认值的参数必须设置 required=False。")
        if self.default is not None and not _matches_json_type(
            self.default,
            self.type,
        ):
            raise ValueError("参数默认值与声明的 JSON 类型不一致。")
        return self


class Tool(ABC):
    """所有 HelloAgents 工具共同遵守的执行契约。"""

    def __init__(self, name: str, description: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ToolError("工具名称不能为空。")
        if not _IDENTIFIER_PATTERN.fullmatch(name.strip()):
            raise ToolError("工具名称只能包含字母、数字和下划线，且不能以数字开头。")
        if not isinstance(description, str) or not description.strip():
            raise ToolError("工具描述不能为空。")

        self.name = name.strip()
        self.description = description.strip()

    def run(self, parameters: Mapping[str, Any]) -> str:
        """统一校验参数、调用具体工具，并检查工具输出。"""
        validated_parameters = self._validate_parameters(parameters)

        try:
            result = self._run(validated_parameters)
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(f"工具 '{self.name}' 执行失败。") from error

        if not isinstance(result, str) or not result.strip():
            raise ToolError(f"工具 '{self.name}' 必须返回非空字符串。")
        return result

    @abstractmethod
    def _run(self, parameters: dict[str, Any]) -> str:
        """执行工具的具体业务逻辑，由子类实现。"""
        raise NotImplementedError

    @abstractmethod
    def get_parameters(self) -> list[ToolParameter]:
        """返回工具的参数定义，用于校验和生成调用 Schema。"""
        raise NotImplementedError

    def to_openai_schema(self) -> dict[str, Any]:
        """转换为 OpenAI-compatible function calling 工具 Schema。"""
        definitions = self._get_parameter_definitions()
        properties: dict[str, Any] = {}
        required: list[str] = []

        for parameter in definitions:
            description = parameter.description
            if parameter.default is not None:
                description += f"（默认值：{parameter.default}）"

            property_schema: dict[str, Any] = {
                "type": parameter.type,
                "description": description,
            }
            if parameter.type == "array":
                property_schema["items"] = {"type": "string"}

            properties[parameter.name] = property_schema
            if parameter.required:
                required.append(parameter.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def _validate_parameters(
        self,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        """拒绝未知参数，并为已声明参数完成必填、默认值和类型检查。"""
        if not isinstance(parameters, Mapping):
            raise ToolError(f"工具 '{self.name}' 的参数必须是映射。")

        definitions = self._get_parameter_definitions()
        definition_by_name = {
            parameter.name: parameter for parameter in definitions
        }
        unknown_names = sorted(set(parameters) - set(definition_by_name))
        if unknown_names:
            raise ToolError(
                f"工具 '{self.name}' 收到未知参数：{', '.join(unknown_names)}。"
            )

        validated: dict[str, Any] = {}
        missing: list[str] = []
        for parameter in definitions:
            if parameter.name in parameters:
                value = parameters[parameter.name]
            elif parameter.default is not None:
                value = deepcopy(parameter.default)
            elif parameter.required:
                missing.append(parameter.name)
                continue
            else:
                value = None

            if value is not None and not _matches_json_type(value, parameter.type):
                raise ToolError(
                    f"工具 '{self.name}' 的参数 '{parameter.name}' "
                    f"必须是 {parameter.type} 类型。"
                )
            if value is None and parameter.required:
                raise ToolError(
                    f"工具 '{self.name}' 的必填参数 '{parameter.name}' 不能是 None。"
                )
            validated[parameter.name] = value

        if missing:
            raise ToolError(
                f"工具 '{self.name}' 缺少必填参数：{', '.join(missing)}。"
            )
        return validated

    def _get_parameter_definitions(self) -> list[ToolParameter]:
        """检查子类返回的参数定义是否合法且不存在重名。"""
        definitions = self.get_parameters()
        if isinstance(definitions, (str, bytes)) or not isinstance(
            definitions,
            Sequence,
        ):
            raise ToolError(f"工具 '{self.name}' 的参数定义必须是序列。")

        result = list(definitions)
        if not all(isinstance(item, ToolParameter) for item in result):
            raise ToolError(
                f"工具 '{self.name}' 的参数定义必须全部是 ToolParameter。"
            )

        names = [parameter.name for parameter in result]
        duplicate_names = sorted(
            {name for name in names if names.count(name) > 1}
        )
        if duplicate_names:
            raise ToolError(
                f"工具 '{self.name}' 存在重复参数：{', '.join(duplicate_names)}。"
            )
        return result

    def __str__(self) -> str:
        return f"Tool(name={self.name}, description={self.description})"


__all__ = ["JsonType", "Tool", "ToolParameter"]
