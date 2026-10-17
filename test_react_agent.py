import unittest

from hello_agents import (
    AgentError,
    CalculatorTool,
    ConfigurationError,
    LLMError,
    Message,
    ReActAgent,
    ToolError,
    ToolRegistry,
)


class FakeLLM:
    provider = "fake-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(
            {
                "messages": [message.model_copy(deep=True) for message in messages],
                "kwargs": kwargs,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def calculator_registry():
    registry = ToolRegistry()
    registry.register_tool(CalculatorTool())
    return registry


class TestReActAgent(unittest.TestCase):
    def test_direct_finish_saves_only_original_question_and_answer(self):
        llm = FakeLLM(
            ["Thought: 这个问题不需要工具。\nAction: Finish[你好！]"]
        )
        agent = ReActAgent(
            name="ReAct 助手",
            llm=llm,
            tool_registry=calculator_registry(),
        )

        result = agent.run("你好", temperature=0)

        self.assertEqual(result, "你好！")
        self.assertEqual(llm.calls[0]["kwargs"], {"temperature": 0})
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
            ],
        )

    def test_calculator_action_adds_observation_then_finishes(self):
        action = (
            "Thought: 需要先计算表达式。\n"
            'Action: calculator:{"expression":"2 + 3 * 4"}'
        )
        llm = FakeLLM(
            [
                action,
                "Thought: 已经得到结果。\nAction: Finish[计算结果是 14。]",
            ]
        )
        agent = ReActAgent(
            name="ReAct 助手",
            llm=llm,
            tool_registry=calculator_registry(),
        )

        result = agent.run("计算 2 + 3 * 4")

        self.assertEqual(result, "计算结果是 14。")
        self.assertEqual(len(llm.calls), 2)
        second_prompt = llm.calls[1]["messages"][-1].content
        self.assertIn(action, second_prompt)
        self.assertIn("Observation: 计算结果：14", second_prompt)
        first_prompt = llm.calls[0]["messages"][-1].content
        self.assertIn("- calculator: 安全计算数学表达式", first_prompt)
        self.assertIn('"expression"', first_prompt)
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "计算 2 + 3 * 4"},
                {"role": "assistant", "content": "计算结果是 14。"},
            ],
        )

    def test_multiple_actions_accumulate_scratchpad(self):
        llm = FakeLLM(
            [
                (
                    "Thought: 第一次计算。\n"
                    'Action: calculator:{"expression":"2 + 3"}'
                ),
                (
                    "Thought: 第二次计算。\n"
                    'Action: calculator:{"expression":"5 * 4"}'
                ),
                "Thought: 两步都已完成。\nAction: Finish[最终结果是 20。]",
            ]
        )
        agent = ReActAgent(
            name="ReAct 助手",
            llm=llm,
            tool_registry=calculator_registry(),
        )

        result = agent.run("先加再乘")

        self.assertEqual(result, "最终结果是 20。")
        final_prompt = llm.calls[2]["messages"][-1].content
        self.assertIn("Observation: 计算结果：5", final_prompt)
        self.assertIn("Observation: 计算结果：20", final_prompt)

    def test_existing_history_is_sent_but_scratchpad_is_not_persisted(self):
        llm = FakeLLM(
            ["Thought: 可以直接回答。\nAction: Finish[新回答]"]
        )
        agent = ReActAgent(
            name="ReAct 助手",
            llm=llm,
            tool_registry=calculator_registry(),
            system_prompt="自定义系统提示词",
        )
        agent.add_message(Message(role="user", content="旧问题"))
        agent.add_message(Message(role="assistant", content="旧回答"))

        agent.run("新问题")

        self.assertEqual(
            [message.to_dict() for message in llm.calls[0]["messages"][:-1]],
            [
                {"role": "system", "content": "自定义系统提示词"},
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
            ],
        )
        history_content = [
            message.content for message in agent.get_history()
        ]
        self.assertNotIn("Thought: 可以直接回答。", history_content)

    def test_invalid_model_protocol_does_not_change_history(self):
        invalid_responses = (
            "普通回答",
            "Thought: 只有思考",
            "Action: Finish[缺少思考]",
            "Thought: 空答案\nAction: Finish[]",
            "Thought: 错误格式\nAction: calculator[2+3]",
            "Thought: 非法 JSON\nAction: calculator:{invalid}",
            "Thought: 参数不是对象\nAction: calculator:[1,2]",
        )

        for response in invalid_responses:
            with self.subTest(response=response):
                llm = FakeLLM([response])
                agent = ReActAgent(
                    name="ReAct 助手",
                    llm=llm,
                    tool_registry=calculator_registry(),
                )
                original = Message(role="assistant", content="已有历史")
                agent.add_message(original)

                with self.assertRaises(AgentError):
                    agent.run("测试")

                self.assertEqual(agent.get_history(), [original])

    def test_unknown_or_failed_tool_does_not_change_history(self):
        cases = (
            (
                'Thought: 调用未知工具。\nAction: missing:{"value":1}',
                "未注册",
            ),
            (
                (
                    "Thought: 尝试除零。\n"
                    'Action: calculator:{"expression":"1 / 0"}'
                ),
                "不能除以零",
            ),
        )
        for response, error_message in cases:
            with self.subTest(response=response):
                agent = ReActAgent(
                    name="ReAct 助手",
                    llm=FakeLLM([response]),
                    tool_registry=calculator_registry(),
                )

                with self.assertRaisesRegex(ToolError, error_message):
                    agent.run("测试工具失败")

                self.assertEqual(agent.get_history(), [])

    def test_llm_failure_does_not_change_history(self):
        agent = ReActAgent(
            name="ReAct 助手",
            llm=FakeLLM([LLMError("Fake 模型失败")]),
            tool_registry=calculator_registry(),
        )

        with self.assertRaises(LLMError):
            agent.run("测试")

        self.assertEqual(agent.get_history(), [])

    def test_max_steps_stops_before_unusable_final_tool_action(self):
        executions = []
        registry = ToolRegistry()

        def fake_echo(value):
            executions.append(value)
            return value

        registry.register_function(
            "echo",
            "回显输入",
            fake_echo,
            parameter_name="text",
        )
        llm = FakeLLM(
            [
                'Thought: 第一步。\nAction: echo:{"text":"一"}',
                'Thought: 第二步。\nAction: echo:{"text":"二"}',
            ]
        )
        agent = ReActAgent(
            name="ReAct 助手",
            llm=llm,
            tool_registry=registry,
            max_steps=2,
        )

        with self.assertRaisesRegex(AgentError, "达到最大 ReAct 步数 2"):
            agent.run("不要无限循环")

        self.assertEqual(executions, ["一"])
        self.assertEqual(agent.get_history(), [])

    def test_configuration_validation(self):
        llm = FakeLLM([])
        registry = calculator_registry()

        with self.assertRaises(ConfigurationError):
            ReActAgent(name="助手", llm=llm, tool_registry="invalid")

        for invalid_steps in (True, 0, -1, 21, 1.5):
            with self.subTest(invalid_steps=invalid_steps):
                with self.assertRaises(ConfigurationError):
                    ReActAgent(
                        name="助手",
                        llm=llm,
                        tool_registry=registry,
                        max_steps=invalid_steps,
                    )

        invalid_templates = (
            "",
            "{question}",
            "{tools}{schemas}{question}{history}{unknown}",
            "{tools",
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                with self.assertRaises(ConfigurationError):
                    ReActAgent(
                        name="助手",
                        llm=llm,
                        tool_registry=registry,
                        prompt_template=template,
                    )


if __name__ == "__main__":
    unittest.main()
