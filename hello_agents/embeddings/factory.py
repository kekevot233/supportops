"""根据显式配置创建 Embedding 后端。"""

from __future__ import annotations

from typing import Any

from ..core.exceptions import EmbeddingError
from .base import BaseEmbedder
from .config import EmbeddingConfig
from .hashing import HashingEmbedder
from .sentence_transformer import LocalSentenceTransformerEmbedder


def create_embedder(
    config: EmbeddingConfig | None = None,
    *,
    model: Any | None = None,
) -> BaseEmbedder:
    """创建向量器；传入 model 主要用于依赖注入和离线测试。"""
    if config is not None and not isinstance(config, EmbeddingConfig):
        raise EmbeddingError("config 必须是 EmbeddingConfig。")
    actual_config = config or EmbeddingConfig()
    if actual_config.provider == "hashing":
        return HashingEmbedder(
            dimension=actual_config.dimension,
            normalize=actual_config.normalize,
        )
    return LocalSentenceTransformerEmbedder(
        actual_config.model_path,
        model=model,
        expected_dimension=actual_config.dimension,
        normalize=actual_config.normalize,
    )


__all__ = ["create_embedder"]
