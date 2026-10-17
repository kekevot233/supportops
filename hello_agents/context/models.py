"""上下文工程使用的数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ContextKind = Literal[
    "system",
    "task",
    "evidence",
    "memory",
    "note",
    "conversation",
    "tool",
    "general",
]


class ContextPacket(BaseModel):
    """一条可独立评分、选择和压缩的上下文信息。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    content: str = Field(min_length=1)
    source: str = Field(default="custom", min_length=1, max_length=100)
    kind: ContextKind = "general"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    trusted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content", "source", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp 必须包含时区。")
        return value


class ContextConfig(BaseModel):
    """GSSC 流水线的预算、评分和压缩配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_tokens: int = Field(default=4096, ge=128, le=1_000_000)
    reserve_ratio: float = Field(default=0.2, ge=0.0, lt=0.9)
    min_relevance: float = Field(default=0.05, ge=0.0, le=1.0)
    relevance_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    recency_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    priority_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    recency_half_life_hours: float = Field(default=168.0, gt=0.0)
    max_packets: int = Field(default=50, gt=0, le=1000)
    min_compressed_packet_tokens: int = Field(default=24, ge=8, le=10_000)
    enable_compression: bool = True
    compression_fail_open: bool = True
    # 每个可选来源的正文总上限；结构包装仍计入整体预算。0 表示禁用该来源。
    source_max_tokens: dict[
        str, Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    ] = Field(default_factory=dict, max_length=1000)

    @field_validator("source_max_tokens")
    @classmethod
    def validate_source_limits(cls, value: dict[str, int]) -> dict[str, int]:
        for name in value:
            if (
                not name.strip()
                or name != name.strip()
                or len(name) > 100
                or any(char in name for char in "\r\n\x00")
            ):
                raise ValueError("来源预算名称必须是 1 到 100 字符的有效来源名。")
        return value

    @model_validator(mode="after")
    def validate_weights(self) -> "ContextConfig":
        total = self.relevance_weight + self.recency_weight + self.priority_weight
        if abs(total - 1.0) > 1e-9:
            raise ValueError("相关性、新近性和优先级权重之和必须为 1。")
        if self.min_compressed_packet_tokens >= self.available_tokens:
            raise ValueError("最小压缩包预算必须小于可用上下文预算。")
        return self

    @property
    def available_tokens(self) -> int:
        """为模型输出预留空间后，可用于上下文的估算 Token 数。"""
        return max(1, int(self.max_tokens * (1.0 - self.reserve_ratio)))


class SelectedContextPacket(BaseModel):
    """记录最终入选信息及其综合评分。"""

    model_config = ConfigDict(extra="forbid")

    packet: ContextPacket
    score: float = Field(ge=0.0, le=1.0)
    estimated_tokens: int = Field(gt=0)
    compressed: bool = False


class ContextQualityReport(BaseModel):
    """结构化上下文的确定性质量指标与改进建议。"""

    model_config = ConfigDict(extra="forbid")

    overall_score: float = Field(ge=0.0, le=1.0)
    average_score: float = Field(ge=0.0, le=1.0)
    information_density: float = Field(ge=0.0, le=1.0)
    query_coverage: float = Field(ge=0.0, le=1.0)
    budget_utilization: float = Field(ge=0.0, le=1.0)
    source_diversity: float = Field(ge=0.0, le=1.0)
    selected_count: int = Field(ge=0)
    trusted_count: int = Field(ge=0)
    untrusted_count: int = Field(ge=0)
    suggestions: list[str] = Field(default_factory=list)


class SourceTokenUsage(BaseModel):
    """可选来源的最终正文占用，不含统一标题、标签与安全包装。"""

    model_config = ConfigDict(extra="forbid")

    content_tokens: int = Field(ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    selected_count: int = Field(ge=0)
    dropped_count: int = Field(ge=0)


class ContextBuildResult(BaseModel):
    """一次上下文构建的可观测结果。"""

    model_config = ConfigDict(extra="forbid")

    context: str
    selected: list[SelectedContextPacket] = Field(default_factory=list)
    gathered_count: int = Field(ge=0)
    dropped_count: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    token_budget: int = Field(gt=0)
    compressed: bool = False
    warnings: list[str] = Field(default_factory=list)
    quality: ContextQualityReport
    token_counter: str = "heuristic"
    source_usage: dict[str, SourceTokenUsage] = Field(default_factory=dict)


__all__ = [
    "ContextBuildResult",
    "ContextConfig",
    "ContextKind",
    "ContextPacket",
    "ContextQualityReport",
    "SelectedContextPacket",
    "SourceTokenUsage",
]
