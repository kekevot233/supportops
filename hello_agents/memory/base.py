"""记忆系统的基础数据模型与存储契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MemoryType = Literal["working", "episodic", "semantic", "perceptual"]


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


class MemoryItem(BaseModel):
    """一条可在不同记忆后端之间传递的标准记忆。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    content: Annotated[str, Field(min_length=1)]
    memory_type: MemoryType = "working"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)
    accessed_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    access_count: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "content", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        """统一清理外部输入的文本边界空格。"""
        return value.strip() if isinstance(value, str) else value

    @field_validator("created_at", "accessed_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """禁止无时区时间，避免本地时间和 UTC 比较产生歧义。"""
        if value is not None and value.utcoffset() is None:
            raise ValueError("记忆时间必须包含时区信息。")
        return value


class MemoryConfig(BaseModel):
    """首阶段记忆系统的离线配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    working_memory_capacity: int = Field(default=50, gt=0)
    working_memory_ttl_seconds: float = Field(default=3600.0, gt=0.0)
    default_retrieval_limit: int = Field(default=5, gt=0)
    max_retrieval_limit: int = Field(default=100, gt=0)
    episodic_recency_decay_days: float = Field(default=30.0, gt=0.0)
    episodic_candidate_multiplier: int = Field(default=5, gt=0, le=20)
    perceptual_recency_decay_hours: float = Field(default=240.0, gt=0.0)
    perceptual_candidate_multiplier: int = Field(default=5, gt=0, le=20)

    @model_validator(mode="after")
    def validate_limits(self) -> "MemoryConfig":
        """默认返回数量不能超过总的检索上限。"""
        if self.default_retrieval_limit > self.max_retrieval_limit:
            raise ValueError("默认检索数量不能大于最大检索数量。")
        return self


class MemorySearchResult(BaseModel):
    """记忆检索结果及其可解释的相关度分数。"""

    model_config = ConfigDict(extra="forbid")

    item: MemoryItem
    score: float = Field(ge=0.0)


class BaseMemory(ABC):
    """所有记忆后端必须实现的最小契约。"""

    @abstractmethod
    def add(self, item: MemoryItem) -> str:
        """保存记忆并返回记忆 ID。"""
        raise NotImplementedError

    @abstractmethod
    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float = 0.0,
    ) -> list[MemorySearchResult]:
        """按相关性检索记忆。"""
        raise NotImplementedError

    @abstractmethod
    def remove(self, memory_id: str) -> MemoryItem:
        """删除并返回指定记忆。"""
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> int:
        """清空后端并返回删除数量。"""
        raise NotImplementedError

    @abstractmethod
    def list_items(self) -> list[MemoryItem]:
        """返回当前有效记忆的快照。"""
        raise NotImplementedError

    @abstractmethod
    def __len__(self) -> int:
        """返回当前有效记忆数量。"""
        raise NotImplementedError


__all__ = [
    "BaseMemory",
    "MemoryConfig",
    "MemoryItem",
    "MemorySearchResult",
    "MemoryType",
    "utc_now",
]
