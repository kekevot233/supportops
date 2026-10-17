"""知识图谱存储接口与内置后端。"""

from .base import BaseGraphStore, GraphEntity, GraphRelation, GraphSearchResult
from .memory import InMemoryGraphStore
from .neo4j import Neo4jGraphStore


__all__ = [
    "BaseGraphStore",
    "GraphEntity",
    "GraphRelation",
    "GraphSearchResult",
    "InMemoryGraphStore",
    "Neo4jGraphStore",
]
