class Memory:
    """
    Reflection Agent 使用的短期记忆模块。

    records 按照时间顺序保存：
    1. 模型生成的执行结果
    2. 模型给出的反思意见
    3. 根据意见生成的新结果
    """

    def __init__(self):
        """
        创建一个空的记忆列表。
        """
        self.records: list[dict[str, str]] = []

    def add_record(
        self,
        record_type: str,
        content: str,
    ) -> None:
        """
        添加一条记忆。

        record_type 只能是：
        - execution：执行结果
        - reflection：反思意见

        content 是具体内容，例如代码或评审意见。
        """
        allowed_types = {
            "execution",
            "reflection",
        }

        if record_type not in allowed_types:
            raise ValueError(
                "record_type 必须是 "
                "'execution' 或 'reflection'"
            )

        record = {
            "type": record_type,
            "content": content,
        }

        self.records.append(record)

        print(
            f"记忆已更新，新增一条 "
            f"'{record_type}' 记录。"
        )

    def get_trajectory(self) -> str:
        """
        把全部记忆转换成一段文本。

        这段文本以后可以放进提示词，
        让模型看到之前的执行和反思过程。
        """
        if not self.records:
            return "暂无记忆记录"

        trajectory_parts = []

        for record in self.records:
            record_type = record["type"]
            content = record["content"]

            if record_type == "execution":
                trajectory_parts.append(
                    "--- 执行结果 ---\n"
                    f"{content}"
                )

            elif record_type == "reflection":
                trajectory_parts.append(
                    "--- 反思意见 ---\n"
                    f"{content}"
                )

        return "\n\n".join(trajectory_parts)

    def get_last_execution(self) -> str | None:
        """
        获取最近一次 execution 记录。

        从列表末尾向前查找，因为最后一次执行结果
        就是当前最新版本。
        """
        for record in reversed(self.records):
            if record["type"] == "execution":
                return record["content"]

        return None

    def clear(self) -> None:
        """
        清空当前任务的全部记忆。
        """
        self.records.clear()
        print("记忆已清空。")