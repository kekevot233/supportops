from executor import Executor
from planner import Planner


class PlanAndSolveAgent:
    def __init__(self, llm_client):
        self.planner = Planner(llm_client)
        self.executor = Executor(llm_client)

    def run(self, question: str) -> str | None:
        print("\n========== Plan 阶段 ==========")

        plan = self.planner.plan(question)

        if not plan:
            print("无法生成有效计划，任务结束。")
            return None

        print("\n最终计划：")

        for index, step in enumerate(plan, start=1):
            print(f"{index}. {step}")

        print("\n========== Solve 阶段 ==========")

        final_answer = self.executor.execute(
            question=question,
            plan=plan,
        )

        if final_answer is None:
            print("计划执行失败。")
            return None

        print("\n========== 最终答案 ==========")
        print(final_answer)

        return final_answer