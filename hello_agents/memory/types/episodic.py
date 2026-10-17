"""SQLite 正文存储与向量检索组合的情景记忆。"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone

from ...core.exceptions import (
    EmbeddingError,
    MemorySystemError,
    VectorStoreError,
)
from ...embeddings import BaseEmbedder
from ...vector_stores import BaseVectorStore, VectorRecord
from ..base import (
    BaseMemory,
    MemoryConfig,
    MemoryItem,
    MemorySearchResult,
    utc_now,
)
from ..stores import SQLiteMemoryStore


class EpisodicMemory(BaseMemory):
    """持久保存具体事件，并通过向量相似度和时间近因性检索。"""

    def __init__(
        self,
        *,
        document_store: SQLiteMemoryStore,
        vector_store: BaseVectorStore,
        embedder: BaseEmbedder,
        user_id: str = "default",
        config: MemoryConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(document_store, SQLiteMemoryStore):
            raise MemorySystemError("document_store 必须是 SQLiteMemoryStore。")
        if not isinstance(vector_store, BaseVectorStore):
            raise MemorySystemError("vector_store 必须实现 BaseVectorStore。")
        if not isinstance(embedder, BaseEmbedder):
            raise MemorySystemError("embedder 必须实现 BaseEmbedder。")
        if not isinstance(user_id, str) or not user_id.strip():
            raise MemorySystemError("情景记忆用户 ID 不能为空。")
        if embedder.dimension != vector_store.dimension:
            raise MemorySystemError(
                "Embedding 与向量存储的维度不一致。"
            )

        self.document_store = document_store
        self.vector_store = vector_store
        self.embedder = embedder
        self.user_id = user_id.strip()
        self.config = config or MemoryConfig()
        self._clock = clock

    def add(self, item: MemoryItem) -> str:
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("情景记忆只能保存 MemoryItem。")
        if item.memory_type != "episodic":
            raise MemorySystemError("EpisodicMemory 只能保存 episodic 类型记忆。")

        snapshot = item.model_copy(deep=True)
        item_user_id = snapshot.metadata.get("user_id")
        if item_user_id is not None and item_user_id != self.user_id:
            raise MemorySystemError("不能写入其他用户的情景记忆。")
        snapshot.metadata["user_id"] = self.user_id

        try:
            vector = self.embedder.embed(snapshot.content)
            record = VectorRecord(
                id=snapshot.id,
                vector=vector,
                metadata=self._vector_metadata(snapshot),
            )
        except (EmbeddingError, VectorStoreError, ValueError) as error:
            raise MemorySystemError("情景记忆编码失败。") from error

        self.document_store.add(snapshot)
        try:
            self.vector_store.upsert([record])
        except (VectorStoreError, ValueError) as error:
            try:
                self.vector_store.delete([snapshot.id])
            except VectorStoreError:
                pass
            self.document_store.delete(snapshot.id)
            raise MemorySystemError("情景记忆向量写入失败，正文已回滚。") from error
        return snapshot.id

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float = 0.0,
    ) -> list[MemorySearchResult]:
        normalized_query = self._validate_retrieval(
            query,
            limit=limit,
            min_importance=min_importance,
        )
        candidate_limit = min(
            1000,
            limit * self.config.episodic_candidate_multiplier,
        )
        try:
            query_vector = self.embedder.embed(normalized_query)
            hits = self.vector_store.search(
                query_vector,
                limit=candidate_limit,
                filters={
                    "memory_type": "episodic",
                    "user_id": self.user_id,
                },
            )
        except (EmbeddingError, VectorStoreError) as error:
            raise MemorySystemError("情景记忆检索失败。") from error

        now = self._now()
        scored: list[tuple[float, MemoryItem]] = []
        for hit in hits:
            item = self.document_store.get(hit.id)
            if item is None:
                continue
            if item.metadata.get("user_id") != self.user_id:
                continue
            if item.memory_type != "episodic" or item.importance < min_importance:
                continue

            vector_score = max(0.0, min(1.0, hit.score))
            recency_score = self._recency_score(item.created_at, now)
            importance_weight = 0.8 + item.importance * 0.4
            combined_score = (
                vector_score * 0.8 + recency_score * 0.2
            ) * importance_weight
            scored.append((combined_score, item))

        scored.sort(
            key=lambda pair: (
                pair[0],
                pair[1].importance,
                pair[1].created_at,
                pair[1].id,
            ),
            reverse=True,
        )
        results: list[MemorySearchResult] = []
        for score, item in scored[:limit]:
            item.access_count += 1
            item.accessed_at = now
            self.document_store.update_access(
                item.id,
                accessed_at=now,
                access_count=item.access_count,
            )
            results.append(MemorySearchResult(item=item, score=score))
        return results

    def remove(self, memory_id: str) -> MemoryItem:
        item = self.document_store.get(memory_id)
        if item is None or item.metadata.get("user_id") != self.user_id:
            raise MemorySystemError(f"未找到记忆：{memory_id}。")
        try:
            self.vector_store.delete([item.id])
        except VectorStoreError as error:
            raise MemorySystemError("情景记忆向量删除失败。") from error
        if not self.document_store.delete(item.id):
            raise MemorySystemError("情景记忆正文删除失败。")
        return item

    def clear(self) -> int:
        items = self.list_items()
        if not items:
            return 0
        try:
            self.vector_store.delete([item.id for item in items])
        except VectorStoreError as error:
            raise MemorySystemError("情景记忆向量清理失败。") from error
        return self.document_store.clear(
            memory_type="episodic",
            user_id=self.user_id,
        )

    def list_items(self) -> list[MemoryItem]:
        return self.document_store.list_items(
            memory_type="episodic",
            user_id=self.user_id,
        )

    def __len__(self) -> int:
        return self.document_store.count(
            memory_type="episodic",
            user_id=self.user_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise MemorySystemError("情景记忆时钟必须返回带时区的 datetime。")
        return value.astimezone(timezone.utc)

    def _recency_score(self, created_at: datetime, now: datetime) -> float:
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        return max(
            0.1,
            math.exp(
                -age_days / self.config.episodic_recency_decay_days
            ),
        )

    def _validate_retrieval(
        self,
        query: object,
        *,
        limit: object,
        min_importance: object,
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

    @staticmethod
    def _vector_metadata(item: MemoryItem) -> dict[str, object]:
        metadata: dict[str, object] = dict(item.metadata)
        metadata.update(
            {
                "memory_id": item.id,
                "memory_type": item.memory_type,
                "importance": item.importance,
                "created_at": item.created_at.isoformat(),
            }
        )
        return metadata


__all__ = ["EpisodicMemory"]
