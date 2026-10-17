"""零依赖、确定性的特征哈希文本向量器。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from ..core.exceptions import EmbeddingError
from .base import BaseEmbedder, normalize_vector, validate_text


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class HashingEmbedder(BaseEmbedder):
    """使用稳定特征哈希生成本地稀疏向量，适合离线基线与测试。"""

    def __init__(self, dimension: int = 384, *, normalize: bool = True) -> None:
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            raise EmbeddingError("Embedding 维度必须是整数。")
        if not 1 <= dimension <= 16384:
            raise EmbeddingError("Embedding 维度必须在 1 到 16384 之间。")
        if not isinstance(normalize, bool):
            raise EmbeddingError("normalize 必须是布尔值。")
        self._dimension = dimension
        self.normalize = normalize

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        normalized_text = validate_text(text)
        tokens = [
            token.lower()
            for token in _TOKEN_PATTERN.findall(normalized_text)
        ]
        if not tokens:
            raise EmbeddingError("待编码文本不包含可识别的词项。")

        vector = [0.0] * self.dimension
        for token, count in Counter(tokens).items():
            digest = hashlib.blake2b(
                token.encode("utf-8"),
                digest_size=16,
                person=b"hello-agents",
            ).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))

        if not any(vector):
            fallback_digest = hashlib.blake2b(
                normalized_text.encode("utf-8"),
                digest_size=8,
                person=b"hello-agents",
            ).digest()
            fallback_index = (
                int.from_bytes(fallback_digest, "big") % self.dimension
            )
            vector[fallback_index] = 1.0

        return normalize_vector(
            vector,
            expected_dimension=self.dimension,
            l2_normalize=self.normalize,
        )


__all__ = ["HashingEmbedder"]
