"""可用于离线开发与测试的进程内向量存储。"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any

from ..core.exceptions import EmbeddingError, VectorStoreError
from ..embeddings import cosine_similarity
from ..embeddings.base import normalize_vector
from .base import (
    BaseVectorStore,
    VectorRecord,
    VectorSearchResult,
    validate_dimension,
    validate_limit,
    validate_metadata,
)


class InMemoryVectorStore(BaseVectorStore):
    """使用精确余弦相似度扫描的小型向量存储。"""

    def __init__(self, dimension: int) -> None:
        self._dimension = validate_dimension(dimension)
        self._records: dict[str, VectorRecord] = {}
        self._lock = RLock()

    @property
    def dimension(self) -> int:
        return self._dimension

    def upsert(self, records: list[VectorRecord]) -> int:
        if not isinstance(records, list) or not records:
            raise VectorStoreError("upsert 至少需要一条向量记录。")
        if not all(isinstance(record, VectorRecord) for record in records):
            raise VectorStoreError("upsert 只能接收 VectorRecord。")
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise VectorStoreError("同一批次不能包含重复向量 ID。")
        for record in records:
            if len(record.vector) != self.dimension:
                raise VectorStoreError(
                    f"向量维度应为 {self.dimension}，实际为 {len(record.vector)}。"
                )

        with self._lock:
            for record in records:
                self._records[record.id] = record.model_copy(deep=True)
        return len(records)

    def search(
        self,
        query_vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        actual_limit = validate_limit(limit)
        try:
            normalized_query = normalize_vector(
                query_vector,
                expected_dimension=self.dimension,
            )
            normalized_filters = (
                validate_metadata(filters) if filters is not None else {}
            )
        except (EmbeddingError, ValueError) as error:
            raise VectorStoreError("向量检索参数无效。") from error

        with self._lock:
            candidates = [
                record.model_copy(deep=True)
                for record in self._records.values()
                if self._matches(record.metadata, normalized_filters)
            ]

        results = [
            VectorSearchResult(
                id=record.id,
                score=max(
                    -1.0,
                    min(
                        1.0,
                        cosine_similarity(normalized_query, record.vector),
                    ),
                ),
                metadata=deepcopy(record.metadata),
            )
            for record in candidates
        ]
        results.sort(key=lambda result: (result.score, result.id), reverse=True)
        return results[:actual_limit]

    def delete(self, record_ids: list[str]) -> None:
        normalized_ids = self._validate_ids(record_ids)
        with self._lock:
            for record_id in normalized_ids:
                self._records.pop(record_id, None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    @staticmethod
    def _matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
        return all(metadata.get(key) == value for key, value in filters.items())

    @staticmethod
    def _validate_ids(record_ids: object) -> list[str]:
        if not isinstance(record_ids, list) or not record_ids:
            raise VectorStoreError("delete 至少需要一个向量 ID。")
        normalized: list[str] = []
        for record_id in record_ids:
            if not isinstance(record_id, str) or not record_id.strip():
                raise VectorStoreError("向量 ID 必须是非空字符串。")
            normalized.append(record_id.strip())
        return normalized


__all__ = ["InMemoryVectorStore"]
