import unittest

from hello_agents import Tool, ToolError, ToolParameter, ToolRegistry


class EchoTool(Tool):
    def __init__(self, name="echo", prefix=""):
        super().__init__(name=name, description="回显文本")
        self.prefix = prefix

    def get_parameters(self):
        return [
            ToolParameter(
                name="text",
                type="string",
                description="需要回显的文本",
            )
        ]

    def _run(self, parameters):
        return self.prefix + parameters["text"]


class AddTool(Tool):
    def __init__(self):
        super().__init__(name="add", description="计算两个整数之和")

    def get_parameters(self):
        return [
            ToolParameter(name="left", type="integer", description="左操作数"),
            ToolParameter(name="right", type="integer", description="右操作数"),
        ]

    def _run(self, parameters):
        return str(parameters["left"] + parameters["right"])


class TestToolRegistry(unittest.TestCase):
    def test_register_find_list_and_describe_tools(self):
        registry = ToolRegistry()
        echo = EchoTool()
        add = AddTool()

        registry.register_tool(echo)
        registry.register_tool(add)

        self.assertEqual(len(registry), 2)
        self.assertIn("echo", registry)
        self.assertIs(registry.get_tool("echo"), echo)
        self.assertIsNone(registry.get_tool("missing"))
        self.assertEqual(registry.list_tools(), [echo, add])
        self.assertEqual(
            registry.get_tools_description(),
            "- echo: 回显文本\n- add: 计算两个整数之和",
        )

    def test_duplicate_requires_explicit_replace(self):
        registry = ToolRegistry()
        original = EchoTool(prefix="old:")
        replacement = EchoTool(prefix="new:")
        registry.register_tool(original)

        with self.assertRaises(ToolError):
            registry.register_tool(replacement)

        registry.register_tool(replacement, replace=True)

        self.assertIs(registry.get_tool("echo"), replacement)
        self.assertEqual(registry.execute_tool("echo", "value"), "new:value")

    def test_only_tool_instances_can_be_registered(self):
        registry = ToolRegistry()

        with self.assertRaises(ToolError):
            registry.register_tool("not-a-tool")

        self.assertEqual(len(registry), 0)

    def test_execute_tool_supports_mapping_and_single_raw_value(self):
        registry = ToolRegistry()
        registry.register_tool(EchoTool())
        registry.register_tool(AddTool())

        self.assertEqual(
            registry.execute_tool("add", {"left": 2, "right": 3}),
            "5",
        )
        self.assertEqual(registry.execute_tool("echo", "你好"), "你好")

        with self.assertRaises(ToolError):
            registry.execute_tool("add", 5)

    def test_register_function_wraps_and_executes_python_function(self):
        registry = ToolRegistry()
        received = []

        def fake_search(query):
            received.append(query)
            return f"搜索结果：{query}"

        registry.register_function(
            name="search",
            description="搜索资料",
            function=fake_search,
            parameter_name="query",
            parameter_description="搜索关键词",
        )

        self.assertEqual(
            registry.execute_tool("search", "Python Agent"),
            "搜索结果：Python Agent",
        )
        self.assertEqual(
            registry.execute_tool("search", {"query": "LangGraph"}),
            "搜索结果：LangGraph",
        )
        self.assertEqual(received, ["Python Agent", "LangGraph"])
        self.assertEqual(
            registry.require_tool("search").to_openai_schema()["function"][
                "parameters"
            ]["required"],
            ["query"],
        )

    def test_register_function_validates_callable_and_reuses_collision_rule(self):
        registry = ToolRegistry()
        registry.register_function("echo", "回显", lambda value: value)

        with self.assertRaises(ToolError):
            registry.register_function("echo", "另一个回显", lambda value: value)
        with self.assertRaises(ToolError):
            registry.register_function("invalid", "非法函数", "not-callable")
        with self.assertRaises(ToolError) as parameter_context:
            registry.register_function(
                "invalid_parameter",
                "非法参数",
                lambda value: value,
                parameter_name="bad-name",
            )
        self.assertEqual(
            str(parameter_context.exception),
            "函数工具的参数定义无效。",
        )

        registry.register_function(
            "echo",
            "替换后的回显",
            lambda value: f"new:{value}",
            replace=True,
        )
        self.assertEqual(registry.execute_tool("echo", "test"), "new:test")

    def test_unknown_unregister_and_clear_behaviour(self):
        registry = ToolRegistry()
        echo = EchoTool()
        registry.register_tool(echo)

        with self.assertRaises(ToolError):
            registry.execute_tool("missing", {})
        with self.assertRaises(ToolError):
            registry.unregister_tool("missing")

        self.assertIs(registry.unregister_tool("echo"), echo)
        self.assertEqual(registry.get_tools_description(), "暂无可用工具")

        registry.register_tool(EchoTool())
        registry.register_tool(AddTool())
        registry.clear()
        self.assertEqual(len(registry), 0)

    def test_to_openai_schemas_preserves_registration_order(self):
        registry = ToolRegistry()
        registry.register_tool(EchoTool())
        registry.register_tool(AddTool())

        schemas = registry.to_openai_schemas()

        self.assertEqual(
            [schema["function"]["name"] for schema in schemas],
            ["echo", "add"],
        )


if __name__ == "__main__":
    unittest.main()
