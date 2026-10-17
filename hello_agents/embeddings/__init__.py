"""可替换、可离线测试的文本 Embedding 层。"""

from .base import BaseEmbedder, cosine_similarity
from .config import EmbeddingConfig, EmbeddingProvider
from .factory import create_embedder
from .hashing import HashingEmbedder
from .sentence_transformer import LocalSentenceTransformerEmbedder


__all__ = [
    "BaseEmbedder",
    "EmbeddingConfig",
    "EmbeddingProvider",
    "HashingEmbedder",
    "LocalSentenceTransformerEmbedder",
    "cosine_similarity",
    "create_embedder",
]
