"""基于 AST 白名单的安全数学计算工具。"""

from __future__ import annotations

import ast
import math
import operator
from numbers import Real
from typing import Any

from ...core.exceptions import ToolError
from ..base import Tool, ToolParameter


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}


class CalculatorTool(Tool):
    """计算受限的数学表达式，不执行任意 Python 代码。"""

    MAX_EXPRESSION_LENGTH = 200
    MAX_AST_NODES = 100
    MAX_AST_DEPTH = 20
    MAX_ABS_EXPONENT = 100
    MAX_ABS_RESULT = 10**100
    MAX_ABS_ROUND_DIGITS = 100

    def __init__(self) -> None:
        super().__init__(
            name="calculator",
            description=(
                "安全计算数学表达式，支持 +、-、*、/、//、%、**、括号、"
                "sqrt、abs、round、pi 和 e"
            ),
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="expression",
                type="string",
                description="需要计算的数学表达式",
            )
        ]

    def _run(self, parameters: dict[str, Any]) -> str:
        expression = parameters["expression"].strip()
        if not expression:
            raise ToolError("计算表达式不能为空。")
        if len(expression) > self.MAX_EXPRESSION_LENGTH:
            raise ToolError(
                f"计算表达式不能超过 {self.MAX_EXPRESSION_LENGTH} 个字符。"
            )

        normalized_expression = (
            expression.replace("×", "*").replace("÷", "/").replace("^", "**")
        )

        try:
            syntax_tree = ast.parse(normalized_expression, mode="eval")
        except SyntaxError as error:
            raise ToolError("无效的数学表达式。") from error

        if sum(1 for _ in ast.walk(syntax_tree)) > self.MAX_AST_NODES:
            raise ToolError("计算表达式包含过多节点。")

        try:
            result = self._evaluate(syntax_tree, depth=0)
        except ToolError:
            raise
        except ZeroDivisionError as error:
            raise ToolError("计算表达式不能除以零。") from error
        except (OverflowError, ValueError) as error:
            raise ToolError("数学表达式无法计算。") from error

        return f"计算结果：{result}"

    def _evaluate(self, node: ast.AST, *, depth: int) -> int | float:
        if depth > self.MAX_AST_DEPTH:
            raise ToolError("计算表达式嵌套过深。")

        if isinstance(node, ast.Expression):
            return self._evaluate(node.body, depth=depth + 1)

        if isinstance(node, ast.Constant):
            if type(node.value) not in {int, float}:
                raise ToolError("计算表达式中只能使用数字。")
            return self._validate_number(node.value)

        if isinstance(node, ast.Name):
            if node.id not in _CONSTANTS:
                raise ToolError(f"不支持数学常量 '{node.id}'。")
            return _CONSTANTS[node.id]

        if isinstance(node, ast.UnaryOp):
            operation = _UNARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ToolError("计算表达式包含不支持的一元运算符。")
            operand = self._evaluate(node.operand, depth=depth + 1)
            return self._validate_number(operation(operand))

        if isinstance(node, ast.BinOp):
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ToolError("计算表达式包含不支持的二元运算符。")

            left = self._evaluate(node.left, depth=depth + 1)
            right = self._evaluate(node.right, depth=depth + 1)
            if isinstance(node.op, ast.Pow) and abs(right) > self.MAX_ABS_EXPONENT:
                raise ToolError(
                    f"幂运算指数绝对值不能超过 {self.MAX_ABS_EXPONENT}。"
                )
            return self._validate_number(operation(left, right))

        if isinstance(node, ast.Call):
            return self._evaluate_call(node, depth=depth)

        raise ToolError("计算表达式包含不允许的 Python 语法。")

    def _evaluate_call(self, node: ast.Call, *, depth: int) -> int | float:
        if not isinstance(node.func, ast.Name):
            raise ToolError("数学函数必须通过函数名直接调用。")
        if node.keywords:
            raise ToolError("数学函数不支持关键字参数。")

        function_name = node.func.id
        if function_name not in {"sqrt", "abs", "round"}:
            raise ToolError(f"不支持数学函数 '{function_name}'。")

        arguments = [
            self._evaluate(argument, depth=depth + 1)
            for argument in node.args
        ]

        if function_name == "sqrt":
            if len(arguments) != 1:
                raise ToolError("sqrt 只接受一个参数。")
            result = math.sqrt(arguments[0])
        elif function_name == "abs":
            if len(arguments) != 1:
                raise ToolError("abs 只接受一个参数。")
            result = abs(arguments[0])
        elif function_name == "round":
            if len(arguments) not in {1, 2}:
                raise ToolError("round 只接受一个或两个参数。")
            if len(arguments) == 2:
                digits = arguments[1]
                if not isinstance(digits, int) or isinstance(digits, bool):
                    raise ToolError("round 的第二个参数必须是整数。")
                if abs(digits) > self.MAX_ABS_ROUND_DIGITS:
                    raise ToolError(
                        "round 的小数位数绝对值不能超过 "
                        f"{self.MAX_ABS_ROUND_DIGITS}。"
                    )
                result = round(arguments[0], digits)
            else:
                result = round(arguments[0])
        return self._validate_number(result)

    def _validate_number(self, value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ToolError("计算结果必须是实数。")
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolError("计算结果必须是有限数值。")
        if abs(value) > self.MAX_ABS_RESULT:
            raise ToolError("计算结果绝对值过大。")
        return value


__all__ = ["CalculatorTool"]
