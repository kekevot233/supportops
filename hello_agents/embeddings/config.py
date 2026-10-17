"""Embedding 后端的显式配置模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EmbeddingProvider = Literal["hashing", "sentence_transformer"]


class EmbeddingConfig(BaseModel):
    """描述向量化后端；配置对象本身不会读取环境变量。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    provider: EmbeddingProvider = "hashing"
    dimension: int = Field(default=384, gt=0, le=16384)
    model_path: str | None = None
    normalize: bool = True

    @field_validator("model_path", mode="before")
    @classmethod
    def normalize_model_path(cls, value: object) -> object:
        if isinstance(value, Path):
            return str(value)
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_provider_options(self) -> "EmbeddingConfig":
        if self.provider == "sentence_transformer" and not self.model_path:
            raise ValueError("本地 SentenceTransformer 后端必须配置 model_path。")
        return self


__all__ = ["EmbeddingConfig", "EmbeddingProvider"]
