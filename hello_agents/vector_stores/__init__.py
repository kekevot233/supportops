"""HelloAgents 向量存储抽象与内置适配器。"""

from .base import BaseVectorStore, VectorRecord, VectorSearchResult
from .memory import InMemoryVectorStore
from .qdrant import QdrantVectorStore


__all__ = [
    "BaseVectorStore",
    "InMemoryVectorStore",
    "QdrantVectorStore",
    "VectorRecord",
    "VectorSearchResult",
]
