"""组合向量检索与知识图谱检索的语义记忆。"""

from __future__ import annotations

from ...core.exceptions import (
    EmbeddingError,
    GraphStoreError,
    MemorySystemError,
    VectorStoreError,
)
from ...embeddings import BaseEmbedder
from ...graph_stores import BaseGraphStore
from ...vector_stores import BaseVectorStore, VectorRecord
from ..base import BaseMemory, MemoryConfig, MemoryItem, MemorySearchResult, utc_now
from ..extractors import BaseKnowledgeExtractor, MetadataKnowledgeExtractor
from ..quality import KnowledgeGraphValidator
from ..stores import SQLiteMemoryStore


class SemanticMemory(BaseMemory):
    def __init__(
        self,
        *,
        document_store: SQLiteMemoryStore,
        vector_store: BaseVectorStore,
        graph_store: BaseGraphStore,
        embedder: BaseEmbedder,
        extractor: BaseKnowledgeExtractor | None = None,
        quality_validator: KnowledgeGraphValidator | None = None,
        minimum_quality_score: float = 0.0,
        user_id: str = "default",
        config: MemoryConfig | None = None,
    ) -> None:
        if not isinstance(document_store, SQLiteMemoryStore):
            raise MemorySystemError("document_store 必须是 SQLiteMemoryStore。")
        if not isinstance(vector_store, BaseVectorStore):
            raise MemorySystemError("vector_store 必须实现 BaseVectorStore。")
        if not isinstance(graph_store, BaseGraphStore):
            raise MemorySystemError("graph_store 必须实现 BaseGraphStore。")
        if not isinstance(embedder, BaseEmbedder):
            raise MemorySystemError("embedder 必须实现 BaseEmbedder。")
        if embedder.dimension != vector_store.dimension:
            raise MemorySystemError("Embedding 与向量存储的维度不一致。")
        if not isinstance(user_id, str) or not user_id.strip():
            raise MemorySystemError("语义记忆用户 ID 不能为空。")
        actual_extractor = extractor or MetadataKnowledgeExtractor()
        if not isinstance(actual_extractor, BaseKnowledgeExtractor):
            raise MemorySystemError("extractor 必须实现 BaseKnowledgeExtractor。")
        actual_validator = quality_validator or KnowledgeGraphValidator()
        if not isinstance(actual_validator, KnowledgeGraphValidator):
            raise MemorySystemError(
                "quality_validator 必须是 KnowledgeGraphValidator。"
            )
        if (
            isinstance(minimum_quality_score, bool)
            or not isinstance(minimum_quality_score, (int, float))
            or not 0.0 <= float(minimum_quality_score) <= 1.0
        ):
            raise MemorySystemError("minimum_quality_score 必须在 0 到 1 之间。")
        self.document_store = document_store
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.embedder = embedder
        self.extractor = actual_extractor
        self.quality_validator = actual_validator
        self.minimum_quality_score = float(minimum_quality_score)
        self.user_id = user_id.strip()
        self.config = config or MemoryConfig()

    def add(self, item: MemoryItem) -> str:
        if not isinstance(item, MemoryItem) or item.memory_type != "semantic":
            raise MemorySystemError("SemanticMemory 只能保存 semantic MemoryItem。")
        snapshot = item.model_copy(deep=True)
        owner = snapshot.metadata.get("user_id")
        if owner is not None and owner != self.user_id:
            raise MemorySystemError("不能写入其他用户的语义记忆。")
        snapshot.metadata["user_id"] = self.user_id
        try:
            vector = self.embedder.embed(snapshot.content)
            knowledge = self.extractor.extract(snapshot, user_id=self.user_id)
            quality = self.quality_validator.validate(
                knowledge,
                expected_user_id=self.user_id,
                expected_memory_id=snapshot.id,
            )
            if not quality.valid:
                issue_codes = ", ".join(
                    sorted({issue.code for issue in quality.issues})
                )
                raise MemorySystemError(
                    f"知识图谱质量校验失败：{issue_codes}。"
                )
            if quality.score < self.minimum_quality_score:
                raise MemorySystemError(
                    "知识图谱质量分数低于允许的最低值。"
                )
            record = VectorRecord(
                id=snapshot.id,
                vector=vector,
                metadata={
                    "memory_id": snapshot.id,
                    "memory_type": "semantic",
                    "user_id": self.user_id,
                    "importance": snapshot.importance,
                },
            )
        except (EmbeddingError, MemorySystemError, ValueError) as error:
            raise MemorySystemError("语义记忆编码或知识提取失败。") from error

        self.document_store.add(snapshot)
        try:
            self.vector_store.upsert([record])
            self.graph_store.upsert(knowledge.entities, knowledge.relations)
        except (VectorStoreError, GraphStoreError) as error:
            self._rollback(snapshot.id)
            raise MemorySystemError("语义记忆写入失败，已执行回滚。") from error
        return snapshot.id

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float = 0.0,
    ) -> list[MemorySearchResult]:
        query = self._validate_retrieval(query, limit, min_importance)
        candidate_limit = min(
            1000, limit * self.config.episodic_candidate_multiplier
        )
        try:
            vector_hits = self.vector_store.search(
                self.embedder.embed(query),
                limit=candidate_limit,
                filters={"memory_type": "semantic", "user_id": self.user_id},
            )
            graph_hits = self.graph_store.search(
                query, user_id=self.user_id, limit=candidate_limit
            )
        except (EmbeddingError, VectorStoreError, GraphStoreError) as error:
            raise MemorySystemError("语义记忆检索失败。") from error

        scores: dict[str, dict[str, float]] = {}
        for hit in vector_hits:
            scores.setdefault(hit.id, {"vector": 0.0, "graph": 0.0})[
                "vector"
            ] = max(0.0, min(1.0, hit.score))
        for hit in graph_hits:
            scores.setdefault(
                hit.memory_id, {"vector": 0.0, "graph": 0.0}
            )["graph"] = hit.score

        ranked: list[tuple[float, MemoryItem]] = []
        for memory_id, components in scores.items():
            item = self.document_store.get(memory_id)
            if item is None or item.metadata.get("user_id") != self.user_id:
                continue
            if item.memory_type != "semantic" or item.importance < min_importance:
                continue
            base_score = components["vector"] * 0.7 + components["graph"] * 0.3
            score = base_score * (0.8 + item.importance * 0.4)
            ranked.append((score, item))
        ranked.sort(
            key=lambda pair: (pair[0], pair[1].importance, pair[1].id),
            reverse=True,
        )
        now = utc_now()
        results = []
        for score, item in ranked[:limit]:
            item.access_count += 1
            item.accessed_at = now
            self.document_store.update_access(
                item.id, accessed_at=now, access_count=item.access_count
            )
            results.append(MemorySearchResult(item=item, score=score))
        return results

    def remove(self, memory_id: str) -> MemoryItem:
        item = self.document_store.get(memory_id)
        if item is None or item.metadata.get("user_id") != self.user_id:
            raise MemorySystemError(f"未找到记忆：{memory_id}。")
        try:
            self.vector_store.delete([item.id])
            self.graph_store.delete_memory(item.id, user_id=self.user_id)
        except (VectorStoreError, GraphStoreError) as error:
            raise MemorySystemError("语义记忆索引删除失败。") from error
        self.document_store.delete(item.id)
        return item

    def clear(self) -> int:
        items = self.list_items()
        if not items:
            return 0
        try:
            self.vector_store.delete([item.id for item in items])
            self.graph_store.clear_user(self.user_id)
        except (VectorStoreError, GraphStoreError) as error:
            raise MemorySystemError("语义记忆索引清理失败。") from error
        return self.document_store.clear(
            memory_type="semantic", user_id=self.user_id
        )

    def list_items(self) -> list[MemoryItem]:
        return self.document_store.list_items(
            memory_type="semantic", user_id=self.user_id
        )

    def __len__(self) -> int:
        return self.document_store.count(
            memory_type="semantic", user_id=self.user_id
        )

    def _rollback(self, memory_id: str) -> None:
        try:
            self.vector_store.delete([memory_id])
        except VectorStoreError:
            pass
        try:
            self.graph_store.delete_memory(memory_id, user_id=self.user_id)
        except GraphStoreError:
            pass
        self.document_store.delete(memory_id)

    def _validate_retrieval(
        self, query: object, limit: object, min_importance: object
    ) -> str:
        if not isinstance(query, str) or not query.strip():
            raise MemorySystemError("检索词不能为空。")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > self.config.max_retrieval_limit
        ):
            raise MemorySystemError("检索数量必须是配置上限内的正整数。")
        if (
            isinstance(min_importance, bool)
            or not isinstance(min_importance, (int, float))
            or not 0.0 <= float(min_importance) <= 1.0
        ):
            raise MemorySystemError("最低重要性必须在 0 到 1 之间。")
        return query.strip()


__all__ = ["SemanticMemory"]
