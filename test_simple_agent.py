import unittest

from hello_agents import (
    AgentError,
    CalculatorTool,
    ConfigurationError,
    LLMError,
    LLMResponse,
    LLMToolCall,
    Message,
    SimpleAgent,
    ToolError,
    ToolRegistry,
)


class FakeLLM:
    provider = "fake-provider"

    def __init__(
        self,
        responses=None,
        stream_items=None,
        structured_responses=None,
    ):
        self.responses = list(responses or [])
        self.stream_items = list(stream_items or [])
        self.structured_responses = list(structured_responses or [])
        self.invoke_calls = []
        self.stream_calls = []
        self.structured_calls = []

    def invoke(self, messages, **kwargs):
        self.invoke_calls.append(
            {
                "messages": [message.model_copy(deep=True) for message in messages],
                "kwargs": kwargs,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def stream_invoke(self, messages, **kwargs):
        self.stream_calls.append(
            {
                "messages": [message.model_copy(deep=True) for message in messages],
                "kwargs": kwargs,
            }
        )
        for item in self.stream_items:
            if isinstance(item, Exception):
                raise item
            yield item

    def invoke_with_tools(self, messages, tools, **kwargs):
        self.structured_calls.append(
            {
                "messages": [
                    message.model_copy(deep=True)
                    if isinstance(message, Message)
                    else dict(message)
                    for message in messages
                ],
                "tools": tools,
                "kwargs": kwargs,
            }
        )
        response = self.structured_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestSimpleAgent(unittest.TestCase):
    def test_run_builds_context_forwards_options_and_saves_turn(self):
        llm = FakeLLM(responses=["你好，我是 Fake 助手。"])
        agent = SimpleAgent(
            name="基础助手",
            llm=llm,
            system_prompt="你是一个简洁的助手。",
        )

        result = agent.run("你好", temperature=0)

        self.assertEqual(result, "你好，我是 Fake 助手。")
        self.assertEqual(llm.invoke_calls[0]["kwargs"], {"temperature": 0})
        self.assertEqual(
            [
                message.to_dict()
                for message in llm.invoke_calls[0]["messages"]
            ],
            [
                {"role": "system", "content": "你是一个简洁的助手。"},
                {"role": "user", "content": "你好"},
            ],
        )
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，我是 Fake 助手。"},
            ],
        )

    def test_second_run_receives_previous_conversation(self):
        llm = FakeLLM(responses=["第一答", "第二答"])
        agent = SimpleAgent(name="基础助手", llm=llm)

        agent.run("第一问")
        agent.run("第二问")

        self.assertEqual(
            [message.to_dict() for message in llm.invoke_calls[1]["messages"]],
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "第二问"},
            ],
        )

    def test_failed_or_empty_normal_call_does_not_change_history(self):
        llm = FakeLLM(
            responses=[
                LLMError("Fake 模型失败"),
                "   ",
            ]
        )
        agent = SimpleAgent(name="基础助手", llm=llm)
        original = Message(role="user", content="已有历史")
        agent.add_message(original)

        with self.assertRaises(LLMError):
            agent.run("失败请求")
        self.assertEqual(agent.get_history(), [original])

        with self.assertRaises(AgentError):
            agent.run("空响应请求")
        self.assertEqual(agent.get_history(), [original])

    def test_stream_run_yields_chunks_and_saves_complete_response(self):
        llm = FakeLLM(stream_items=["你", "", "好"])
        agent = SimpleAgent(
            name="流式助手",
            llm=llm,
            system_prompt="请简短回答。",
        )

        chunks = list(agent.stream_run("打个招呼", temperature=0.1))

        self.assertEqual(chunks, ["你", "好"])
        self.assertEqual(llm.stream_calls[0]["kwargs"], {"temperature": 0.1})
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "打个招呼"},
                {"role": "assistant", "content": "你好"},
            ],
        )

    def test_stream_failure_does_not_save_partial_response(self):
        llm = FakeLLM(
            stream_items=["部分内容", LLMError("Fake 流式失败")]
        )
        agent = SimpleAgent(name="流式助手", llm=llm)

        with self.assertRaises(LLMError):
            list(agent.stream_run("测试"))

        self.assertEqual(agent.get_history(), [])

    def test_empty_or_invalid_stream_does_not_change_history(self):
        empty_llm = FakeLLM(stream_items=[])
        empty_agent = SimpleAgent(name="流式助手", llm=empty_llm)

        with self.assertRaises(AgentError):
            list(empty_agent.stream_run("测试"))
        self.assertEqual(empty_agent.get_history(), [])

        invalid_llm = FakeLLM(stream_items=[123])
        invalid_agent = SimpleAgent(name="流式助手", llm=invalid_llm)

        with self.assertRaises(AgentError):
            list(invalid_agent.stream_run("测试"))
        self.assertEqual(invalid_agent.get_history(), [])

    def test_stream_run_rejects_empty_input_before_llm_call(self):
        llm = FakeLLM(stream_items=["不会调用"])
        agent = SimpleAgent(name="流式助手", llm=llm)

        with self.assertRaises(AgentError):
            list(agent.stream_run("   "))

        self.assertEqual(llm.stream_calls, [])

    def test_tool_call_executes_calculator_then_returns_final_answer(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            responses=[
                '[TOOL_CALL:calculator:{"expression":"2 + 3 * 4"}]',
                "计算结果是 14。",
            ]
        )
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )

        result = agent.run("帮我计算 2 + 3 * 4", temperature=0)

        self.assertEqual(result, "计算结果是 14。")
        self.assertEqual(len(llm.invoke_calls), 2)
        self.assertEqual(
            llm.invoke_calls[1]["messages"][-1].to_dict(),
            {
                "role": "user",
                "content": "[OBSERVATION:calculator]\n计算结果：14",
            },
        )
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "帮我计算 2 + 3 * 4"},
                {"role": "assistant", "content": "计算结果是 14。"},
            ],
        )

    def test_tool_loop_supports_multiple_calls_with_temporary_context(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        first_call = '[TOOL_CALL:calculator:{"expression":"2 + 3"}]'
        second_call = '[TOOL_CALL:calculator:{"expression":"5 * 4"}]'
        llm = FakeLLM(responses=[first_call, second_call, "最终结果是 20。"])
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )

        result = agent.run("先加再乘")

        self.assertEqual(result, "最终结果是 20。")
        self.assertEqual(len(llm.invoke_calls), 3)
        self.assertEqual(
            [
                message.to_dict()
                for message in llm.invoke_calls[2]["messages"][-4:]
            ],
            [
                {"role": "assistant", "content": first_call},
                {
                    "role": "user",
                    "content": "[OBSERVATION:calculator]\n计算结果：5",
                },
                {"role": "assistant", "content": second_call},
                {
                    "role": "user",
                    "content": "[OBSERVATION:calculator]\n计算结果：20",
                },
            ],
        )

    def test_tool_instructions_include_description_schema_and_protocol(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(responses=["无需调用工具。"])
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            system_prompt="请简洁回答。",
            tool_registry=registry,
        )

        agent.run("你好")

        system_message = llm.invoke_calls[0]["messages"][0]
        self.assertEqual(system_message.role, "system")
        self.assertIn("请简洁回答。", system_message.content)
        self.assertIn("- calculator: 安全计算数学表达式", system_message.content)
        self.assertIn('"expression"', system_message.content)
        self.assertIn(
            '[TOOL_CALL:<工具名>:<JSON对象>]',
            system_message.content,
        )

    def test_empty_registry_keeps_original_single_call_path(self):
        registry = ToolRegistry()
        llm = FakeLLM(responses=["普通回答"])
        agent = SimpleAgent(
            name="基础助手",
            llm=llm,
            system_prompt="原始提示词",
            tool_registry=registry,
        )

        result = agent.run("普通问题")

        self.assertEqual(result, "普通回答")
        self.assertEqual(len(llm.invoke_calls), 1)
        self.assertEqual(
            [message.to_dict() for message in llm.invoke_calls[0]["messages"]],
            [
                {"role": "system", "content": "原始提示词"},
                {"role": "user", "content": "普通问题"},
            ],
        )

    def test_unregistered_tool_fails_without_changing_history(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(responses=['[TOOL_CALL:missing:{"value":1}]'])
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )
        original = Message(role="assistant", content="已有历史")
        agent.add_message(original)

        with self.assertRaisesRegex(ToolError, "未注册"):
            agent.run("调用不存在的工具")

        self.assertEqual(agent.get_history(), [original])

    def test_invalid_tool_json_fails_without_second_llm_call(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            responses=['[TOOL_CALL:calculator:{"expression":"2 + 3",}]']
        )
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )

        with self.assertRaisesRegex(AgentError, "不是有效 JSON"):
            agent.run("计算")

        self.assertEqual(len(llm.invoke_calls), 1)
        self.assertEqual(agent.get_history(), [])

    def test_tool_execution_failure_does_not_reenter_llm_or_save_history(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            responses=['[TOOL_CALL:calculator:{"expression":"1 / 0"}]']
        )
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )

        with self.assertRaisesRegex(ToolError, "不能除以零"):
            agent.run("计算")

        self.assertEqual(len(llm.invoke_calls), 1)
        self.assertEqual(agent.get_history(), [])

    def test_llm_failure_after_observation_does_not_save_partial_turn(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            responses=[
                '[TOOL_CALL:calculator:{"expression":"6 * 7"}]',
                LLMError("Fake 后续模型失败"),
            ]
        )
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
        )
        original = Message(role="user", content="已有历史")
        agent.add_message(original)

        with self.assertRaises(LLMError):
            agent.run("计算")

        self.assertEqual(len(llm.invoke_calls), 2)
        self.assertEqual(agent.get_history(), [original])

    def test_max_tool_iterations_stops_before_extra_tool_execution(self):
        registry = ToolRegistry()
        executions = []

        def fake_echo(value):
            executions.append(value)
            return f"回显：{value}"

        registry.register_function(
            "echo",
            "回显输入",
            fake_echo,
            parameter_name="text",
        )
        llm = FakeLLM(
            responses=[
                '[TOOL_CALL:echo:{"text":"第一次"}]',
                '[TOOL_CALL:echo:{"text":"第二次"}]',
            ]
        )
        agent = SimpleAgent(
            name="工具助手",
            llm=llm,
            tool_registry=registry,
            max_tool_iterations=1,
        )

        with self.assertRaisesRegex(AgentError, "达到最大工具调用次数 1"):
            agent.run("不要无限循环")

        self.assertEqual(executions, ["第一次"])
        self.assertEqual(len(llm.invoke_calls), 2)
        self.assertEqual(agent.get_history(), [])

    def test_tool_configuration_rejects_invalid_registry_and_limits(self):
        llm = FakeLLM(responses=["不会调用"])

        with self.assertRaises(ConfigurationError):
            SimpleAgent(name="助手", llm=llm, tool_registry="invalid")

        for invalid_limit in (True, 0, -1, 21, 1.5):
            with self.subTest(invalid_limit=invalid_limit):
                with self.assertRaises(ConfigurationError):
                    SimpleAgent(
                        name="助手",
                        llm=llm,
                        max_tool_iterations=invalid_limit,
                    )

        for invalid_mode in ("prompt", "", None, []):
            with self.subTest(invalid_mode=invalid_mode):
                with self.assertRaises(ConfigurationError):
                    SimpleAgent(
                        name="助手",
                        llm=llm,
                        tool_call_mode=invalid_mode,
                    )

    def test_native_tool_call_executes_and_preserves_clean_history(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            structured_responses=[
                LLMResponse(
                    tool_calls=[
                        LLMToolCall(
                            id="call_1",
                            name="calculator",
                            arguments={"expression": "6 * 7"},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(content="答案是 42。", finish_reason="stop"),
            ]
        )
        agent = SimpleAgent(
            name="原生工具助手",
            llm=llm,
            system_prompt="请简洁回答。",
            tool_registry=registry,
            tool_call_mode="native",
        )

        result = agent.run("6 乘以 7 是多少？", temperature=0)

        self.assertEqual(result, "答案是 42。")
        self.assertEqual(len(llm.structured_calls), 2)
        self.assertEqual(
            llm.structured_calls[0]["messages"][0].to_dict(),
            {"role": "system", "content": "请简洁回答。"},
        )
        self.assertEqual(
            llm.structured_calls[0]["tools"][0]["function"]["name"],
            "calculator",
        )
        self.assertEqual(
            llm.structured_calls[1]["messages"][-2:],
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"6 * 7"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "计算结果：42",
                },
            ],
        )
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "6 乘以 7 是多少？"},
                {"role": "assistant", "content": "答案是 42。"},
            ],
        )

    def test_native_mode_supports_parallel_tool_calls(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            structured_responses=[
                LLMResponse(
                    tool_calls=[
                        LLMToolCall(
                            id="call_1",
                            name="calculator",
                            arguments={"expression": "2 + 3"},
                        ),
                        LLMToolCall(
                            id="call_2",
                            name="calculator",
                            arguments={"expression": "4 * 5"},
                        ),
                    ]
                ),
                LLMResponse(content="两个结果分别是 5 和 20。"),
            ]
        )
        agent = SimpleAgent(
            name="原生工具助手",
            llm=llm,
            tool_registry=registry,
            tool_call_mode="native",
        )

        result = agent.run("计算两个表达式")

        self.assertEqual(result, "两个结果分别是 5 和 20。")
        tool_messages = llm.structured_calls[1]["messages"][-2:]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["call_1", "call_2"],
        )
        self.assertEqual(
            [message["content"] for message in tool_messages],
            ["计算结果：5", "计算结果：20"],
        )

    def test_native_mode_checks_batch_limit_before_tool_execution(self):
        registry = ToolRegistry()
        executions = []

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
            structured_responses=[
                LLMResponse(
                    tool_calls=[
                        LLMToolCall(
                            id="call_1",
                            name="echo",
                            arguments={"text": "一"},
                        ),
                        LLMToolCall(
                            id="call_2",
                            name="echo",
                            arguments={"text": "二"},
                        ),
                    ]
                )
            ]
        )
        agent = SimpleAgent(
            name="原生工具助手",
            llm=llm,
            tool_registry=registry,
            tool_call_mode="native",
            max_tool_iterations=1,
        )

        with self.assertRaisesRegex(AgentError, "达到最大工具调用次数 1"):
            agent.run("测试上限")

        self.assertEqual(executions, [])
        self.assertEqual(agent.get_history(), [])

    def test_native_mode_failure_after_tool_does_not_save_history(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        llm = FakeLLM(
            structured_responses=[
                LLMResponse(
                    tool_calls=[
                        LLMToolCall(
                            id="call_1",
                            name="calculator",
                            arguments={"expression": "2 + 3"},
                        )
                    ]
                ),
                LLMError("Fake 原生后续调用失败"),
            ]
        )
        agent = SimpleAgent(
            name="原生工具助手",
            llm=llm,
            tool_registry=registry,
            tool_call_mode="native",
        )
        original = Message(role="assistant", content="已有历史")
        agent.add_message(original)

        with self.assertRaises(LLMError):
            agent.run("计算")

        self.assertEqual(agent.get_history(), [original])


if __name__ == "__main__":
    unittest.main()
