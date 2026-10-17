import re

from memory import Memory
from prompts import (
    INITIAL_PROMPT_TEMPLATE,
    REFLECT_PROMPT_TEMPLATE,
    REFINE_PROMPT_TEMPLATE,
)


class ReflectionAgent:
    """
    通过“执行、反思、优化”循环改进模型输出。
    """

    def __init__(
        self,
        llm_client,
        max_iterations: int = 2,
    ):
        self.llm_client = llm_client
        self.max_iterations = max_iterations
        self.memory = Memory()

    def run(self, task: str) -> str | None:
        """
        执行完整的 Reflection 流程。
        """
        # 每次处理新任务前清空旧记忆
        self.memory.clear()

        print("\n========== 原始任务 ==========")
        print(task)

        # 1. 生成初始版本
        print("\n========== 初始执行 ==========")

        initial_prompt = INITIAL_PROMPT_TEMPLATE.format(
            task=task,
        )

        initial_code = self.llm_client.generate(
            prompt=initial_prompt,
            system_prompt=(
                "你是一位负责完成编程任务的 Python 程序员。"
            ),
        )

        if not initial_code:
            print("错误：模型没有返回初始代码。")
            return None

        initial_code = self._clean_code_block(
            initial_code
        )

        # 把大模型生成的代码写入 Memory
        self.memory.add_record(
            record_type="execution",
            content=initial_code,
        )

        print("\n--- 初始代码 ---")
        print(initial_code)

        # 2. 进入反思和优化循环
        for iteration in range(
            1,
            self.max_iterations + 1,
        ):
            print(
                f"\n========== 第 {iteration}/"
                f"{self.max_iterations} 轮反思 =========="
            )

            # 从 Memory 读取最新代码
            last_code = (
                self.memory.get_last_execution()
            )

            if last_code is None:
                print("错误：Memory 中没有可评审的代码。")
                return None

            # 3. 调用大模型进行反思
            reflect_prompt = (
                REFLECT_PROMPT_TEMPLATE.format(
                    task=task,
                    code=last_code,
                )
            )

            feedback = self.llm_client.generate(
                prompt=reflect_prompt,
                system_prompt=(
                    "你是一位严格、客观的代码评审专家。"
                ),
            )

            if not feedback:
                print("错误：模型没有返回评审意见。")
                break

            # 把大模型生成的反馈写入 Memory
            self.memory.add_record(
                record_type="reflection",
                content=feedback,
            )

            print("\n--- 评审意见 ---")
            print(feedback)

            # 4. 判断是否需要继续优化
            status = self._parse_reflection_status(
                feedback
            )

            if status is None:
                print(
                    "\n错误：没有解析到有效的评审状态，"
                    "结束 Reflection。"
                )
                break

            if status == "PASS":
                print(
                    "\n评审状态为 PASS，"
                    "当前代码无需改进，结束 Reflection。"
                )
                break
            # 5. 根据反馈生成优化版本
            print("\n========== 代码优化 ==========")

            refine_prompt = (
                REFINE_PROMPT_TEMPLATE.format(
                    task=task,
                    last_code_attempt=last_code,
                    feedback=feedback,
                )
            )

            refined_code = self.llm_client.generate(
                prompt=refine_prompt,
                system_prompt=(
                    "你是一位根据评审意见优化代码的"
                    " Python 程序员。"
                ),
            )

            if not refined_code:
                print("错误：模型没有返回优化代码。")
                break

            refined_code = self._clean_code_block(
                refined_code
            )

            # 把新版本写入 Memory
            self.memory.add_record(
                record_type="execution",
                content=refined_code,
            )

            print("\n--- 优化后的代码 ---")
            print(refined_code)

        # 6. 从 Memory 获取最终版本
        final_code = self.memory.get_last_execution()

        print("\n========== 最终代码 ==========")
        print(final_code)

        print("\n========== Memory 轨迹 ==========")
        print(self.memory.get_trajectory())

        return final_code

    @staticmethod
    def _clean_code_block(text: str) -> str:
        """
        删除模型返回内容外层的 Markdown 代码块。

        例如把：

        ```python
        def example():
            pass
        ```

        转换成纯 Python 代码。
        """
        match = re.search(
            r"```(?:python)?\s*(.*?)```",
            text,
            re.DOTALL,
        )

        if match:
            return match.group(1).strip()

        return text.strip()

    @staticmethod
    def _parse_reflection_status(
            feedback: str,
    ) -> str | None:
        """
        从评审意见中解析结构化状态。
        """
        match = re.search(
            r"^STATUS:\s*"
            r"(PASS|NEEDS_IMPROVEMENT)\s*$",
            feedback,
            re.MULTILINE,
        )

        if not match:
            return None

        return match.group(1)