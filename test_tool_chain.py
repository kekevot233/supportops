import unittest

from hello_agents import (
    Tool,
    ToolChain,
    ToolChainManager,
    ToolError,
    ToolParameter,
    ToolRegistry,
)


class AddTool(Tool):
    def __init__(self):
        super().__init__(name="add", description="计算两个整数之和")

    def get_parameters(self):
        return [
            ToolParameter(name="left", type="integer", description="左值"),
            ToolParameter(name="right", type="integer", description="右值"),
        ]

    def _run(self, parameters):
        return str(parameters["left"] + parameters["right"])


def build_registry(executions=None):
    executions = executions if executions is not None else []
    registry = ToolRegistry()

    def uppercase(value):
        executions.append(("uppercase", value))
        return value.upper()

    def decorate(value):
        executions.append(("decorate", value))
        return f"<{value}>"

    registry.register_function(
        "uppercase",
        "转换为大写",
        uppercase,
        parameter_name="text",
    )
    registry.register_function(
        "decorate",
        "添加尖括号",
        decorate,
        parameter_name="text",
    )
    registry.register_tool(AddTool())
    return registry


class TestToolChain(unittest.TestCase):
    def test_executes_steps_and_exposes_isolated_context(self):
        executions = []
        registry = build_registry(executions)
        chain = ToolChain("format_text", "转换并装饰文本")
        chain.add_step(
            "uppercase",
            {"text": "{input}"},
            output_key="upper",
        )
        chain.add_step(
            "decorate",
            {"text": "结果={upper}; 标签={label}"},
            output_key="decorated",
        )
        supplied_context = {"label": "完成"}

        result = chain.execute_with_context(
            registry,
            "hello",
            supplied_context,
        )

        self.assertEqual(result.final_output, "<结果=HELLO; 标签=完成>")
        self.assertEqual(
            executions,
            [
                ("uppercase", "hello"),
                ("decorate", "结果=HELLO; 标签=完成"),
            ],
        )
        self.assertEqual(result.context["upper"], "HELLO")
        self.assertEqual(result.context["decorated"], result.final_output)
        self.assertEqual(supplied_context, {"label": "完成"})

    def test_exact_placeholder_preserves_non_string_value(self):
        registry = build_registry()
        chain = ToolChain("integer_chain", "保留整数参数")
        chain.add_step(
            "add",
            {"left": "{input}", "right": "{increment}"},
            output_key="sum",
        )

        result = chain.execute(
            registry,
            2,
            {"increment": 3},
        )

        self.assertEqual(result, "5")

    def test_default_output_keys_and_step_copies(self):
        registry = build_registry()
        chain = ToolChain("defaults", "测试默认输出键")
        template = {"text": "{input}"}
        chain.add_step("uppercase", template)
        template["text"] = "被修改"
        chain.add_step("decorate", {"text": "{step_1_result}"})

        steps = chain.list_steps()
        steps[0].parameter_template["text"] = "外部修改"

        self.assertEqual(chain.execute(registry, "hello"), "<HELLO>")
        self.assertEqual(
            [step.output_key for step in chain.list_steps()],
            ["step_1_result", "step_2_result"],
        )

    def test_preflight_missing_variable_or_tool_avoids_partial_execution(self):
        executions = []
        registry = build_registry(executions)

        missing_variable = ToolChain("missing_variable", "缺少变量")
        missing_variable.add_step("uppercase", {"text": "{input}"})
        missing_variable.add_step("decorate", {"text": "{unknown}"})
        with self.assertRaisesRegex(ToolError, "缺少模板变量"):
            missing_variable.execute(registry, "hello")
        self.assertEqual(executions, [])

        missing_tool = ToolChain("missing_tool", "缺少工具")
        missing_tool.add_step("uppercase", {"text": "{input}"})
        missing_tool.add_step("not_registered", {"text": "{step_1_result}"})
        with self.assertRaisesRegex(ToolError, "未注册"):
            missing_tool.execute(registry, "hello")
        self.assertEqual(executions, [])

    def test_rejects_unsafe_templates_and_context_collisions(self):
        registry = build_registry()
        chain = ToolChain("safe_chain", "安全模板")

        unsafe_templates = (
            "{input.__class__}",
            "{input[value]}",
            "{input!r}",
            "{input:>10}",
            {1: "value"},
            object(),
        )
        for template in unsafe_templates:
            with self.subTest(template=template):
                with self.assertRaises(ToolError):
                    chain.add_step("uppercase", template)

        chain.add_step("uppercase", {"text": "{input}"}, output_key="upper")
        with self.assertRaises(ToolError):
            chain.execute(registry, "hello", {"input": "override"})
        with self.assertRaises(ToolError):
            chain.execute(registry, "hello", {"upper": "collision"})

    def test_rejects_invalid_metadata_empty_chain_and_duplicate_outputs(self):
        invalid_metadata = (
            ("bad-name", "描述"),
            ("valid", ""),
        )
        for name, description in invalid_metadata:
            with self.subTest(name=name):
                with self.assertRaises(ToolError):
                    ToolChain(name, description)

        empty_chain = ToolChain("empty", "空链")
        with self.assertRaises(ToolError):
            empty_chain.execute(build_registry(), "input")

        chain = ToolChain("duplicates", "重复输出")
        chain.add_step("uppercase", "{input}", output_key="result")
        with self.assertRaisesRegex(ToolError, "已经存在"):
            chain.add_step("decorate", "{result}", output_key="result")
        with self.assertRaises(ToolError):
            chain.add_step("decorate", "{result}", output_key="input")

    def test_manager_register_replace_execute_and_unregister(self):
        registry = build_registry()
        manager = ToolChainManager(registry)
        first = ToolChain("format", "第一条链")
        first.add_step("uppercase", {"text": "{input}"})
        replacement = ToolChain("format", "替换链")
        replacement.add_step("decorate", {"text": "{input}"})

        manager.register_chain(first)
        with self.assertRaises(ToolError):
            manager.register_chain(replacement)

        manager.register_chain(replacement, replace=True)
        self.assertEqual(manager.execute_chain("format", "hello"), "<hello>")
        self.assertEqual(manager.list_chains(), [replacement])
        self.assertEqual(len(manager), 1)
        self.assertIs(manager.unregister_chain("format"), replacement)
        with self.assertRaises(ToolError):
            manager.require_chain("format")

    def test_manager_and_chain_type_validation(self):
        with self.assertRaises(ToolError):
            ToolChainManager("invalid")

        manager = ToolChainManager(build_registry())
        with self.assertRaises(ToolError):
            manager.register_chain("invalid")

        chain = ToolChain("valid", "有效链")
        chain.add_step("uppercase", {"text": "{input}"})
        with self.assertRaises(ToolError):
            chain.execute("invalid", "hello")
        with self.assertRaises(ToolError):
            chain.execute(build_registry(), "hello", "invalid")


if __name__ == "__main__":
    unittest.main()
