"""Embedding 组件的统一契约与向量工具。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from numbers import Real

from ..core.exceptions import EmbeddingError


class BaseEmbedder(ABC):
    """文本向量化后端共同遵守的最小契约。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回单个向量的固定维度。"""
        raise NotImplementedError

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """把一段非空文本编码成固定维度向量。"""
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序批量编码；后端可以覆盖它以提高吞吐量。"""
        normalized = validate_text_batch(texts)
        return [self.embed(text) for text in normalized]


def validate_text(text: object) -> str:
    """校验并清理一段待编码文本。"""
    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("待编码文本不能为空。")
    return text.strip()


def validate_text_batch(texts: object) -> list[str]:
    """校验批量输入，同时拒绝把单个字符串误当成字符序列。"""
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise EmbeddingError("批量编码输入必须是文本序列。")
    return [validate_text(text) for text in texts]


def normalize_vector(
    vector: object,
    *,
    expected_dimension: int | None = None,
    l2_normalize: bool = False,
) -> list[float]:
    """把模型输出转换成有限浮点数，并可执行 L2 归一化。"""
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise EmbeddingError("Embedding 模型必须返回数值序列。")

    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise EmbeddingError("Embedding 向量只能包含数值。")
        converted = float(value)
        if not math.isfinite(converted):
            raise EmbeddingError("Embedding 向量不能包含 NaN 或无穷值。")
        values.append(converted)

    if not values:
        raise EmbeddingError("Embedding 向量不能为空。")
    if expected_dimension is not None and len(values) != expected_dimension:
        raise EmbeddingError(
            f"Embedding 向量维度应为 {expected_dimension}，实际为 {len(values)}。"
        )

    if l2_normalize:
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:
            raise EmbeddingError("Embedding 模型返回了零向量。")
        values = [value / norm for value in values]
    return values


def cosine_similarity(left: object, right: object) -> float:
    """计算两个同维有限向量的余弦相似度。"""
    left_values = normalize_vector(left)
    right_values = normalize_vector(
        right,
        expected_dimension=len(left_values),
    )
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(
        left_value * right_value
        for left_value, right_value in zip(
            left_values,
            right_values,
            strict=True,
        )
    ) / (left_norm * right_norm)


__all__ = [
    "BaseEmbedder",
    "cosine_similarity",
    "normalize_vector",
    "validate_text",
    "validate_text_batch",
]
