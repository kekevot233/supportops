"""文档 RAG 的公开数据模型。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..vector_stores.base import validate_metadata


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    content: Annotated[str, Field(min_length=1)]
    source: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "content", "source", "title", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_document_metadata(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)


class DocumentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    document_id: Annotated[str, Field(min_length=1)]
    content: Annotated[str, Field(min_length=1)]
    source: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    chunk_index: int = Field(ge=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    section: str | None = None
    page_number: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "id", "document_id", "content", "source", "title", mode="before"
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("section", mode="before")
    @classmethod
    def normalize_section(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_chunk_metadata(cls, value: object) -> dict[str, Any]:
        return validate_metadata(value)

    @model_validator(mode="after")
    def validate_offsets(self) -> "DocumentChunk":
        if self.end_char <= self.start_char:
            raise ValueError("文档块结束位置必须大于开始位置。")
        return self


class RetrievedDocumentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk: DocumentChunk
    vector_score: float = Field(ge=-1.0, le=1.0)
    lexical_score: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0, le=1.0)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1)]
    document_id: Annotated[str, Field(min_length=1)]
    chunk_id: Annotated[str, Field(min_length=1)]
    source: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    chunk_index: int = Field(ge=0)
    section: str | None = None
    page_number: int | None = Field(default=None, gt=0)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)


class DocumentRAGResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1)]
    hits: list[RetrievedDocumentChunk] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class IngestionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: Annotated[str, Field(min_length=1)]
    chunk_count: int = Field(ge=0)
    replaced_chunk_count: int = Field(ge=0)
    unchanged: bool = False


__all__ = [
    "Citation",
    "Document",
    "DocumentChunk",
    "DocumentRAGResult",
    "IngestionReport",
    "RetrievedDocumentChunk",
]
