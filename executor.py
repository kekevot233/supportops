from prompts import EXECUTOR_PROMPT_TEMPLATE
class Executor:
    def __init__(self, llm_client):
        self.llm_client = llm_client

    def execute(
        self,
        question: str,
        plan: list[str],
    ) -> str | None:
        """
        按顺序执行计划中的所有步骤。
        """
        if not plan:
            print("错误：计划为空，无法执行。")
            return None

        history = []
        last_result = None

        print("\n--- 开始执行计划 ---")

        for index, current_step in enumerate(
            plan,
            start=1,
        ):
            print(
                f"\n--- 执行步骤 "
                f"{index}/{len(plan)} ---"
            )

            print(f"当前步骤：{current_step}")

            history_text = self._format_history(history)

            prompt = EXECUTOR_PROMPT_TEMPLATE.format(
                question=question,
                plan=self._format_plan(plan),
                history=history_text,
                current_step=current_step,
            )

            last_result = self.llm_client.generate(
                prompt=prompt,
                system_prompt=(
                    "你是一个严格按照计划逐步执行任务的助手。"
                ),
            )

            print(f"步骤结果：{last_result}")

            history.append({
                "step": current_step,
                "result": last_result,
            })

        return last_result

    @staticmethod
    def _format_plan(plan: list[str]) -> str:
        """
        把计划列表转换成方便模型阅读的文本。
        """
        lines = []

        for index, step in enumerate(plan, start=1):
            lines.append(f"{index}. {step}")

        return "\n".join(lines)

    @staticmethod
    def _format_history(
        history: list[dict[str, str]],
    ) -> str:
        """
        把已经执行的步骤和结果转换成文本。
        """
        if not history:
            return "暂无历史结果"

        lines = []

        for index, record in enumerate(
            history,
            start=1,
        ):
            lines.append(
                f"步骤 {index}：{record['step']}"
            )
            lines.append(
                f"结果：{record['result']}"
            )

        return "\n".join(lines)