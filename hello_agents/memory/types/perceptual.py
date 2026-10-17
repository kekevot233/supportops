"""支持模态分离向量空间的感知记忆。"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from ...core.exceptions import (
    EmbeddingError,
    MemorySystemError,
    VectorStoreError,
)
from ...vector_stores import BaseVectorStore, VectorRecord
from ..base import (
    BaseMemory,
    MemoryConfig,
    MemoryItem,
    MemorySearchResult,
    utc_now,
)
from ..modalities import BaseModalityEncoder, infer_modality, normalize_modality
from ..stores import SQLiteMemoryStore


class PerceptualMemory(BaseMemory):
    """保存文本或媒体感知，并在每种模态自己的向量空间中检索。"""

    def __init__(
        self,
        *,
        document_store: SQLiteMemoryStore,
        encoders: Mapping[str, BaseModalityEncoder],
        vector_stores: Mapping[str, BaseVectorStore],
        user_id: str = "default",
        config: MemoryConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(document_store, SQLiteMemoryStore):
            raise MemorySystemError("document_store 必须是 SQLiteMemoryStore。")
        if not isinstance(encoders, Mapping) or not encoders:
            raise MemorySystemError("感知记忆至少需要一个模态编码器。")
        if not isinstance(vector_stores, Mapping) or not vector_stores:
            raise MemorySystemError("感知记忆至少需要一个模态向量存储。")
        if not isinstance(user_id, str) or not user_id.strip():
            raise MemorySystemError("感知记忆用户 ID 不能为空。")

        normalized_encoders: dict[str, BaseModalityEncoder] = {}
        for key, encoder in encoders.items():
            modality = normalize_modality(key)
            if not isinstance(encoder, BaseModalityEncoder):
                raise MemorySystemError(
                    "encoders 必须全部实现 BaseModalityEncoder。"
                )
            if modality != encoder.modality:
                raise MemorySystemError("编码器配置键必须与编码器模态一致。")
            if modality in normalized_encoders:
                raise MemorySystemError(f"模态编码器重复配置：{modality}。")
            normalized_encoders[modality] = encoder

        normalized_stores: dict[str, BaseVectorStore] = {}
        for key, store in vector_stores.items():
            modality = normalize_modality(key)
            if not isinstance(store, BaseVectorStore):
                raise MemorySystemError(
                    "vector_stores 必须全部实现 BaseVectorStore。"
                )
            if modality in normalized_stores:
                raise MemorySystemError(f"模态向量存储重复配置：{modality}。")
            normalized_stores[modality] = store

        if set(normalized_encoders) != set(normalized_stores):
            raise MemorySystemError("每个感知模态必须同时配置编码器和向量存储。")
        for modality, encoder in normalized_encoders.items():
            if encoder.dimension != normalized_stores[modality].dimension:
                raise MemorySystemError(
                    f"{modality} 编码器与向量存储的维度不一致。"
                )

        self.document_store = document_store
        self.encoders = normalized_encoders
        self.vector_stores = normalized_stores
        self.user_id = user_id.strip()
        self.config = config or MemoryConfig()
        self._clock = clock

    def add(self, item: MemoryItem) -> str:
        if not isinstance(item, MemoryItem) or item.memory_type != "perceptual":
            raise MemorySystemError("PerceptualMemory 只能保存 perceptual MemoryItem。")
        snapshot = item.model_copy(deep=True)
        owner = snapshot.metadata.get("user_id")
        if owner is not None and owner != self.user_id:
            raise MemorySystemError("不能写入其他用户的感知记忆。")
        snapshot.metadata["user_id"] = self.user_id
        modality = self._item_modality(snapshot)
        snapshot.metadata["modality"] = modality

        encoder = self.encoders.get(modality)
        store = self.vector_stores.get(modality)
        if encoder is None or store is None:
            raise MemorySystemError(f"尚未配置 {modality} 感知后端。")
        try:
            record = VectorRecord(
                id=snapshot.id,
                vector=encoder.encode_item(snapshot),
                metadata=self._vector_metadata(snapshot),
            )
        except (EmbeddingError, MemorySystemError, ValueError) as error:
            raise MemorySystemError("感知记忆编码失败。") from error

        self.document_store.add(snapshot)
        try:
            store.upsert([record])
        except (VectorStoreError, ValueError) as error:
            try:
                store.delete([snapshot.id])
            except VectorStoreError:
                pass
            self.document_store.delete(snapshot.id)
            raise MemorySystemError("感知记忆向量写入失败，正文已回滚。") from error
        return snapshot.id

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float = 0.0,
        target_modality: str | None = None,
        query_modality: str = "text",
    ) -> list[MemorySearchResult]:
        normalized_query = self._validate_retrieval(
            query,
            limit=limit,
            min_importance=min_importance,
        )
        source_modality = normalize_modality(query_modality)
        targets = self._target_modalities(target_modality)
        candidate_limit = min(
            1000,
            limit * self.config.perceptual_candidate_multiplier,
        )

        hits_by_id: dict[str, tuple[float, str]] = {}
        try:
            for modality in targets:
                query_vector = self.encoders[modality].encode_query(
                    normalized_query,
                    query_modality=source_modality,
                )
                hits = self.vector_stores[modality].search(
                    query_vector,
                    limit=candidate_limit,
                    filters={
                        "memory_type": "perceptual",
                        "user_id": self.user_id,
                        "modality": modality,
                    },
                )
                for hit in hits:
                    current = hits_by_id.get(hit.id)
                    if current is None or hit.score > current[0]:
                        hits_by_id[hit.id] = (hit.score, modality)
        except (EmbeddingError, MemorySystemError, VectorStoreError) as error:
            raise MemorySystemError("感知记忆检索失败。") from error

        now = self._now()
        ranked: list[tuple[float, MemoryItem]] = []
        for memory_id, (raw_score, modality) in hits_by_id.items():
            item = self.document_store.get(memory_id)
            if item is None or item.metadata.get("user_id") != self.user_id:
                continue
            if item.memory_type != "perceptual":
                continue
            if item.metadata.get("modality") != modality:
                continue
            if item.importance < min_importance:
                continue
            vector_score = max(0.0, min(1.0, raw_score))
            recency_score = self._recency_score(item.created_at, now)
            importance_weight = 0.8 + item.importance * 0.4
            combined = (
                vector_score * 0.8 + recency_score * 0.2
            ) * importance_weight
            ranked.append((combined, item))

        ranked.sort(
            key=lambda pair: (
                pair[0],
                pair[1].importance,
                pair[1].created_at,
                pair[1].id,
            ),
            reverse=True,
        )
        results: list[MemorySearchResult] = []
        for score, item in ranked[:limit]:
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
        if (
            item is None
            or item.memory_type != "perceptual"
            or item.metadata.get("user_id") != self.user_id
        ):
            raise MemorySystemError(f"未找到记忆：{memory_id}。")
        modality = self._stored_modality(item)
        try:
            self.vector_stores[modality].delete([item.id])
        except VectorStoreError as error:
            raise MemorySystemError("感知记忆向量删除失败。") from error
        if not self.document_store.delete(item.id):
            raise MemorySystemError("感知记忆正文删除失败。")
        return item

    def clear(self) -> int:
        items = self.list_items()
        if not items:
            return 0
        grouped: defaultdict[str, list[str]] = defaultdict(list)
        for item in items:
            grouped[self._stored_modality(item)].append(item.id)
        try:
            for modality, memory_ids in grouped.items():
                self.vector_stores[modality].delete(memory_ids)
        except VectorStoreError as error:
            raise MemorySystemError("感知记忆向量清理失败。") from error
        return self.document_store.clear(
            memory_type="perceptual",
            user_id=self.user_id,
        )

    def list_items(self) -> list[MemoryItem]:
        return self.document_store.list_items(
            memory_type="perceptual",
            user_id=self.user_id,
        )

    def __len__(self) -> int:
        return self.document_store.count(
            memory_type="perceptual",
            user_id=self.user_id,
        )

    def _item_modality(self, item: MemoryItem) -> str:
        raw_data = item.metadata.get("raw_data")
        explicit = item.metadata.get("modality")
        if explicit is not None and not isinstance(explicit, str):
            raise MemorySystemError("感知记忆 modality 必须是字符串。")
        if raw_data is not None and not isinstance(raw_data, str):
            raise MemorySystemError("感知记忆 raw_data 必须是文件路径字符串。")
        return infer_modality(raw_data, explicit=explicit)

    def _stored_modality(self, item: MemoryItem) -> str:
        modality = normalize_modality(item.metadata.get("modality"))
        if modality not in self.vector_stores:
            raise MemorySystemError(f"尚未配置 {modality} 感知后端。")
        return modality

    def _target_modalities(self, target_modality: str | None) -> list[str]:
        if target_modality is None:
            return list(self.encoders)
        modality = normalize_modality(target_modality)
        if modality not in self.encoders:
            raise MemorySystemError(f"尚未配置 {modality} 感知后端。")
        return [modality]

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise MemorySystemError("感知记忆时钟必须返回带时区的 datetime。")
        return value.astimezone(timezone.utc)

    def _recency_score(self, created_at: datetime, now: datetime) -> float:
        age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
        return max(
            0.1,
            math.exp(
                -age_hours / self.config.perceptual_recency_decay_hours
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
                "memory_type": "perceptual",
                "importance": item.importance,
                "created_at": item.created_at.isoformat(),
            }
        )
        return metadata


__all__ = ["PerceptualMemory"]
