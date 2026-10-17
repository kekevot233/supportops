"""向量存储的标准数据模型与抽象契约。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.exceptions import EmbeddingError, VectorStoreError
from ..embeddings.base import normalize_vector


def validate_metadata(metadata: object) -> dict[str, Any]:
    """确保 payload 可以安全序列化并与调用方状态隔离。"""
    if not isinstance(metadata, dict):
        raise ValueError("向量元数据必须是字典。")
    copied = deepcopy(metadata)
    try:
        json.dumps(copied, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("向量元数据必须是 JSON 可序列化数据。") from error
    return copied


class VectorRecord(BaseModel):
    """一个待写入向量存储的点。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    vector: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("vector", mode="before")
    @classmethod
    def validate_vector(cls, value: object) -> list[float]:
        try:
            return normalize_vector(value)
        except EmbeddingError as error:
            raise ValueError(str(error)) from error

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)


class VectorSearchResult(BaseModel):
    """向量检索返回的点 ID、相似度和 payload。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    score: float = Field(ge=-1.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)


class BaseVectorStore(ABC):
    """内存、Qdrant 等向量后端共同遵守的接口。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回集合的固定向量维度。"""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, records: list[VectorRecord]) -> int:
        """新增或替换向量，并返回处理的记录数量。"""
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query_vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        """按向量相似度搜索，可使用 payload 精确过滤。"""
        raise NotImplementedError

    @abstractmethod
    def delete(self, record_ids: list[str]) -> None:
        """删除给定 ID 的向量。"""
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        """清空当前集合。"""
        raise NotImplementedError

    @abstractmethod
    def count(self) -> int:
        """返回当前集合中的向量数量。"""
        raise NotImplementedError


def validate_dimension(dimension: object) -> int:
    if isinstance(dimension, bool) or not isinstance(dimension, int):
        raise VectorStoreError("向量存储维度必须是整数。")
    if not 1 <= dimension <= 65536:
        raise VectorStoreError("向量存储维度必须在 1 到 65536 之间。")
    return dimension


def validate_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise VectorStoreError("向量检索数量必须是正整数。")
    if limit > 1000:
        raise VectorStoreError("单次向量检索数量不能超过 1000。")
    return limit


__all__ = [
    "BaseVectorStore",
    "VectorRecord",
    "VectorSearchResult",
    "validate_dimension",
    "validate_limit",
    "validate_metadata",
]
