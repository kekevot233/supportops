"""知识图谱存储的模型与抽象契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..vector_stores.base import validate_metadata


class GraphEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    entity_type: Annotated[str, Field(min_length=1)] = "Concept"
    user_id: Annotated[str, Field(min_length=1)]
    source_memory_ids: list[str] = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "entity_type", "user_id", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("source_memory_ids")
    @classmethod
    def validate_sources(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("来源记忆 ID 不能为空。")
            if value.strip() not in normalized:
                normalized.append(value.strip())
        return normalized

    @field_validator("properties", mode="before")
    @classmethod
    def validate_properties(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)


class GraphRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]
    relation_type: Annotated[str, Field(min_length=1)]
    user_id: Annotated[str, Field(min_length=1)]
    source_memory_id: Annotated[str, Field(min_length=1)]
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "id", "source_id", "target_id", "relation_type",
        "user_id", "source_memory_id", mode="before"
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("properties", mode="before")
    @classmethod
    def validate_properties(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)


class GraphSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: Annotated[str, Field(min_length=1)]
    score: float = Field(ge=0.0, le=1.0)
    matched_entity_ids: list[str] = Field(default_factory=list)


class BaseGraphStore(ABC):
    @abstractmethod
    def upsert(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> list[GraphSearchResult]:
        raise NotImplementedError

    @abstractmethod
    def delete_memory(self, memory_id: str, *, user_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def clear_user(self, user_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def stats(self, user_id: str) -> dict[str, int]:
        raise NotImplementedError


__all__ = [
    "BaseGraphStore",
    "GraphEntity",
    "GraphRelation",
    "GraphSearchResult",
]
