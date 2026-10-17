"""容量受限、带过期策略的进程内工作记忆。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import RLock

from ...core.exceptions import MemorySystemError
from ..base import (
    BaseMemory,
    MemoryConfig,
    MemoryItem,
    MemorySearchResult,
    utc_now,
)


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """同时切分英文单词与中文单字，不依赖外部分词模型。"""
    return [token.lower() for token in _TOKEN_PATTERN.findall(text)]


class WorkingMemory(BaseMemory):
    """保存当前任务上下文，并按容量和 TTL 自动清理。"""

    def __init__(
        self,
        config: MemoryConfig | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config or MemoryConfig()
        self._clock = clock
        self._items: dict[str, MemoryItem] = {}
        self._lock = RLock()

    def add(self, item: MemoryItem) -> str:
        """保存独立快照；满容量时淘汰价值最低的记忆。"""
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("工作记忆只能保存 MemoryItem。")
        if item.memory_type != "working":
            raise MemorySystemError("WorkingMemory 只能保存 working 类型记忆。")

        with self._lock:
            now = self._now()
            self._purge_expired(now)
            if item.id in self._items:
                raise MemorySystemError(f"记忆 ID 已存在：{item.id}。")

            snapshot = item.model_copy(deep=True)
            if snapshot.expires_at is None:
                snapshot.expires_at = now + timedelta(
                    seconds=self.config.working_memory_ttl_seconds
                )

            if len(self._items) >= self.config.working_memory_capacity:
                eviction_id = min(
                    self._items,
                    key=lambda memory_id: self._eviction_key(
                        self._items[memory_id]
                    ),
                )
                del self._items[eviction_id]

            self._items[snapshot.id] = snapshot
            return snapshot.id

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float = 0.0,
    ) -> list[MemorySearchResult]:
        """以 TF-IDF、关键词覆盖、时效性和重要性综合排序。"""
        normalized_query = self._validate_retrieval(
            query,
            limit=limit,
            min_importance=min_importance,
        )
        query_tokens = _tokenize(normalized_query)

        with self._lock:
            now = self._now()
            self._purge_expired(now)
            candidates = [
                item
                for item in self._items.values()
                if item.importance >= min_importance
            ]
            if not candidates:
                return []

            document_tokens = [_tokenize(item.content) for item in candidates]
            document_frequency = Counter(
                token
                for tokens in document_tokens
                for token in set(tokens)
            )
            scored: list[tuple[float, MemoryItem]] = []
            for item, tokens in zip(candidates, document_tokens, strict=True):
                score = self._score(
                    query_tokens,
                    tokens,
                    document_frequency,
                    len(candidates),
                    item,
                    now,
                )
                if score > 0.0:
                    scored.append((score, item))

            scored.sort(
                key=lambda pair: (
                    pair[0],
                    pair[1].importance,
                    pair[1].created_at,
                    pair[1].id,
                ),
                reverse=True,
            )
            selected = scored[:limit]
            results: list[MemorySearchResult] = []
            for score, item in selected:
                item.access_count += 1
                item.accessed_at = now
                results.append(
                    MemorySearchResult(
                        item=item.model_copy(deep=True),
                        score=score,
                    )
                )
            return results

    def remove(self, memory_id: str) -> MemoryItem:
        """删除指定工作记忆。"""
        normalized_id = self._normalize_id(memory_id)
        with self._lock:
            self._purge_expired(self._now())
            try:
                item = self._items.pop(normalized_id)
            except KeyError as error:
                raise MemorySystemError(
                    f"未找到记忆：{normalized_id}。"
                ) from error
            return item.model_copy(deep=True)

    def clear(self) -> int:
        """清空所有工作记忆。"""
        with self._lock:
            count = len(self._items)
            self._items.clear()
            return count

    def list_items(self) -> list[MemoryItem]:
        """按创建时间返回有效记忆的深拷贝快照。"""
        with self._lock:
            self._purge_expired(self._now())
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    self._items.values(),
                    key=lambda value: (value.created_at, value.id),
                )
            ]

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired(self._now())
            return len(self._items)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise MemorySystemError("工作记忆时钟必须返回带时区的 datetime。")
        return value.astimezone(timezone.utc)

    def _purge_expired(self, now: datetime) -> None:
        expired_ids = [
            memory_id
            for memory_id, item in self._items.items()
            if item.expires_at is not None and item.expires_at <= now
        ]
        for memory_id in expired_ids:
            del self._items[memory_id]

    @staticmethod
    def _eviction_key(item: MemoryItem) -> tuple[float, int, datetime, str]:
        return (
            item.importance,
            item.access_count,
            item.accessed_at,
            item.id,
        )

    def _validate_retrieval(
        self,
        query: str,
        *,
        limit: int,
        min_importance: float,
    ) -> str:
        if not isinstance(query, str) or not query.strip():
            raise MemorySystemError("检索词不能为空。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise MemorySystemError("检索数量必须是正整数。")
        if limit > self.config.max_retrieval_limit:
            raise MemorySystemError(
                f"检索数量不能超过 {self.config.max_retrieval_limit}。"
            )
        if (
            isinstance(min_importance, bool)
            or not isinstance(min_importance, (int, float))
            or not 0.0 <= float(min_importance) <= 1.0
        ):
            raise MemorySystemError("最低重要性必须在 0 到 1 之间。")
        return query.strip()

    def _score(
        self,
        query_tokens: list[str],
        document_tokens: list[str],
        document_frequency: Counter[str],
        document_count: int,
        item: MemoryItem,
        now: datetime,
    ) -> float:
        query_counter = Counter(query_tokens)
        document_counter = Counter(document_tokens)
        vocabulary = set(query_counter) | set(document_counter)
        if not vocabulary:
            return 0.0

        def idf(token: str) -> float:
            return math.log(
                (document_count + 1) / (document_frequency[token] + 1)
            ) + 1.0

        query_vector = {
            token: query_counter[token] * idf(token) for token in vocabulary
        }
        document_vector = {
            token: document_counter[token] * idf(token) for token in vocabulary
        }
        dot_product = sum(
            query_vector[token] * document_vector[token]
            for token in vocabulary
        )
        query_norm = math.sqrt(sum(value**2 for value in query_vector.values()))
        document_norm = math.sqrt(
            sum(value**2 for value in document_vector.values())
        )
        cosine = (
            dot_product / (query_norm * document_norm)
            if query_norm and document_norm
            else 0.0
        )

        query_set = set(query_tokens)
        document_set = set(document_tokens)
        keyword_coverage = (
            len(query_set & document_set) / len(query_set) if query_set else 0.0
        )
        age_seconds = max(0.0, (now - item.created_at).total_seconds())
        time_decay = max(
            0.1,
            math.exp(-age_seconds / self.config.working_memory_ttl_seconds),
        )
        importance_factor = 0.8 + 0.4 * item.importance
        return (
            (0.7 * cosine + 0.3 * keyword_coverage)
            * time_decay
            * importance_factor
        )

    @staticmethod
    def _normalize_id(memory_id: str) -> str:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemorySystemError("记忆 ID 不能为空。")
        return memory_id.strip()


__all__ = ["WorkingMemory"]
