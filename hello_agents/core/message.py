"""HelloAgents 内部统一使用的消息数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


MessageRole = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """表示用户、模型、系统或工具产生的一条消息。"""

    content: str
    role: MessageRole
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        """转换为 OpenAI-compatible API 接受的基础消息格式。"""
        return {
            "role": self.role,
            "content": self.content,
        }

    def __str__(self) -> str:
        """返回适合日志和调试阅读的简洁形式。"""
        return f"[{self.role}] {self.content}"
