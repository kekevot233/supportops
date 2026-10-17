from typing import Any, Callable



class ToolExecutor:
    """
    负责注册、查找和执行工具。
    """

    def __init__(self):
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        func: Callable,
    ):
        """
        注册一个工具。
        """
        if name in self.tools:
            print(f"警告：工具 {name} 已存在，将被覆盖。")

        self.tools[name] = {
            "description": description,
            "func": func,
        }

        print(f"工具 {name} 已注册。")

    def get_tool(self, name: str) -> Callable | None:
        """
        根据名称获取工具函数。
        """
        tool_info = self.tools.get(name)

        if tool_info is None:
            return None

        return tool_info["func"]

    def get_available_tools(self) -> str:
        """
        返回所有工具的名称和描述。
        """
        descriptions = []

        for name, tool_info in self.tools.items():
            description = tool_info["description"]
            descriptions.append(f"- {name}: {description}")

        return "\n".join(descriptions)
