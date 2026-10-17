import math
import unittest

from hello_agents import CalculatorTool, ToolError, ToolRegistry


class TestCalculatorTool(unittest.TestCase):
    def setUp(self):
        self.calculator = CalculatorTool()

    def test_calculates_operator_precedence_and_common_symbols(self):
        cases = (
            ("2 + 3 * 4", "计算结果：14"),
            ("(2 + 3) * 4", "计算结果：20"),
            ("7 // 2", "计算结果：3"),
            ("7 % 2", "计算结果：1"),
            ("-5 + +2", "计算结果：-3"),
            ("2 ^ 3", "计算结果：8"),
            ("6 × 7", "计算结果：42"),
            ("8 ÷ 2", "计算结果：4.0"),
        )

        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(
                    self.calculator.run({"expression": expression}),
                    expected,
                )

    def test_supports_whitelisted_functions_and_constants(self):
        self.assertEqual(
            self.calculator.run(
                {"expression": "sqrt(16) + abs(-3) + round(2.6)"}
            ),
            "计算结果：10.0",
        )
        self.assertEqual(
            self.calculator.run({"expression": "round(pi, 3)"}),
            "计算结果：3.142",
        )
        self.assertAlmostEqual(
            float(
                self.calculator.run({"expression": "e"}).removeprefix(
                    "计算结果："
                )
            ),
            math.e,
        )

    def test_rejects_empty_invalid_and_zero_division_expressions(self):
        expressions = (
            "",
            "   ",
            "2 +",
            "1 / 0",
            "sqrt(-1)",
            "1e309",
            "(-1) ** 0.5",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ToolError):
                    self.calculator.run({"expression": expression})

    def test_rejects_python_code_and_non_whitelisted_syntax(self):
        expressions = (
            "__import__('os').system('echo unsafe')",
            "(1).__class__",
            "[1][0]",
            "[x for x in [1]]",
            "(lambda: 1)()",
            "open('file.txt')",
            "sum([1, 2])",
            "unknown + 1",
            "True + 1",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ToolError):
                    self.calculator.run({"expression": expression})

    def test_rejects_invalid_function_arguments(self):
        expressions = (
            "sqrt(1, 2)",
            "abs()",
            "round(1, 2, 3)",
            "round(1.25, 1.5)",
            "round(1.25, 101)",
            "round(1.25, ndigits=1)",
            "math.sqrt(4)",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ToolError):
                    self.calculator.run({"expression": expression})

    def test_enforces_expression_resource_limits(self):
        expressions = (
            "1" * (self.calculator.MAX_EXPRESSION_LENGTH + 1),
            "2 ** 101",
            "10 ** 100 * 10",
            "+" * (self.calculator.MAX_AST_DEPTH + 2) + "1",
            "+".join("1" for _ in range(60)),
        )

        for expression in expressions:
            with self.subTest(expression=expression[:30]):
                with self.assertRaises(ToolError):
                    self.calculator.run({"expression": expression})

    def test_can_be_called_through_registry_with_raw_or_mapping_input(self):
        registry = ToolRegistry()
        registry.register_tool(self.calculator)

        self.assertEqual(
            registry.execute_tool("calculator", "2 + 2"),
            "计算结果：4",
        )
        self.assertEqual(
            registry.execute_tool("calculator", {"expression": "3 * 3"}),
            "计算结果：9",
        )

    def test_schema_describes_expression_parameter(self):
        schema = self.calculator.to_openai_schema()

        self.assertEqual(schema["function"]["name"], "calculator")
        self.assertEqual(
            schema["function"]["parameters"]["required"],
            ["expression"],
        )
        self.assertEqual(
            schema["function"]["parameters"]["properties"]["expression"][
                "type"
            ],
            "string",
        )


if __name__ == "__main__":
    unittest.main()
