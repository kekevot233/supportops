"""Agent 临时上下文提供器的最小契约。"""

from abc import ABC, abstractmethod


class BaseContextProvider(ABC):
    """为一次 Agent 运行生成不写入对话历史的外部上下文。"""

    @abstractmethod
    def build_context(self, query: str) -> str:
        raise NotImplementedError


__all__ = ["BaseContextProvider"]
