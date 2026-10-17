import unittest

from hello_agents import (
    Agent,
    AgentError,
    Config,
    ConfigurationError,
    LLMError,
    Message,
)


class FakeLLM:
    provider = "fake-provider"


class RecordingAgent(Agent):
    def __init__(self, *args, response="Fake 回答", error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response
        self.error = error
        self.received_input = None
        self.received_kwargs = None

    def _execute(self, input_text, **kwargs):
        self.received_input = input_text
        self.received_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class MessageBuildingAgent(Agent):
    def _execute(self, input_text, **kwargs):
        self.last_messages = self._build_messages(input_text)
        return "已构造消息"


class TestAgent(unittest.TestCase):
    def test_abstract_agent_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Agent(name="抽象 Agent", llm=FakeLLM())

    def test_run_validates_then_delegates_to_execute(self):
        agent = RecordingAgent(name="测试 Agent", llm=FakeLLM())

        result = agent.run(" 保留两侧空格 ", temperature=0)

        self.assertEqual(result, "Fake 回答")
        self.assertEqual(agent.received_input, " 保留两侧空格 ")
        self.assertEqual(agent.received_kwargs, {"temperature": 0})

    def test_run_rejects_empty_input_and_invalid_output(self):
        agent = RecordingAgent(name="测试 Agent", llm=FakeLLM())

        for invalid_input in ("", "   ", None):
            with self.subTest(invalid_input=invalid_input):
                with self.assertRaises(AgentError):
                    agent.run(invalid_input)

        agent.response = "   "
        with self.assertRaises(AgentError):
            agent.run("有效输入")

    def test_run_preserves_framework_errors_and_wraps_unexpected_errors(self):
        framework_error = LLMError("Fake LLM 失败")
        agent = RecordingAgent(
            name="测试 Agent",
            llm=FakeLLM(),
            error=framework_error,
        )

        with self.assertRaises(LLMError) as framework_context:
            agent.run("测试")
        self.assertIs(framework_context.exception, framework_error)

        agent.error = RuntimeError("Fake 未预期错误")
        with self.assertRaises(AgentError) as unexpected_context:
            agent.run("测试")
        self.assertIsInstance(unexpected_context.exception.__cause__, RuntimeError)

    def test_history_is_copied_and_trimmed_to_configured_limit(self):
        agent = RecordingAgent(
            name="测试 Agent",
            llm=FakeLLM(),
            config=Config(max_history_length=2),
        )
        first = Message(role="user", content="第一条")
        agent.add_message(first)
        agent.add_message(Message(role="assistant", content="第二条"))
        agent.add_message(Message(role="user", content="第三条"))

        first.content = "外部已修改"
        history = agent.get_history()
        history[0].content = "返回值已修改"

        self.assertEqual(
            [message.content for message in agent.get_history()],
            ["第二条", "第三条"],
        )

    def test_clear_history_and_reject_non_message(self):
        agent = RecordingAgent(name="测试 Agent", llm=FakeLLM())
        agent.add_message(Message(role="user", content="测试"))

        with self.assertRaises(AgentError):
            agent.add_message({"role": "user", "content": "不是 Message"})

        agent.clear_history()
        self.assertEqual(agent.get_history(), [])

    def test_build_messages_uses_system_history_user_order(self):
        agent = MessageBuildingAgent(
            name="测试 Agent",
            llm=FakeLLM(),
            system_prompt="你是测试助手",
        )
        agent.add_message(Message(role="user", content="历史问题"))
        agent.add_message(Message(role="assistant", content="历史回答"))

        result = agent.run("当前问题")

        self.assertEqual(result, "已构造消息")
        self.assertEqual(
            [message.to_dict() for message in agent.last_messages],
            [
                {"role": "system", "content": "你是测试助手"},
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "user", "content": "当前问题"},
            ],
        )

    def test_initialization_and_string_representation(self):
        agent = RecordingAgent(name="  测试 Agent  ", llm=FakeLLM())

        self.assertEqual(agent.name, "测试 Agent")
        self.assertEqual(
            str(agent),
            "Agent(name=测试 Agent, provider=fake-provider)",
        )

        invalid_arguments = (
            {"name": "   ", "llm": FakeLLM()},
            {"name": "测试", "llm": None},
            {"name": "测试", "llm": FakeLLM(), "system_prompt": 123},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ConfigurationError):
                    RecordingAgent(**arguments)


if __name__ == "__main__":
    unittest.main()
