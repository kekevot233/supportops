import unittest

from hello_agents import (
    AgentError,
    ConfigurationError,
    LLMError,
    Message,
    PlanAndSolveAgent,
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


class TestPlanAndSolveAgent(unittest.TestCase):
    def test_executes_plan_in_order_and_saves_final_result(self):
        llm = FakeLLM(
            [
                '["计算周二销量", "计算周三销量", "计算三天总销量"]',
                "周二卖出 30 个苹果。",
                "周三卖出 25 个苹果。",
                "三天总共卖出 70 个苹果。",
            ]
        )
        agent = PlanAndSolveAgent(name="规划助手", llm=llm)

        result = agent.run("周一 15 个，求三天总销量", temperature=0)

        self.assertEqual(result, "三天总共卖出 70 个苹果。")
        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(llm.calls[0]["kwargs"], {"temperature": 0})
        final_prompt = llm.calls[3]["messages"][-1].content
        self.assertIn("步骤 1：计算周二销量", final_prompt)
        self.assertIn("结果：周二卖出 30 个苹果。", final_prompt)
        self.assertIn("结果：周三卖出 25 个苹果。", final_prompt)
        self.assertEqual(
            [message.to_dict() for message in agent.get_history()],
            [
                {"role": "user", "content": "周一 15 个，求三天总销量"},
                {
                    "role": "assistant",
                    "content": "三天总共卖出 70 个苹果。",
                },
            ],
        )

    def test_single_step_plan_is_supported(self):
        llm = FakeLLM(['["直接回答问题"]', "最终答案"])
        agent = PlanAndSolveAgent(name="规划助手", llm=llm)

        self.assertEqual(agent.run("简单问题"), "最终答案")
        self.assertEqual(len(llm.calls), 2)

    def test_previous_history_and_custom_system_prompt_are_used(self):
        llm = FakeLLM(['["回答"]', "新答案"])
        agent = PlanAndSolveAgent(
            name="规划助手",
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
            "planner": "PLAN::{question}",
            "executor": "EXEC::{question}::{plan}::{history}::{current_step}",
        }
        llm = FakeLLM(['["步骤"]', "答案"])
        agent = PlanAndSolveAgent(
            name="规划助手",
            llm=llm,
            prompts=prompts,
        )

        agent.run("任务")

        self.assertEqual(llm.calls[0]["messages"][-1].content, "PLAN::任务")
        self.assertEqual(
            llm.calls[1]["messages"][-1].content,
            "EXEC::任务::1. 步骤::暂无历史结果::步骤",
        )

    def test_invalid_plan_does_not_change_history(self):
        invalid_plans = (
            "not json",
            "{}",
            "[]",
            "[1]",
            '[""]',
            '```json\n["步骤"]\n```',
        )
        for raw_plan in invalid_plans:
            with self.subTest(raw_plan=raw_plan):
                agent = PlanAndSolveAgent(
                    name="规划助手",
                    llm=FakeLLM([raw_plan]),
                )
                original = Message(role="assistant", content="已有历史")
                agent.add_message(original)

                with self.assertRaises(AgentError):
                    agent.run("任务")

                self.assertEqual(agent.get_history(), [original])

    def test_plan_step_limit_is_enforced_before_execution(self):
        llm = FakeLLM(['["一", "二", "三"]'])
        agent = PlanAndSolveAgent(
            name="规划助手",
            llm=llm,
            max_plan_steps=2,
        )

        with self.assertRaisesRegex(AgentError, "不能超过 2 个"):
            agent.run("任务")

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(agent.get_history(), [])

    def test_executor_empty_or_llm_failure_does_not_save_history(self):
        cases = (
            ['["步骤"]', "   "],
            ['["步骤"]', LLMError("Fake 执行失败")],
        )
        for responses in cases:
            with self.subTest(responses=responses):
                agent = PlanAndSolveAgent(
                    name="规划助手",
                    llm=FakeLLM(responses),
                )

                with self.assertRaises((AgentError, LLMError)):
                    agent.run("任务")

                self.assertEqual(agent.get_history(), [])

    def test_configuration_validation(self):
        llm = FakeLLM([])

        for invalid_limit in (True, 0, -1, 21, 1.5):
            with self.subTest(invalid_limit=invalid_limit):
                with self.assertRaises(ConfigurationError):
                    PlanAndSolveAgent(
                        name="助手",
                        llm=llm,
                        max_plan_steps=invalid_limit,
                    )

        invalid_prompts = (
            {},
            {"planner": "{question}"},
            {
                "planner": "{question}{unknown}",
                "executor": "{question}{plan}{history}{current_step}",
            },
            {
                "planner": "{question",
                "executor": "{question}{plan}{history}{current_step}",
            },
        )
        for prompts in invalid_prompts:
            with self.subTest(prompts=prompts):
                with self.assertRaises(ConfigurationError):
                    PlanAndSolveAgent(name="助手", llm=llm, prompts=prompts)


if __name__ == "__main__":
    unittest.main()
