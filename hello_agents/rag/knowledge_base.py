"""组合 SQLite、Embedding、向量检索、重排和引用的文档知识库。"""

from __future__ import annotations

from ..core.exceptions import EmbeddingError, MemorySystemError, RAGError, VectorStoreError
from ..embeddings import BaseEmbedder
from ..memory import MemoryItem, SQLiteMemoryStore
from ..vector_stores import BaseVectorStore, VectorRecord
from .chunking import DocumentChunker
from .models import (
    Citation,
    Document,
    DocumentChunk,
    DocumentRAGResult,
    IngestionReport,
    RetrievedDocumentChunk,
)
from .rerankers import BaseReranker, HybridReranker


class DocumentKnowledgeBase:
    def __init__(
        self,
        *,
        document_store: SQLiteMemoryStore,
        vector_store: BaseVectorStore,
        embedder: BaseEmbedder,
        chunker: DocumentChunker | None = None,
        reranker: BaseReranker | None = None,
        user_id: str = "default",
        candidate_multiplier: int = 5,
    ) -> None:
        if not isinstance(document_store, SQLiteMemoryStore):
            raise RAGError("document_store 必须是 SQLiteMemoryStore。")
        if not isinstance(vector_store, BaseVectorStore):
            raise RAGError("vector_store 必须实现 BaseVectorStore。")
        if not isinstance(embedder, BaseEmbedder):
            raise RAGError("embedder 必须实现 BaseEmbedder。")
        if embedder.dimension != vector_store.dimension:
            raise RAGError("Embedding 与向量存储维度不一致。")
        if not isinstance(user_id, str) or not user_id.strip():
            raise RAGError("文档知识库用户 ID 不能为空。")
        if (
            isinstance(candidate_multiplier, bool)
            or not isinstance(candidate_multiplier, int)
            or not 1 <= candidate_multiplier <= 20
        ):
            raise RAGError("candidate_multiplier 必须在 1 到 20 之间。")
        self.document_store = document_store
        self.vector_store = vector_store
        self.embedder = embedder
        self.chunker = chunker or DocumentChunker()
        self.reranker = reranker or HybridReranker()
        if not isinstance(self.chunker, DocumentChunker):
            raise RAGError("chunker 必须是 DocumentChunker。")
        if not isinstance(self.reranker, BaseReranker):
            raise RAGError("reranker 必须实现 BaseReranker。")
        self.user_id = user_id.strip()
        self.candidate_multiplier = candidate_multiplier

    def ingest(self, document: Document, *, replace: bool = False) -> IngestionReport:
        if not isinstance(document, Document):
            raise RAGError("ingest() 只能接收 Document。")
        if not isinstance(replace, bool):
            raise RAGError("replace 必须是布尔值。")
        old_items = self._document_items(document.id)
        if old_items and not replace:
            raise RAGError(f"文档已存在：{document.id}。")
        chunks = self.chunker.chunk(document)
        if old_items and {item.id for item in old_items} == {chunk.id for chunk in chunks}:
            return IngestionReport(
                document_id=document.id,
                chunk_count=len(chunks),
                replaced_chunk_count=0,
                unchanged=True,
            )
        try:
            vectors = self.embedder.embed_batch([chunk.content for chunk in chunks])
            items = [self._to_item(chunk) for chunk in chunks]
            records = [
                VectorRecord(
                    id=chunk.id,
                    vector=vector,
                    metadata={
                        "record_kind": "document_chunk",
                        "user_id": self.user_id,
                        "document_id": document.id,
                        "chunk_index": chunk.chunk_index,
                    },
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        except (EmbeddingError, ValueError) as error:
            raise RAGError("文档切块向量化失败。") from error

        added_ids: list[str] = []
        try:
            for item in items:
                self.document_store.add(item)
                added_ids.append(item.id)
            self.vector_store.upsert(records)
        except (MemorySystemError, VectorStoreError) as error:
            self._rollback_new(added_ids)
            raise RAGError("文档摄取失败，已回滚新版本。") from error

        if old_items:
            old_ids = [item.id for item in old_items]
            try:
                self.vector_store.delete(old_ids)
            except VectorStoreError as error:
                self._rollback_new(added_ids)
                raise RAGError("文档旧版本清理失败，已回滚新版本。") from error
            for item in old_items:
                self.document_store.delete(item.id)
        return IngestionReport(
            document_id=document.id,
            chunk_count=len(chunks),
            replaced_chunk_count=len(old_items),
        )

    def retrieve(self, query: str, *, limit: int = 5) -> list[RetrievedDocumentChunk]:
        query = self._validate_query(query, limit)
        candidate_limit = min(1000, limit * self.candidate_multiplier)
        try:
            vector = self.embedder.embed(query)
            vector_hits = self.vector_store.search(
                vector,
                limit=candidate_limit,
                filters={"record_kind": "document_chunk", "user_id": self.user_id},
            )
        except (EmbeddingError, VectorStoreError) as error:
            raise RAGError("文档知识检索失败。") from error
        candidates = []
        for hit in vector_hits:
            item = self.document_store.get(hit.id)
            if item is None or item.metadata.get("user_id") != self.user_id:
                continue
            try:
                chunk = self._from_item(item)
            except (TypeError, ValueError, KeyError) as error:
                raise RAGError("文档块元数据损坏。") from error
            candidates.append(
                RetrievedDocumentChunk(
                    chunk=chunk,
                    vector_score=hit.score,
                    lexical_score=0.0,
                    score=max(0.0, hit.score),
                )
            )
        return self.reranker.rerank(query, candidates, limit=limit)

    def query(self, query: str, *, limit: int = 5) -> DocumentRAGResult:
        normalized = self._validate_query(query, limit)
        hits = self.retrieve(normalized, limit=limit)
        citations = [self._citation(hit.chunk, index) for index, hit in enumerate(hits, 1)]
        return DocumentRAGResult(query=normalized, hits=hits, citations=citations)

    def remove_document(self, document_id: str) -> int:
        document_id = self._normalize_id(document_id)
        items = self._document_items(document_id)
        if not items:
            return 0
        ids = [item.id for item in items]
        try:
            self.vector_store.delete(ids)
        except VectorStoreError as error:
            raise RAGError("文档向量删除失败。") from error
        for item in items:
            self.document_store.delete(item.id)
        return len(items)

    def list_documents(self) -> list[dict[str, object]]:
        grouped: dict[str, dict[str, object]] = {}
        for item in self._all_items():
            document_id = str(item.metadata["document_id"])
            entry = grouped.setdefault(
                document_id,
                {
                    "document_id": document_id,
                    "title": item.metadata["title"],
                    "source": item.metadata["source"],
                    "chunk_count": 0,
                },
            )
            entry["chunk_count"] = int(entry["chunk_count"]) + 1
        return [grouped[key] for key in sorted(grouped)]

    def _to_item(self, chunk: DocumentChunk) -> MemoryItem:
        return MemoryItem(
            id=chunk.id,
            content=chunk.content,
            memory_type="semantic",
            metadata={
                "record_kind": "document_chunk",
                "user_id": self.user_id,
                "document_id": chunk.document_id,
                "source": chunk.source,
                "title": chunk.title,
                "chunk_index": chunk.chunk_index,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "section": chunk.section,
                "page_number": chunk.page_number,
                "document_metadata": chunk.metadata,
            },
        )

    @staticmethod
    def _from_item(item: MemoryItem) -> DocumentChunk:
        metadata = item.metadata
        return DocumentChunk(
            id=item.id,
            document_id=metadata["document_id"],
            content=item.content,
            source=metadata["source"],
            title=metadata["title"],
            chunk_index=metadata["chunk_index"],
            start_char=metadata["start_char"],
            end_char=metadata["end_char"],
            section=metadata.get("section"),
            page_number=metadata.get("page_number"),
            metadata=metadata.get("document_metadata", {}),
        )

    def _all_items(self) -> list[MemoryItem]:
        return [
            item
            for item in self.document_store.list_items(memory_type="semantic", user_id=self.user_id)
            if item.metadata.get("record_kind") == "document_chunk"
        ]

    def _document_items(self, document_id: str) -> list[MemoryItem]:
        return [item for item in self._all_items() if item.metadata.get("document_id") == document_id]

    def _rollback_new(self, ids: list[str]) -> None:
        if ids:
            try:
                self.vector_store.delete(ids)
            except VectorStoreError:
                pass
            for item_id in ids:
                self.document_store.delete(item_id)

    @staticmethod
    def _citation(chunk: DocumentChunk, index: int) -> Citation:
        return Citation(
            label=f"D{index}",
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            source=chunk.source,
            title=chunk.title,
            chunk_index=chunk.chunk_index,
            section=chunk.section,
            page_number=chunk.page_number,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
        )

    @staticmethod
    def _normalize_id(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RAGError("文档 ID 不能为空。")
        return value.strip()

    @staticmethod
    def _validate_query(query: object, limit: object) -> str:
        if not isinstance(query, str) or not query.strip():
            raise RAGError("文档检索词不能为空。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise RAGError("文档检索数量必须在 1 到 100 之间。")
        return query.strip()


__all__ = ["DocumentKnowledgeBase"]
