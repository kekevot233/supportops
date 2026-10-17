import unittest

from hello_agents import (
    AgentError,
    ConfigurationError,
    LLMError,
    Message,
    ReflectionAgent,
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


class TestReflectionAgent(unittest.TestCase):
    def test_pass_keeps_initial_answer_and_saves_clean_history(self):
        llm = FakeLLM(
            [
                "初始回答",
                "回答完整准确。\nSTATUS: PASS",
            ]
        )
        agent = ReflectionAgent(name="反思助手", llm=llm)

        result = agent.run("解释 Agent", temperature=0)

        self.assertEqual(result, "初始回答")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0]["kwargs"], {"temperature": 0})
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "解释 Agent"},
                {"role": "assistant", "content": "初始回答"},
            ],
        )

    def test_needs_improvement_refines_then_passes(self):
        llm = FakeLLM(
            [
                "版本一",
                "缺少例子。\nSTATUS: NEEDS_IMPROVEMENT",
                "版本二，增加了例子。",
                "已经完整。\nSTATUS: PASS",
            ]
        )
        agent = ReflectionAgent(
            name="反思助手",
            llm=llm,
            max_iterations=2,
        )

        result = agent.run("写一段说明")

        self.assertEqual(result, "版本二，增加了例子。")
        self.assertEqual(len(llm.calls), 4)
        refine_prompt = llm.calls[2]["messages"][-1].content
        self.assertIn("版本一", refine_prompt)
        self.assertIn("缺少例子。", refine_prompt)

    def test_last_iteration_can_return_refined_unreviewed_answer(self):
        llm = FakeLLM(
            [
                "初稿",
                "需要改进。\nSTATUS: NEEDS_IMPROVEMENT",
                "最终改进稿",
            ]
        )
        agent = ReflectionAgent(
            name="反思助手",
            llm=llm,
            max_iterations=1,
        )

        self.assertEqual(agent.run("任务"), "最终改进稿")
        self.assertEqual(len(llm.calls), 3)

    def test_previous_history_and_custom_system_prompt_are_used(self):
        llm = FakeLLM(["新回答", "合格。\nSTATUS: PASS"])
        agent = ReflectionAgent(
            name="反思助手",
            llm=llm,
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

    def test_custom_prompts_are_applied(self):
        prompts = {
            "initial": "INITIAL::{task}",
            "reflect": "REFLECT::{task}::{content}",
            "refine": "REFINE::{task}::{last_attempt}::{feedback}",
        }
        llm = FakeLLM(["回答", "通过。\nSTATUS: PASS"])
        agent = ReflectionAgent(
            name="反思助手",
            llm=llm,
            prompts=prompts,
        )

        agent.run("任务")

        self.assertEqual(llm.calls[0]["messages"][-1].content, "INITIAL::任务")
        self.assertEqual(
            llm.calls[1]["messages"][-1].content,
            "REFLECT::任务::回答",
        )

    def test_invalid_feedback_status_does_not_save_history(self):
        invalid_feedback = (
            "没有状态",
            "STATUS: PASS\n后面还有文字",
            "STATUS: UNKNOWN",
            "STATUS: PASS\nSTATUS: NEEDS_IMPROVEMENT",
        )
        for feedback in invalid_feedback:
            with self.subTest(feedback=feedback):
                agent = ReflectionAgent(
                    name="反思助手",
                    llm=FakeLLM(["初稿", feedback]),
                )
                original = Message(role="assistant", content="已有历史")
                agent.add_message(original)

                with self.assertRaises(AgentError):
                    agent.run("任务")

                self.assertEqual(agent.get_history(), [original])

    def test_empty_or_failed_llm_call_does_not_save_history(self):
        cases = ("   ", LLMError("Fake 模型失败"))
        for response in cases:
            with self.subTest(response=response):
                agent = ReflectionAgent(
                    name="反思助手",
                    llm=FakeLLM([response]),
                )

                with self.assertRaises((AgentError, LLMError)):
                    agent.run("任务")

                self.assertEqual(agent.get_history(), [])

    def test_configuration_validation(self):
        llm = FakeLLM([])

        for invalid_iterations in (True, 0, -1, 11, 1.5):
            with self.subTest(invalid_iterations=invalid_iterations):
                with self.assertRaises(ConfigurationError):
                    ReflectionAgent(
                        name="助手",
                        llm=llm,
                        max_iterations=invalid_iterations,
                    )

        invalid_prompts = (
            {},
            {"initial": "{task}", "reflect": "{task}{content}"},
            {
                "initial": "{task}{unknown}",
                "reflect": "{task}{content}",
                "refine": "{task}{last_attempt}{feedback}",
            },
            {
                "initial": "{task",
                "reflect": "{task}{content}",
                "refine": "{task}{last_attempt}{feedback}",
            },
        )
        for prompts in invalid_prompts:
            with self.subTest(prompts=prompts):
                with self.assertRaises(ConfigurationError):
                    ReflectionAgent(name="助手", llm=llm, prompts=prompts)


if __name__ == "__main__":
    unittest.main()
