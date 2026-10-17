"""Qdrant 向量数据库适配器。"""

from __future__ import annotations

import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..core.exceptions import EmbeddingError, VectorStoreError
from ..embeddings.base import normalize_vector
from .base import (
    BaseVectorStore,
    VectorRecord,
    VectorSearchResult,
    validate_dimension,
    validate_limit,
    validate_metadata,
)


_COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_ORIGINAL_ID_KEY = "_hello_agents_record_id"


class QdrantVectorStore(BaseVectorStore):
    """通过官方 qdrant-client 管理一个余弦距离集合。"""

    def __init__(
        self,
        *,
        collection_name: str,
        dimension: int,
        url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        models_module: Any | None = None,
    ) -> None:
        if (
            not isinstance(collection_name, str)
            or not _COLLECTION_PATTERN.fullmatch(collection_name.strip())
        ):
            raise VectorStoreError(
                "Qdrant 集合名只能包含字母、数字、下划线和连字符。"
            )
        if client is None and (
            not isinstance(url, str) or not url.strip()
        ):
            raise VectorStoreError("未注入客户端时必须提供 Qdrant URL。")

        self.collection_name = collection_name.strip()
        self._dimension = validate_dimension(dimension)
        self.url = url.strip() if isinstance(url, str) else None
        self.api_key = api_key
        self._client = client
        self._models_module = models_module
        self._collection_ready = False

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

        models = self._get_models()
        points = []
        for record in records:
            try:
                vector = normalize_vector(
                    record.vector,
                    expected_dimension=self.dimension,
                )
            except EmbeddingError as error:
                raise VectorStoreError("待写入向量的维度无效。") from error
            payload = dict(record.metadata)
            payload[_ORIGINAL_ID_KEY] = record.id
            points.append(
                models.PointStruct(
                    id=self._point_id(record.id),
                    vector=vector,
                    payload=payload,
                )
            )

        self._ensure_collection()
        try:
            self._get_client().upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )
        except Exception as error:
            raise VectorStoreError("Qdrant 向量写入失败。") from error
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
            vector = normalize_vector(
                query_vector,
                expected_dimension=self.dimension,
            )
            normalized_filters = (
                validate_metadata(filters) if filters is not None else {}
            )
        except (EmbeddingError, ValueError) as error:
            raise VectorStoreError("向量检索参数无效。") from error
        models = self._get_models()
        query_filter = None
        if normalized_filters:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value),
                    )
                    for key, value in normalized_filters.items()
                ]
            )

        self._ensure_collection()
        try:
            response = self._get_client().query_points(
                collection_name=self.collection_name,
                query=vector,
                query_filter=query_filter,
                limit=actual_limit,
                with_payload=True,
            )
            points = response.points
        except Exception as error:
            raise VectorStoreError("Qdrant 向量检索失败。") from error

        results: list[VectorSearchResult] = []
        for point in points:
            score = max(-1.0, min(1.0, float(point.score)))
            payload = dict(point.payload or {})
            original_id = str(payload.pop(_ORIGINAL_ID_KEY, point.id))
            results.append(
                VectorSearchResult(
                    id=original_id,
                    score=score,
                    metadata=payload,
                )
            )
        return results

    def delete(self, record_ids: list[str]) -> None:
        normalized_ids = self._validate_ids(record_ids)
        models = self._get_models()
        self._ensure_collection()
        try:
            self._get_client().delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(
                    points=[self._point_id(record_id) for record_id in normalized_ids]
                ),
                wait=True,
            )
        except Exception as error:
            raise VectorStoreError("Qdrant 向量删除失败。") from error

    def clear(self) -> None:
        self._ensure_collection()
        try:
            self._get_client().delete_collection(self.collection_name)
            self._collection_ready = False
            self._ensure_collection()
        except VectorStoreError:
            raise
        except Exception as error:
            raise VectorStoreError("Qdrant 集合清空失败。") from error

    def count(self) -> int:
        self._ensure_collection()
        try:
            response = self._get_client().count(
                collection_name=self.collection_name,
                exact=True,
            )
            return int(response.count)
        except Exception as error:
            raise VectorStoreError("Qdrant 向量计数失败。") from error

    def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        client = self._get_client()
        models = self._get_models()
        try:
            exists = client.collection_exists(self.collection_name)
            if not exists:
                client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.dimension,
                        distance=models.Distance.COSINE,
                    ),
                )
        except Exception as error:
            raise VectorStoreError("Qdrant 集合初始化失败。") from error
        self._collection_ready = True

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient
        except ImportError as error:
            raise VectorStoreError(
                "使用 Qdrant 需要安装 qdrant-client。"
            ) from error
        try:
            self._client = QdrantClient(
                url=self.url,
                api_key=self.api_key,
            )
        except Exception as error:
            raise VectorStoreError("Qdrant 客户端初始化失败。") from error
        return self._client

    def _get_models(self) -> Any:
        if self._models_module is not None:
            return self._models_module
        try:
            from qdrant_client import models
        except ImportError as error:
            raise VectorStoreError(
                "使用 Qdrant 需要安装 qdrant-client。"
            ) from error
        self._models_module = models
        return models

    @staticmethod
    def _validate_ids(record_ids: object) -> list[str]:
        if not isinstance(record_ids, list) or not record_ids:
            raise VectorStoreError("delete 至少需要一个向量 ID。")
        result: list[str] = []
        for record_id in record_ids:
            if not isinstance(record_id, str) or not record_id.strip():
                raise VectorStoreError("向量 ID 必须是非空字符串。")
            result.append(record_id.strip())
        return result

    def _point_id(self, record_id: str) -> str:
        """把任意框架 ID 稳定映射为 Qdrant 接受的 UUID。"""
        return str(
            uuid5(
                NAMESPACE_URL,
                f"hello-agents:{self.collection_name}:{record_id}",
            )
        )


__all__ = ["QdrantVectorStore"]
