import unittest

from pydantic import ValidationError

from hello_agents import Tool, ToolError, ToolParameter


class RepeatTool(Tool):
    def __init__(self):
        super().__init__(name="repeat_text", description="重复指定文本")

    def get_parameters(self):
        return [
            ToolParameter(
                name="text",
                type="string",
                description="需要重复的文本",
            ),
            ToolParameter(
                name="times",
                type="integer",
                description="重复次数",
                required=False,
                default=1,
            ),
            ToolParameter(
                name="uppercase",
                type="boolean",
                description="是否转换为大写",
                required=False,
                default=False,
            ),
        ]

    def _run(self, parameters):
        text = parameters["text"]
        if parameters["uppercase"]:
            text = text.upper()
        return text * parameters["times"]


class BrokenTool(Tool):
    def __init__(self, result=None, error=None):
        super().__init__(name="broken_tool", description="用于测试异常")
        self.result = result
        self.error = error

    def get_parameters(self):
        return []

    def _run(self, parameters):
        if self.error is not None:
            raise self.error
        return self.result


class InvalidDefinitionsTool(Tool):
    def __init__(
        self,
        definitions,
        name="invalid_definitions",
        description="非法参数定义",
    ):
        super().__init__(name=name, description=description)
        self.definitions = definitions

    def get_parameters(self):
        return self.definitions

    def _run(self, parameters):
        return "不会执行"


class TestToolBase(unittest.TestCase):
    def test_tool_is_abstract(self):
        with self.assertRaises(TypeError):
            Tool(name="abstract_tool", description="不能直接创建")

    def test_parameter_normalizes_text_and_validates_default(self):
        parameter = ToolParameter(
            name="  query  ",
            type="string",
            description="  搜索关键词  ",
            required=False,
            default="Python",
        )

        self.assertEqual(parameter.name, "query")
        self.assertEqual(parameter.description, "搜索关键词")
        self.assertEqual(parameter.default, "Python")

        invalid_values = (
            {"name": "1query", "type": "string", "description": "测试"},
            {"name": "query-name", "type": "string", "description": "测试"},
            {"name": "query", "type": "unknown", "description": "测试"},
            {"name": "query", "type": "integer", "description": "测试", "default": "1"},
            {"name": "query", "type": "number", "description": "测试", "default": True},
            {"name": "query", "type": "string", "description": "测试", "default": "Python"},
            {"name": "query", "type": "string", "description": "   "},
            {"name": "query", "type": "string", "description": "测试", "extra": 1},
        )
        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ToolParameter(**values)

    def test_run_applies_defaults_and_returns_text(self):
        tool = RepeatTool()

        self.assertEqual(tool.run({"text": "Hi"}), "Hi")
        self.assertEqual(
            tool.run({"text": "Hi", "times": 2, "uppercase": True}),
            "HIHI",
        )
        self.assertEqual(
            str(tool),
            "Tool(name=repeat_text, description=重复指定文本)",
        )

    def test_run_rejects_missing_unknown_and_wrong_type_parameters(self):
        tool = RepeatTool()
        invalid_parameters = (
            {},
            {"text": "Hi", "unknown": 1},
            {"text": "Hi", "times": "2"},
            {"text": "Hi", "times": True},
            {"text": None},
            "not-a-mapping",
        )

        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters):
                with self.assertRaises(ToolError):
                    tool.run(parameters)

    def test_run_wraps_unexpected_error_and_rejects_invalid_output(self):
        secret_error = RuntimeError("fake-sensitive-detail")
        failing_tool = BrokenTool(error=secret_error)

        with self.assertRaises(ToolError) as context:
            failing_tool.run({})

        self.assertEqual(str(context.exception), "工具 'broken_tool' 执行失败。")
        self.assertNotIn("fake-sensitive-detail", str(context.exception))
        self.assertIs(context.exception.__cause__, secret_error)

        for invalid_result in (None, 123, "   "):
            with self.subTest(invalid_result=invalid_result):
                with self.assertRaises(ToolError):
                    BrokenTool(result=invalid_result).run({})

    def test_invalid_tool_metadata_is_rejected(self):
        invalid_metadata = (
            {"name": "", "description": "描述"},
            {"name": "bad-name", "description": "描述"},
            {"name": "tool", "description": "   "},
        )

        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ToolError):
                    InvalidDefinitionsTool([], **metadata)

    def test_invalid_parameter_definitions_are_rejected(self):
        duplicate = ToolParameter(
            name="value",
            type="string",
            description="测试值",
        )
        invalid_definitions = (
            "not-a-sequence",
            ["not-a-parameter"],
            [duplicate, duplicate.model_copy()],
        )

        for definitions in invalid_definitions:
            with self.subTest(definitions=definitions):
                with self.assertRaises(ToolError):
                    InvalidDefinitionsTool(definitions).run({})

    def test_openai_schema_contains_parameters_and_constraints(self):
        tool = RepeatTool()

        self.assertEqual(
            tool.to_openai_schema(),
            {
                "type": "function",
                "function": {
                    "name": "repeat_text",
                    "description": "重复指定文本",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "需要重复的文本",
                            },
                            "times": {
                                "type": "integer",
                                "description": "重复次数（默认值：1）",
                            },
                            "uppercase": {
                                "type": "boolean",
                                "description": "是否转换为大写（默认值：False）",
                            },
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
