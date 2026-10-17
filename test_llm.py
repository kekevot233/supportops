import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hello_agents import (
    CalculatorTool,
    Config,
    ConfigurationError,
    HelloAgentsLLM,
    LLMError,
    LLMResponse,
    LLMToolCall,
    Message,
    ToolRegistry,
)


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def make_fake_client(*, response=None, error=None):
    completions = FakeCompletions(response=response, error=error)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return client, completions


def make_response(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ]
    )


def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def make_tool_response(*, content=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ]
    )


def calculator_schemas():
    registry = ToolRegistry()
    registry.register_tool(CalculatorTool())
    return registry.to_openai_schemas()


def make_chunk(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
            )
        ]
    )


class TestHelloAgentsLLM(unittest.TestCase):
    def test_invoke_normalizes_messages_and_uses_configuration(self):
        client, completions = make_fake_client(response=make_response("Fake 回答"))
        config = Config(
            default_model="fake-model",
            temperature=0.25,
            max_tokens=128,
            request_timeout_seconds=12,
        )
        llm = HelloAgentsLLM(config=config, client=client, environ={})

        result = llm.invoke(
            [
                Message(role="system", content="你是测试助手"),
                {"role": "user", "content": "你好"},
            ],
            stop=["END"],
        )

        self.assertEqual(result, "Fake 回答")
        self.assertEqual(
            completions.calls,
            [
                {
                    "model": "fake-model",
                    "messages": [
                        {"role": "system", "content": "你是测试助手"},
                        {"role": "user", "content": "你好"},
                    ],
                    "temperature": 0.25,
                    "stream": False,
                    "max_tokens": 128,
                    "stop": ["END"],
                }
            ],
        )

    def test_stream_invoke_yields_only_non_empty_text(self):
        chunks = [
            make_chunk("你"),
            make_chunk(None),
            SimpleNamespace(choices=[]),
            make_chunk("好"),
        ]
        client, completions = make_fake_client(response=iter(chunks))
        llm = HelloAgentsLLM(
            model="fake-stream-model",
            client=client,
            environ={},
            temperature=0,
        )

        result = list(llm.stream_invoke([{"role": "user", "content": "测试"}]))

        self.assertEqual(result, ["你", "好"])
        self.assertTrue(completions.calls[0]["stream"])
        self.assertEqual(completions.calls[0]["temperature"], 0.0)

    def test_invoke_with_tools_returns_structured_text_response(self):
        response = make_tool_response(
            content="不需要调用工具。",
            tool_calls=None,
            finish_reason="stop",
        )
        client, completions = make_fake_client(response=response)
        llm = HelloAgentsLLM(
            model="fake-tool-model",
            client=client,
            environ={},
            temperature=0,
        )

        result = llm.invoke_with_tools(
            [Message(role="user", content="你好")],
            calculator_schemas(),
        )

        self.assertEqual(
            result,
            LLMResponse(content="不需要调用工具。", finish_reason="stop"),
        )
        self.assertEqual(completions.calls[0]["tool_choice"], "auto")
        self.assertEqual(
            completions.calls[0]["tools"][0]["function"]["name"],
            "calculator",
        )
        self.assertFalse(completions.calls[0]["stream"])

    def test_invoke_with_tools_parses_none_content_and_tool_call(self):
        response = make_tool_response(
            tool_calls=[
                make_tool_call(
                    "call_1",
                    "calculator",
                    '{"expression":"2 + 3"}',
                )
            ],
            finish_reason="tool_calls",
        )
        client, completions = make_fake_client(response=response)
        llm = HelloAgentsLLM(client=client, environ={})

        result = llm.invoke_with_tools(
            [{"role": "user", "content": "计算 2 + 3"}],
            calculator_schemas(),
            tool_choice={
                "type": "function",
                "function": {"name": "calculator"},
            },
        )

        self.assertIsNone(result.content)
        self.assertEqual(
            result.tool_calls,
            [
                LLMToolCall(
                    id="call_1",
                    name="calculator",
                    arguments={"expression": "2 + 3"},
                )
            ],
        )
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(
            completions.calls[0]["tool_choice"],
            {
                "type": "function",
                "function": {"name": "calculator"},
            },
        )
        self.assertEqual(
            result.to_assistant_message(),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"2 + 3"}',
                        },
                    }
                ],
            },
        )

    def test_invoke_with_tools_supports_multiple_calls_and_tool_messages(self):
        response = make_tool_response(
            content="继续计算",
            tool_calls=[
                make_tool_call("call_1", "calculator", '{"expression":"2+3"}'),
                make_tool_call("call_2", "calculator", '{"expression":"4*5"}'),
            ],
        )
        client, completions = make_fake_client(response=response)
        llm = HelloAgentsLLM(client=client, environ={})
        messages = [
            {"role": "user", "content": "计算两个表达式"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "previous_call",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"1+1"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "previous_call",
                "content": "计算结果：2",
            },
        ]

        result = llm.invoke_with_tools(messages, calculator_schemas())

        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(
            completions.calls[0]["messages"],
            messages,
        )

    def test_invoke_with_tools_rejects_invalid_arguments_and_empty_payload(self):
        invalid_responses = (
            (
                make_tool_response(
                    tool_calls=[
                        make_tool_call("call_1", "calculator", "{invalid}")
                    ]
                ),
                "不是有效 JSON",
            ),
            (
                make_tool_response(
                    tool_calls=[
                        make_tool_call("call_1", "calculator", "[1,2]")
                    ]
                ),
                "必须是 JSON 对象",
            ),
            (
                make_tool_response(
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="custom",
                            function=SimpleNamespace(
                                name="calculator",
                                arguments='{"expression":"2+3"}',
                            ),
                        )
                    ]
                ),
                "响应格式无效",
            ),
            (
                make_tool_response(
                    tool_calls=[
                        make_tool_call(
                            "duplicate",
                            "calculator",
                            '{"expression":"2+3"}',
                        ),
                        make_tool_call(
                            "duplicate",
                            "calculator",
                            '{"expression":"4+5"}',
                        ),
                    ]
                ),
                "重复的工具调用 ID",
            ),
            (make_tool_response(), "未包含文本或工具调用"),
        )

        for response, message in invalid_responses:
            with self.subTest(message=message):
                client, _ = make_fake_client(response=response)
                llm = HelloAgentsLLM(client=client, environ={})
                with self.assertRaisesRegex(LLMError, message):
                    llm.invoke_with_tools(
                        [{"role": "user", "content": "测试"}],
                        calculator_schemas(),
                    )

    def test_invoke_with_tools_validates_inputs_before_sdk_call(self):
        client, completions = make_fake_client(
            response=make_tool_response(content="不会使用")
        )
        llm = HelloAgentsLLM(client=client, environ={})

        invalid_cases = (
            ([], calculator_schemas(), "auto"),
            ([{"role": "user", "content": "测试"}], [], "auto"),
            (
                [{"role": "tool", "content": "缺少调用 ID"}],
                calculator_schemas(),
                "auto",
            ),
            (
                [{"role": "user", "content": "测试"}],
                [{"type": "function", "function": {"name": "bad-name"}}],
                "auto",
            ),
            (
                [{"role": "user", "content": "测试"}],
                calculator_schemas(),
                "invalid",
            ),
        )
        for messages, tools, tool_choice in invalid_cases:
            with self.subTest(tool_choice=tool_choice):
                with self.assertRaises(LLMError):
                    llm.invoke_with_tools(
                        messages,
                        tools,
                        tool_choice=tool_choice,
                    )

        self.assertEqual(completions.calls, [])

    def test_invoke_with_tools_wraps_sdk_error_without_leaking_details(self):
        client, _ = make_fake_client(
            error=RuntimeError("fake-native-secret")
        )
        llm = HelloAgentsLLM(client=client, environ={})

        with self.assertRaises(LLMError) as context:
            llm.invoke_with_tools(
                [{"role": "user", "content": "测试"}],
                calculator_schemas(),
            )

        self.assertEqual(str(context.exception), "大语言模型工具调用失败。")
        self.assertNotIn("fake-native-secret", str(context.exception))

    def test_invalid_messages_are_rejected_before_sdk_call(self):
        client, completions = make_fake_client(response=make_response("不会使用"))
        llm = HelloAgentsLLM(client=client, environ={})

        invalid_cases = (
            [],
            [{"role": "unknown", "content": "错误角色"}],
            [{"role": "user"}],
            [123],
        )
        for messages in invalid_cases:
            with self.subTest(messages=messages):
                with self.assertRaises(LLMError):
                    llm.invoke(messages)

        self.assertEqual(completions.calls, [])

    def test_sdk_error_is_wrapped_without_leaking_original_text(self):
        client, _ = make_fake_client(
            error=RuntimeError("fake-secret-must-not-appear")
        )
        llm = HelloAgentsLLM(client=client, environ={})

        with self.assertRaises(LLMError) as context:
            llm.invoke([{"role": "user", "content": "测试"}])

        self.assertEqual(str(context.exception), "大语言模型调用失败。")
        self.assertNotIn("fake-secret", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, RuntimeError)

    def test_auto_detects_deepseek_and_builds_client_from_fake_environment(self):
        fake_environment = {
            "LLM_API_KEY": "fake-api-key",
            "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_MODEL_ID": "fake-deepseek-model",
        }

        with patch("hello_agents.core.llm.OpenAI") as openai_class:
            llm = HelloAgentsLLM(environ=fake_environment)

        self.assertEqual(llm.provider, "deepseek")
        self.assertEqual(llm.model, "fake-deepseek-model")
        self.assertEqual(llm.base_url, "https://api.deepseek.com")
        openai_class.assert_called_once_with(
            api_key="fake-api-key",
            base_url="https://api.deepseek.com",
            timeout=60.0,
        )

    def test_missing_credentials_raise_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            HelloAgentsLLM(provider="deepseek", environ={})

    def test_reserved_request_fields_cannot_be_overridden(self):
        client, completions = make_fake_client(response=make_response("不会使用"))
        llm = HelloAgentsLLM(client=client, environ={})

        with self.assertRaises(LLMError):
            llm.invoke(
                [{"role": "user", "content": "测试"}],
                stream=True,
            )

        self.assertEqual(completions.calls, [])


if __name__ == "__main__":
    unittest.main()
