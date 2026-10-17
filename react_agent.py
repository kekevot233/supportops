import re

from prompts import REACT_PROMPT_TEMPLATE


class ReActAgent:
    def __init__(
        self,
        llm_client,
        tool_executor,
        max_steps: int = 5,
    ):
        self.llm_client = llm_client
        self.tool_executor = tool_executor
        self.max_steps = max_steps
        self.history = []

    def run(self, question: str) -> str | None:
        """
        运行 ReAct Agent。
        """
        self.history = []

        for current_step in range(1, self.max_steps + 1):
            print(f"\n--- 第 {current_step} 步 ---")

            tools_description = (
                self.tool_executor.get_available_tools()
            )

            history_text = "\n".join(self.history)

            if not history_text:
                history_text = "暂无历史记录"

            prompt = REACT_PROMPT_TEMPLATE.format(
                tools=tools_description,
                question=question,
                history=history_text,
            )

            # 调用大模型
            llm_output = self.llm_client.generate(
                prompt=prompt,
                system_prompt="你是一个遵循 ReAct 范式的智能助手。",
            )

            print("\n--- 模型输出 ---")
            print(llm_output)

            # 解析 Thought 和 Action
            thought, action = self._parse_output(llm_output)

            if thought:
                print(f"\nThought: {thought}")

            if not action:
                observation = (
                    "错误：没有解析到 Action。"
                    "请严格按照 Thought 和 Action 格式输出。"
                )

                print(f"\nObservation: {observation}")

                self.history.append(
                    f"Observation: {observation}"
                )
                continue

            # 判断是否完成
            if action.startswith("Finish"):
                final_answer = self._parse_finish(action)

                if final_answer is None:
                    observation = (
                        "错误：Finish 格式不正确，"
                        "正确格式为 Finish[最终答案]。"
                    )

                    self.history.append(
                        f"Observation: {observation}"
                    )
                    continue

                print(f"\n最终答案：{final_answer}")
                return final_answer

            # 解析工具调用
            tool_name, tool_input = self._parse_action(action)

            if not tool_name:
                observation = (
                    "错误：工具调用格式不正确。"
                    "正确格式为 工具名称[工具输入]。"
                )
            else:
                tool_function = self.tool_executor.get_tool(
                    tool_name
                )

                if tool_function is None:
                    observation = (
                        f"错误：不存在名为 {tool_name} 的工具。"
                    )
                else:
                    try:
                        print(
                            f"\nAction: "
                            f"{tool_name}[{tool_input}]"
                        )

                        observation = tool_function(tool_input)

                    except Exception as e:
                        observation = (
                            f"错误：执行工具时发生异常：{e}"
                        )

            print(f"\nObservation: {observation}")

            # 保存本轮历史
            self.history.append(f"Thought: {thought}")
            self.history.append(f"Action: {action}")
            self.history.append(
                f"Observation: {observation}"
            )

        print("\n已达到最大步数，Agent 停止运行。")
        return None

    @staticmethod
    def _parse_output(text: str):
        """
        从模型输出中提取 Thought 和 Action。
        """
        thought_match = re.search(
            r"Thought:\s*(.*?)(?=\nAction:|$)",
            text,
            re.DOTALL,
        )

        action_match = re.search(
            r"Action:\s*(.*?)$",
            text,
            re.DOTALL,
        )

        thought = (
            thought_match.group(1).strip()
            if thought_match
            else None
        )

        action = (
            action_match.group(1).strip()
            if action_match
            else None
        )

        return thought, action

    @staticmethod
    def _parse_action(action: str):
        """
        将 Search[查询内容] 解析为工具名和输入。
        """
        match = re.fullmatch(
            r"(\w+)\[(.*)]",
            action,
            re.DOTALL,
        )

        if not match:
            return None, None

        tool_name = match.group(1)
        tool_input = match.group(2).strip()

        return tool_name, tool_input

    @staticmethod
    def _parse_finish(action: str):
        """
        从 Finish[答案] 中提取最终答案。
        """
        match = re.fullmatch(
            r"Finish\[(.*)]",
            action,
            re.DOTALL,
        )

        if not match:
            return None

        return match.group(1).strip()