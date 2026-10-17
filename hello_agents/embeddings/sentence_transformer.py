"""本地 SentenceTransformer 模型适配器。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.exceptions import EmbeddingError
from .base import (
    BaseEmbedder,
    normalize_vector,
    validate_text,
    validate_text_batch,
)


class LocalSentenceTransformerEmbedder(BaseEmbedder):
    """延迟加载本地模型；不会根据模型名称自动访问网络。"""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        model: Any | None = None,
        expected_dimension: int | None = None,
        normalize: bool = True,
    ) -> None:
        if model is None and model_path is None:
            raise EmbeddingError("必须提供本地模型路径或已加载的模型对象。")
        if expected_dimension is not None and (
            isinstance(expected_dimension, bool)
            or not isinstance(expected_dimension, int)
            or expected_dimension <= 0
        ):
            raise EmbeddingError("预期向量维度必须是正整数。")
        if not isinstance(normalize, bool):
            raise EmbeddingError("normalize 必须是布尔值。")

        self.model_path = Path(model_path) if model_path is not None else None
        self._model = model
        self._expected_dimension = expected_dimension
        self.normalize = normalize

    @property
    def dimension(self) -> int:
        if self._expected_dimension is not None:
            return self._expected_dimension

        model = self._ensure_model()
        get_dimension = getattr(model, "get_sentence_embedding_dimension", None)
        if not callable(get_dimension):
            raise EmbeddingError(
                "本地模型未公开向量维度，请配置 expected_dimension。"
            )
        try:
            dimension = get_dimension()
        except Exception as error:
            raise EmbeddingError("无法读取本地模型的向量维度。") from error
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            raise EmbeddingError("本地模型返回了无效的向量维度。")
        if dimension <= 0:
            raise EmbeddingError("本地模型的向量维度必须大于零。")
        self._expected_dimension = dimension
        return dimension

    def embed(self, text: str) -> list[float]:
        normalized = validate_text(text)
        return self.embed_batch([normalized])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        normalized_texts = validate_text_batch(texts)
        if not normalized_texts:
            return []

        model = self._ensure_model()
        try:
            raw_vectors = model.encode(normalized_texts)
        except Exception as error:
            raise EmbeddingError("本地 Embedding 模型编码失败。") from error
        if hasattr(raw_vectors, "tolist"):
            raw_vectors = raw_vectors.tolist()
        if (
            isinstance(raw_vectors, (str, bytes))
            or not isinstance(raw_vectors, Sequence)
            or len(raw_vectors) != len(normalized_texts)
        ):
            raise EmbeddingError("本地模型返回的向量数量与输入数量不一致。")

        expected_dimension = self._expected_dimension
        vectors: list[list[float]] = []
        for raw_vector in raw_vectors:
            vector = normalize_vector(
                raw_vector,
                expected_dimension=expected_dimension,
                l2_normalize=self.normalize,
            )
            if expected_dimension is None:
                expected_dimension = len(vector)
            vectors.append(vector)

        self._expected_dimension = expected_dimension
        return vectors

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self.model_path is None or not self.model_path.exists():
            raise EmbeddingError("本地 SentenceTransformer 模型路径不存在。")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise EmbeddingError(
                "使用本地模型需要安装 sentence-transformers。"
            ) from error
        try:
            self._model = SentenceTransformer(str(self.model_path))
        except Exception as error:
            raise EmbeddingError("本地 SentenceTransformer 模型加载失败。") from error
        return self._model


__all__ = ["LocalSentenceTransformerEmbedder"]
