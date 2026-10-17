"""复用框架切块、特征哈希、向量召回和重排；索引条目可随版本持久化。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from hello_agents import (
    Citation, Document, DocumentChunk, DocumentChunker, DocumentRAGResult, HashingEmbedder,
    HybridReranker, InMemoryVectorStore, RetrievedDocumentChunk, VectorRecord,
)
from hello_agents.embeddings import BaseEmbedder
from hello_agents.embeddings.base import normalize_vector


class IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk: DocumentChunk
    vector: list[float]


class DocumentIndex(Protocol):
    profile: str
    dimension: int
    embedding_id: str
    embedding_revision: str

    def build(self, document: Document) -> list[IndexEntry]: ...
    def query(self, question: str, entries: list[IndexEntry], *, limit: int) -> DocumentRAGResult: ...


class LocalDocumentIndex:
    # 标识包含编码实现版本、维度与切块参数；更换编码器需要新 profile 并重建。
    profile = "hashing-blake2b-v1-384-chunk700-overlap80"
    dimension = 384
    embedding_id = "hashing-blake2b"
    embedding_revision = "v1"

    def __init__(self):
        self.embedder = HashingEmbedder(384)
        self.chunker = DocumentChunker(chunk_size=700, chunk_overlap=80)
        self.reranker = HybridReranker()

    def build(self, document: Document) -> list[IndexEntry]:
        chunks = self.chunker.chunk(document)
        vectors = self.embedder.embed_batch([chunk.content for chunk in chunks])
        return [IndexEntry(chunk=chunk, vector=normalize_vector(vector, expected_dimension=self.dimension))
                for chunk, vector in zip(chunks, vectors, strict=True)]

    def query(self, question: str, entries: list[IndexEntry], *, limit: int) -> DocumentRAGResult:
        if not entries:
            return DocumentRAGResult(query=question)
        store = InMemoryVectorStore(self.dimension)
        store.upsert([VectorRecord(id=entry.chunk.id, vector=entry.vector) for entry in entries])
        chunks = {entry.chunk.id: entry.chunk for entry in entries}
        query_vector = normalize_vector(self.embedder.embed(question), expected_dimension=self.dimension)
        recalled = store.search(query_vector, limit=min(len(entries), limit * 5))
        candidates = [RetrievedDocumentChunk(chunk=chunks[row.id], vector_score=row.score,
                                              lexical_score=0.0, score=max(0.0, row.score)) for row in recalled]
        hits = self.reranker.rerank(question, candidates, limit=limit)
        citations = [Citation(label=f"D{number}", **{
            field: getattr(hit.chunk, field) for field in (
                "document_id", "source", "title", "chunk_index", "section", "page_number",
                "start_char", "end_char")}, chunk_id=hit.chunk.id) for number, hit in enumerate(hits, 1)]
        return DocumentRAGResult(query=question, hits=hits, citations=citations)


class EmbeddedDocumentIndex(LocalDocumentIndex):
    """接受已显式创建的编码器；模型身份/版本改变必须使用新 profile 并重建索引。"""

    def __init__(self, embedder: BaseEmbedder, *, embedding_id: str, embedding_revision: str,
                 chunk_size=700, chunk_overlap=80, vector_weight=0.75):
        if not isinstance(embedder, BaseEmbedder):
            raise ValueError("需要显式注入 BaseEmbedder。")
        for identifier in (embedding_id, embedding_revision):
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", identifier):
                raise ValueError("模型身份和版本只能使用非敏感标识符。")
        if type(embedder.dimension) is not int or not 1 <= embedder.dimension <= 16384:
            raise ValueError("编码器维度无效。")
        self.embedder, self.dimension = embedder, embedder.dimension
        self.embedding_id, self.embedding_revision = embedding_id, embedding_revision
        self.chunker = DocumentChunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        self.reranker = HybridReranker(vector_weight=vector_weight)
        settings = {"implementation": "supportops-embedded-index-v1", "metric": "cosine",
                    "embedding_id": embedding_id, "embedding_revision": embedding_revision,
                    "dimension": self.dimension, "chunk_size": self.chunker.chunk_size,
                    "chunk_overlap": self.chunker.chunk_overlap, "vector_weight": self.reranker.vector_weight,
                    "candidate_multiplier": 5}
        digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()
        self.profile = "embedded-v1-" + digest


__all__ = ["DocumentIndex", "IndexEntry", "LocalDocumentIndex", "EmbeddedDocumentIndex"]
