
### 2. 创建 `planner.py`


import ast
import re

from prompts import PLANNER_PROMPT_TEMPLATE


class Planner:
    def __init__(self, llm_client):
        self.llm_client = llm_client

    def plan(self, question: str) -> list[str]:
        """
        将用户问题拆解成步骤列表。
        """
        prompt = PLANNER_PROMPT_TEMPLATE.format(
            question=question
        )

        print("\n--- 正在生成计划 ---")

        response_text = self.llm_client.generate(
            prompt=prompt,
            system_prompt="你是一个擅长拆解复杂任务的规划专家。",
        )

        print("\n--- 模型生成的原始计划 ---")
        print(response_text)

        plan_text = self._extract_plan_text(response_text)

        if plan_text is None:
            print("错误：没有找到计划列表。")
            return []

        try:
            plan = ast.literal_eval(plan_text)
        except (ValueError, SyntaxError) as e:
            print(f"错误：计划解析失败：{e}")
            return []

        if not isinstance(plan, list):
            print("错误：模型返回的内容不是列表。")
            return []

        if not all(isinstance(step, str) for step in plan):
            print("错误：计划中的步骤必须是字符串。")
            return []

        return plan

    @staticmethod
    def _extract_plan_text(response_text: str) -> str | None:
        """
        提取 ```python 和 ``` 之间的列表。
        """
        code_block_match = re.search(
            r"```(?:python)?\s*(.*?)```",
            response_text,
            re.DOTALL,
        )

        if code_block_match:
            return code_block_match.group(1).strip()

        # 模型有时不会输出代码块，尝试直接寻找列表
        list_match = re.search(
            r"\[.*]",
            response_text,
            re.DOTALL,
        )

        if list_match:
            return list_match.group(0).strip()

        return None