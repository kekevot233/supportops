"""记忆遗忘策略的可验证、可解释规则。"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .base import MemoryItem


class ForgettingStrategy(str, Enum):
    IMPORTANCE = "importance"
    TIME = "time"
    CAPACITY = "capacity"
    SMART = "smart"


class ForgettingPolicy(BaseModel):
    """描述如何筛选应该删除的记忆，不直接执行删除。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    strategy: ForgettingStrategy
    importance_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    max_age_days: float = Field(default=90.0, gt=0.0)
    capacity: int = Field(default=1000, ge=0)
    retention_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    max_removals: int | None = Field(default=None, gt=0)
    importance_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    recency_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    access_weight: float = Field(default=0.2, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights(self) -> "ForgettingPolicy":
        total = self.importance_weight + self.recency_weight + self.access_weight
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("智能遗忘的三个权重之和必须等于 1。")
        return self

    def select(self, items: list[MemoryItem], *, now: datetime) -> list[MemoryItem]:
        """返回按最应该先遗忘排序的快照。"""
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("遗忘策略时间必须包含时区。")
        snapshots = [item.model_copy(deep=True) for item in items]
        scored = [(self._retention_score(item, now), item) for item in snapshots]

        if self.strategy == ForgettingStrategy.IMPORTANCE:
            selected = [pair for pair in scored if pair[1].importance < self.importance_threshold]
        elif self.strategy == ForgettingStrategy.TIME:
            selected = [
                pair
                for pair in scored
                if self._age_days(pair[1], now) > self.max_age_days
            ]
        elif self.strategy == ForgettingStrategy.CAPACITY:
            overflow = max(0, len(scored) - self.capacity)
            selected = sorted(scored, key=self._sort_key)[:overflow]
        else:
            selected = [pair for pair in scored if pair[0] < self.retention_threshold]

        selected.sort(key=self._sort_key)
        if self.max_removals is not None:
            selected = selected[: self.max_removals]
        return [item for _, item in selected]

    def _retention_score(self, item: MemoryItem, now: datetime) -> float:
        age_days = self._age_days(item, now)
        recency = math.exp(-age_days / self.max_age_days)
        access = min(1.0, math.log1p(item.access_count) / math.log(11.0))
        return (
            item.importance * self.importance_weight
            + recency * self.recency_weight
            + access * self.access_weight
        )

    @staticmethod
    def _age_days(item: MemoryItem, now: datetime) -> float:
        return max(0.0, (now - item.created_at).total_seconds() / 86400.0)

    @staticmethod
    def _sort_key(pair: tuple[float, MemoryItem]) -> tuple[float, datetime, str]:
        score, item = pair
        return score, item.created_at, item.id


__all__ = ["ForgettingPolicy", "ForgettingStrategy"]
