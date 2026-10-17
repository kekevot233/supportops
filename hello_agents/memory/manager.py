"""面向 Agent 的统一记忆管理入口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..core.exceptions import MemorySystemError
from .base import (
    BaseMemory,
    MemoryConfig,
    MemoryItem,
    MemorySearchResult,
    MemoryType,
    utc_now,
)
from .lifecycle import ForgettingPolicy
from .lifecycle_store import (
    LifecycleStep,
    LifecycleTask,
    SQLiteLifecycleStore,
)
from .types import WorkingMemory


_SUPPORTED_MEMORY_TYPES = frozenset(
    {"working", "episodic", "semantic", "perceptual"}
)


class MemoryManager:
    """根据记忆类型路由请求，屏蔽底层存储差异。"""

    def __init__(
        self,
        *,
        user_id: str = "default",
        config: MemoryConfig | None = None,
        backends: Mapping[str, BaseMemory] | None = None,
        lifecycle_store: SQLiteLifecycleStore | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise MemorySystemError("用户 ID 不能为空。")

        self.user_id = user_id.strip()
        self.config = config or MemoryConfig()
        self._clock = clock
        configured_backends: dict[str, BaseMemory] = (
            dict(backends)
            if backends is not None
            else {"working": WorkingMemory(self.config)}
        )
        if not configured_backends:
            raise MemorySystemError("至少需要配置一个记忆后端。")
        for memory_type, backend in configured_backends.items():
            if memory_type not in _SUPPORTED_MEMORY_TYPES:
                raise MemorySystemError(f"未知记忆类型：{memory_type}。")
            if not isinstance(backend, BaseMemory):
                raise MemorySystemError(
                    f"{memory_type} 后端必须实现 BaseMemory。"
                )
        self._backends = configured_backends
        if lifecycle_store is not None and not isinstance(
            lifecycle_store, SQLiteLifecycleStore
        ):
            raise MemorySystemError(
                "lifecycle_store 必须是 SQLiteLifecycleStore。"
            )
        self.lifecycle_store = lifecycle_store

    def add_memory(
        self,
        content: str,
        *,
        memory_type: MemoryType = "working",
        importance: float = 0.5,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """创建标准记忆，并交给对应后端保存。"""
        backend = self._get_backend(memory_type)
        if not isinstance(content, str) or not content.strip():
            raise MemorySystemError("记忆内容不能为空。")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise MemorySystemError("记忆元数据必须是映射。")

        owned_metadata = deepcopy(dict(metadata or {}))
        owned_metadata["user_id"] = self.user_id
        try:
            item = MemoryItem(
                content=content,
                memory_type=memory_type,
                importance=importance,
                metadata=owned_metadata,
            )
        except (TypeError, ValueError) as error:
            raise MemorySystemError("记忆参数无效。") from error
        return backend.add(item)

    def retrieve_memories(
        self,
        query: str,
        *,
        limit: int | None = None,
        memory_types: Sequence[MemoryType] | None = None,
        min_importance: float = 0.0,
    ) -> list[MemorySearchResult]:
        """跨指定后端检索，再进行统一排序与截断。"""
        actual_limit = (
            self.config.default_retrieval_limit if limit is None else limit
        )
        if (
            isinstance(actual_limit, bool)
            or not isinstance(actual_limit, int)
            or actual_limit <= 0
            or actual_limit > self.config.max_retrieval_limit
        ):
            raise MemorySystemError(
                "检索数量必须是正整数且不能超过配置上限。"
            )

        selected_types = self._normalize_memory_types(memory_types)
        results: list[MemorySearchResult] = []
        for memory_type in selected_types:
            backend = self._get_backend(memory_type)
            results.extend(
                backend.retrieve(
                    query,
                    limit=actual_limit,
                    min_importance=min_importance,
                )
            )
        results.sort(
            key=lambda result: (
                result.score,
                result.item.importance,
                result.item.created_at,
                result.item.id,
            ),
            reverse=True,
        )
        return results[:actual_limit]

    def remove_memory(
        self,
        memory_id: str,
        *,
        memory_type: MemoryType = "working",
    ) -> MemoryItem:
        """从指定后端删除记忆。"""
        return self._get_backend(memory_type).remove(memory_id)

    def clear(self, memory_type: MemoryType | None = None) -> int:
        """清空一个后端或当前配置的全部后端。"""
        if memory_type is not None:
            return self._get_backend(memory_type).clear()
        return sum(backend.clear() for backend in self._backends.values())

    def get_stats(self) -> dict[str, Any]:
        """返回不含具体记忆内容的运行状态。"""
        counts = {
            memory_type: len(backend)
            for memory_type, backend in sorted(self._backends.items())
        }
        return {
            "user_id": self.user_id,
            "total": sum(counts.values()),
            "by_type": counts,
        }

    def forget_memories(
        self,
        policy: ForgettingPolicy,
        *,
        memory_types: Sequence[MemoryType] | None = None,
        idempotency_key: str | None = None,
    ) -> list[MemoryItem]:
        """按策略选择并通过各后端公开删除接口遗忘记忆。"""
        if not isinstance(policy, ForgettingPolicy):
            raise MemorySystemError("遗忘策略必须是 ForgettingPolicy。")
        if self.lifecycle_store is not None:
            return self._forget_reliably(
                policy,
                memory_types=memory_types,
                idempotency_key=idempotency_key,
            )
        if idempotency_key is not None:
            raise MemorySystemError("使用幂等键必须配置 lifecycle_store。")
        now = self._now()
        removed: list[MemoryItem] = []
        for memory_type in self._normalize_memory_types(memory_types):
            backend = self._get_backend(memory_type)
            selected = policy.select(backend.list_items(), now=now)
            for item in selected:
                removed.append(backend.remove(item.id))
        return removed

    def consolidate_memories(
        self,
        *,
        source_type: MemoryType,
        target_type: MemoryType,
        min_importance: float = 0.7,
        limit: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        """把高价值记忆迁移到另一后端，并返回旧 ID 到新 ID 的映射。"""
        if source_type == target_type:
            raise MemorySystemError("记忆整合的来源类型和目标类型不能相同。")
        if (
            isinstance(min_importance, bool)
            or not isinstance(min_importance, (int, float))
            or not 0.0 <= float(min_importance) <= 1.0
        ):
            raise MemorySystemError("整合最低重要性必须在 0 到 1 之间。")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise MemorySystemError("整合数量必须是正整数或 None。")
        if self.lifecycle_store is not None:
            return self._consolidate_reliably(
                source_type=source_type,
                target_type=target_type,
                min_importance=float(min_importance),
                limit=limit,
                idempotency_key=idempotency_key,
            )
        if idempotency_key is not None:
            raise MemorySystemError("使用幂等键必须配置 lifecycle_store。")
        source = self._get_backend(source_type)
        target = self._get_backend(target_type)
        candidates = [
            item
            for item in source.list_items()
            if item.importance >= float(min_importance)
        ]
        candidates.sort(
            key=lambda item: (item.importance, item.created_at, item.id),
            reverse=True,
        )
        if limit is not None:
            candidates = candidates[:limit]

        migrated: dict[str, str] = {}
        for item in candidates:
            metadata = deepcopy(item.metadata)
            metadata.update(
                {
                    "user_id": self.user_id,
                    "consolidated_from": item.id,
                    "consolidated_from_type": source_type,
                }
            )
            promoted = MemoryItem(
                content=item.content,
                memory_type=target_type,
                importance=item.importance,
                created_at=item.created_at,
                accessed_at=item.accessed_at,
                access_count=item.access_count,
                metadata=metadata,
            )
            target.add(promoted)
            try:
                source.remove(item.id)
            except Exception as error:
                try:
                    target.remove(promoted.id)
                except Exception:
                    pass
                raise MemorySystemError(
                    "记忆整合删除来源失败，已尝试回滚目标记忆。"
                ) from error
            migrated[item.id] = promoted.id
        return migrated

    def reconcile_lifecycle_tasks(self, *, limit: int = 100) -> list[LifecycleTask]:
        """核对未完成任务的真实后端状态并尽力恢复。"""
        store = self._require_lifecycle_store()
        tasks = store.list_recoverable(user_id=self.user_id, limit=limit)
        reconciled = []
        for task in tasks:
            try:
                if task.operation == "forget":
                    self._resume_forget_task(task)
                else:
                    self._resume_consolidation_task(task)
            except MemorySystemError:
                pass
            refreshed = store.get(task.id)
            if refreshed is not None:
                reconciled.append(refreshed)
        return reconciled

    def list_lifecycle_tasks(self) -> list[LifecycleTask]:
        return self._require_lifecycle_store().list_tasks(user_id=self.user_id)

    def _forget_reliably(
        self,
        policy: ForgettingPolicy,
        *,
        memory_types: Sequence[MemoryType] | None,
        idempotency_key: str | None,
    ) -> list[MemoryItem]:
        selected_types = self._normalize_memory_types(memory_types)
        request = {
            "policy": policy.model_dump(mode="json"),
            "memory_types": selected_types,
        }
        task = self._existing_lifecycle_task(
            "forget", idempotency_key, request
        )
        if task is None:
            now = self._now()
            steps = []
            for memory_type in selected_types:
                backend = self._get_backend(memory_type)
                for item in policy.select(backend.list_items(), now=now):
                    steps.append(
                        LifecycleStep(
                            key=f"{memory_type}:{item.id}",
                            payload={
                                "memory_type": memory_type,
                                "source_item": item.model_dump(mode="json"),
                            },
                        )
                    )
            task = self._create_lifecycle_task(
                "forget", idempotency_key, request, steps
            )
        return self._resume_forget_task(task)

    def _consolidate_reliably(
        self,
        *,
        source_type: MemoryType,
        target_type: MemoryType,
        min_importance: float,
        limit: int | None,
        idempotency_key: str | None,
    ) -> dict[str, str]:
        request = {
            "source_type": source_type,
            "target_type": target_type,
            "min_importance": min_importance,
            "limit": limit,
        }
        task = self._existing_lifecycle_task(
            "consolidate", idempotency_key, request
        )
        if task is None:
            candidates = [
                item
                for item in self._get_backend(source_type).list_items()
                if item.importance >= min_importance
            ]
            candidates.sort(
                key=lambda item: (item.importance, item.created_at, item.id),
                reverse=True,
            )
            if limit is not None:
                candidates = candidates[:limit]
            task_id = uuid4().hex
            steps = []
            for item in candidates:
                target_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"hello-agents-lifecycle:{self.user_id}:{task_id}:{item.id}:{target_type}",
                    )
                )
                steps.append(
                    LifecycleStep(
                        key=f"{source_type}:{item.id}",
                        payload={
                            "source_type": source_type,
                            "target_type": target_type,
                            "source_item": item.model_dump(mode="json"),
                            "target_id": target_id,
                        },
                    )
                )
            task = self._create_lifecycle_task(
                "consolidate",
                idempotency_key,
                request,
                steps,
                task_id=task_id,
            )
        return self._resume_consolidation_task(task)

    def _resume_forget_task(self, task: LifecycleTask) -> list[MemoryItem]:
        if task.status == "completed":
            return [
                MemoryItem.model_validate(item)
                for item in (task.result or {}).get("removed", [])
            ]
        self._start_task_attempt(task)
        try:
            for step in task.steps:
                if step.status == "completed":
                    continue
                memory_type = cast(MemoryType, step.payload["memory_type"])
                source_item = MemoryItem.model_validate(step.payload["source_item"])
                backend = self._get_backend(memory_type)
                if self._find_item(backend, source_item.id) is not None:
                    backend.remove(source_item.id)
                step.status = "completed"
                step.result = {"memory_id": source_item.id}
                step.error_code = None
                self._save_task(task)
            removed = [step.payload["source_item"] for step in task.steps]
            task.result = {"removed": removed}
            self._complete_task(task)
            return [MemoryItem.model_validate(item) for item in removed]
        except Exception as error:
            self._fail_task(task, "forget_step_failed")
            raise MemorySystemError(
                "记忆遗忘任务未完成，已记录并可通过对账重试。"
            ) from error

    def _resume_consolidation_task(self, task: LifecycleTask) -> dict[str, str]:
        if task.status == "completed":
            return dict((task.result or {}).get("migrated", {}))
        self._start_task_attempt(task)
        try:
            for step in task.steps:
                if step.status == "completed":
                    continue
                payload = step.payload
                source_type = cast(MemoryType, payload["source_type"])
                target_type = cast(MemoryType, payload["target_type"])
                source = self._get_backend(source_type)
                target = self._get_backend(target_type)
                snapshot = MemoryItem.model_validate(payload["source_item"])
                target_id = str(payload["target_id"])
                source_item = self._find_item(source, snapshot.id)
                target_item = self._find_item(target, target_id)

                if target_item is None and source_item is None:
                    step.status = "failed"
                    step.error_code = "source_and_target_missing"
                    self._save_task(task)
                    raise MemorySystemError("整合来源与目标均不存在，无法自动修复。")
                if target_item is not None and (
                    target_item.metadata.get("lifecycle_task_id") != task.id
                    or target_item.metadata.get("consolidated_from") != snapshot.id
                ):
                    step.status = "failed"
                    step.error_code = "target_identity_conflict"
                    self._save_task(task)
                    raise MemorySystemError("整合目标 ID 已被其他数据占用。")
                if target_item is None:
                    metadata = deepcopy(snapshot.metadata)
                    metadata.update(
                        {
                            "user_id": self.user_id,
                            "consolidated_from": snapshot.id,
                            "consolidated_from_type": source_type,
                            "lifecycle_task_id": task.id,
                        }
                    )
                    promoted = MemoryItem(
                        id=target_id,
                        content=snapshot.content,
                        memory_type=target_type,
                        importance=snapshot.importance,
                        created_at=snapshot.created_at,
                        accessed_at=snapshot.accessed_at,
                        access_count=snapshot.access_count,
                        metadata=metadata,
                    )
                    target.add(promoted)
                    step.status = "target_written"
                    step.error_code = None
                    self._save_task(task)
                if source_item is not None:
                    source.remove(snapshot.id)
                step.status = "completed"
                step.result = {
                    "source_id": snapshot.id,
                    "target_id": target_id,
                }
                step.error_code = None
                self._save_task(task)
            migrated = {
                str(step.payload["source_item"]["id"]): str(step.payload["target_id"])
                for step in task.steps
            }
            task.result = {"migrated": migrated}
            self._complete_task(task)
            return migrated
        except Exception as error:
            self._fail_task(task, "consolidation_step_failed")
            raise MemorySystemError(
                "记忆整合任务未完成，已记录并可通过对账重试。"
            ) from error

    def _existing_lifecycle_task(
        self,
        operation: str,
        idempotency_key: str | None,
        request: dict[str, Any],
    ) -> LifecycleTask | None:
        if idempotency_key is None:
            return None
        key = self._normalize_idempotency_key(idempotency_key)
        task = self._require_lifecycle_store().get_by_key(self.user_id, key)
        if task is None:
            return None
        if task.operation != operation or task.request != request:
            raise MemorySystemError("幂等键已用于不同的生命周期请求。")
        return task

    def _create_lifecycle_task(
        self,
        operation: str,
        idempotency_key: str | None,
        request: dict[str, Any],
        steps: list[LifecycleStep],
        *,
        task_id: str | None = None,
    ) -> LifecycleTask:
        now = self._now()
        task = LifecycleTask(
            id=task_id or uuid4().hex,
            idempotency_key=(
                self._normalize_idempotency_key(idempotency_key)
                if idempotency_key is not None
                else uuid4().hex
            ),
            user_id=self.user_id,
            operation=operation,
            request=request,
            steps=steps,
            created_at=now,
            updated_at=now,
        )
        store = self._require_lifecycle_store()
        try:
            return store.create(task)
        except MemorySystemError:
            existing = store.get_by_key(self.user_id, task.idempotency_key)
            if (
                existing is not None
                and existing.operation == task.operation
                and existing.request == task.request
            ):
                return existing
            raise

    def _start_task_attempt(self, task: LifecycleTask) -> None:
        task.status = "running"
        task.attempts += 1
        task.error_code = None
        self._save_task(task)

    def _complete_task(self, task: LifecycleTask) -> None:
        task.status = "completed"
        task.error_code = None
        self._save_task(task)

    def _fail_task(self, task: LifecycleTask, error_code: str) -> None:
        task.status = "failed"
        task.error_code = error_code
        self._save_task(task)

    def _save_task(self, task: LifecycleTask) -> None:
        task.updated_at = self._now()
        self._require_lifecycle_store().save(task)

    def _require_lifecycle_store(self) -> SQLiteLifecycleStore:
        if self.lifecycle_store is None:
            raise MemorySystemError("尚未配置生命周期任务日志。")
        return self.lifecycle_store

    @staticmethod
    def _find_item(backend: BaseMemory, memory_id: str) -> MemoryItem | None:
        return next((item for item in backend.list_items() if item.id == memory_id), None)

    @staticmethod
    def _normalize_idempotency_key(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MemorySystemError("生命周期幂等键不能为空。")
        key = value.strip()
        if len(key) > 200:
            raise MemorySystemError("生命周期幂等键不能超过 200 个字符。")
        return key

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise MemorySystemError("记忆管理时钟必须返回带时区的 datetime。")
        return value.astimezone(timezone.utc)

    def _get_backend(self, memory_type: str) -> BaseMemory:
        if not isinstance(memory_type, str) or not memory_type.strip():
            raise MemorySystemError("记忆类型不能为空。")
        normalized_type = memory_type.strip().lower()
        if normalized_type not in _SUPPORTED_MEMORY_TYPES:
            raise MemorySystemError(f"未知记忆类型：{normalized_type}。")
        try:
            return self._backends[normalized_type]
        except KeyError as error:
            raise MemorySystemError(
                f"记忆类型尚未配置后端：{normalized_type}。"
            ) from error

    def _normalize_memory_types(
        self,
        memory_types: Sequence[MemoryType] | None,
    ) -> list[MemoryType]:
        if memory_types is None:
            return [cast(MemoryType, name) for name in self._backends]
        if isinstance(memory_types, (str, bytes)) or not isinstance(
            memory_types,
            Sequence,
        ):
            raise MemorySystemError("记忆类型列表必须是序列。")
        if not memory_types:
            raise MemorySystemError("记忆类型列表不能为空。")

        result: list[MemoryType] = []
        for memory_type in memory_types:
            self._get_backend(memory_type)
            if memory_type not in result:
                result.append(memory_type)
        return result


__all__ = ["MemoryManager"]
