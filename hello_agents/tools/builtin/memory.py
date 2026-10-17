"""供 Agent 主动写入和检索记忆的内置工具。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ...core.exceptions import MemorySystemError, ToolError
from ...memory import ForgettingPolicy, MemoryManager, MemoryType
from ..base import Tool, ToolParameter


class MemoryTool(Tool):
    """把统一记忆管理器暴露为标准 Tool。"""

    def __init__(self, manager: MemoryManager | None = None) -> None:
        super().__init__(
            name="memory",
            description=(
                "添加、检索、删除、遗忘、整合记忆，或管理生命周期任务。"
            ),
        )
        self.manager = manager or MemoryManager()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "操作名称：add、search、remove、forget、consolidate、"
                    "reconcile、lifecycle_status 或 stats"
                ),
            ),
            ToolParameter(
                name="content",
                type="string",
                description="add 操作要保存的记忆内容",
                required=False,
            ),
            ToolParameter(
                name="query",
                type="string",
                description="search 操作的检索词",
                required=False,
            ),
            ToolParameter(
                name="memory_type",
                type="string",
                description=(
                    "记忆类型：working、episodic、semantic 或 perceptual；"
                    "需配置对应后端"
                ),
                required=False,
                default="working",
            ),
            ToolParameter(
                name="importance",
                type="number",
                description="记忆重要性，取值 0 到 1",
                required=False,
                default=0.5,
            ),
            ToolParameter(
                name="metadata",
                type="object",
                description=(
                    "可选上下文；语义记忆可通过 entities 和 relations "
                    "提供结构化知识"
                ),
                required=False,
            ),
            ToolParameter(
                name="file_path",
                type="string",
                description="perceptual add 操作关联的图像、音频或文档路径",
                required=False,
            ),
            ToolParameter(
                name="modality",
                type="string",
                description="perceptual add 操作的显式模态；省略时按扩展名推断",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description=(
                    "search/consolidate 的数量上限，或 reconcile 单次处理的任务数"
                ),
                required=False,
                default=5,
            ),
            ToolParameter(
                name="idempotency_key",
                type="string",
                description=(
                    "forget/consolidate 的可选幂等键；需配置生命周期日志"
                ),
                required=False,
            ),
            ToolParameter(
                name="memory_id",
                type="string",
                description="remove 操作要删除的记忆 ID",
                required=False,
            ),
            ToolParameter(
                name="min_importance",
                type="number",
                description="search 操作接受的最低重要性，取值 0 到 1",
                required=False,
                default=0.0,
            ),
            ToolParameter(
                name="strategy",
                type="string",
                description="forget 策略：importance、time、capacity 或 smart",
                required=False,
            ),
            ToolParameter(
                name="threshold",
                type="number",
                description="importance 或 smart 遗忘策略的阈值",
                required=False,
                default=0.2,
            ),
            ToolParameter(
                name="max_age_days",
                type="number",
                description="time/smart 策略使用的最大保留天数",
                required=False,
                default=90.0,
            ),
            ToolParameter(
                name="capacity",
                type="integer",
                description="capacity 策略允许保留的最大数量",
                required=False,
                default=1000,
            ),
            ToolParameter(
                name="max_removals",
                type="integer",
                description="单次 forget 最多删除多少条；省略表示不限制",
                required=False,
            ),
            ToolParameter(
                name="target_memory_type",
                type="string",
                description="consolidate 操作的目标记忆类型",
                required=False,
            ),
            ToolParameter(
                name="consolidation_threshold",
                type="number",
                description="consolidate 接受的最低重要性",
                required=False,
                default=0.7,
            ),
        ]

    def _run(self, parameters: dict[str, Any]) -> str:
        action = parameters["action"].strip().lower()
        try:
            if action == "add":
                return self._add(parameters)
            if action == "search":
                return self._search(parameters)
            if action == "remove":
                return self._remove(parameters)
            if action == "forget":
                return self._forget(parameters)
            if action == "consolidate":
                return self._consolidate(parameters)
            if action == "reconcile":
                return self._reconcile(parameters)
            if action == "lifecycle_status":
                return self._lifecycle_status()
            if action == "stats":
                return self._stats()
        except MemorySystemError as error:
            raise ToolError(f"记忆操作失败：{error}") from error
        raise ToolError(
            "memory.action 必须是 add、search、remove、forget、consolidate、"
            "reconcile、lifecycle_status 或 stats。"
        )

    def _add(self, parameters: dict[str, Any]) -> str:
        memory_type = self._memory_type(parameters["memory_type"])
        content = parameters.get("content")
        file_path = parameters.get("file_path")
        modality = parameters.get("modality")
        if file_path is not None:
            if memory_type != "perceptual":
                raise ToolError("file_path 只适用于 perceptual 记忆。")
            if not isinstance(file_path, str) or not file_path.strip():
                raise ToolError("file_path 必须是非空字符串。")
            file_path = file_path.strip()
        if modality is not None:
            if memory_type != "perceptual":
                raise ToolError("modality 只适用于 perceptual 记忆。")
            if not isinstance(modality, str) or not modality.strip():
                raise ToolError("modality 必须是非空字符串。")
            modality = modality.strip()
        if not isinstance(content, str) or not content.strip():
            if memory_type == "perceptual" and file_path:
                filename = Path(file_path).name
                if not filename:
                    raise ToolError("file_path 必须包含文件名。")
                content = f"感知文件：{filename}"
            else:
                raise ToolError("add 操作必须提供非空 content。")
        raw_metadata = parameters.get("metadata")
        if raw_metadata is not None and not isinstance(raw_metadata, Mapping):
            raise ToolError("metadata 必须是对象。")
        metadata = dict(raw_metadata or {})
        metadata.setdefault("source", "memory_tool")
        if file_path is not None:
            metadata["raw_data"] = file_path
        if modality is not None:
            metadata["modality"] = modality
        memory_id = self.manager.add_memory(
            content.strip(),
            memory_type=memory_type,
            importance=float(parameters["importance"]),
            metadata=metadata,
        )
        return f"已添加 {memory_type} 记忆，ID：{memory_id}。"

    def _search(self, parameters: dict[str, Any]) -> str:
        query = parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("search 操作必须提供非空 query。")
        memory_type = self._memory_type(parameters["memory_type"])
        results = self.manager.retrieve_memories(
            query,
            limit=parameters["limit"],
            memory_types=[memory_type],
            min_importance=float(parameters["min_importance"]),
        )
        if not results:
            return "没有找到相关记忆。"

        lines = [f"找到 {len(results)} 条相关记忆："]
        for index, result in enumerate(results, start=1):
            lines.append(
                f"{index}. [{result.item.memory_type}] {result.item.content} "
                f"(ID: {result.item.id}, 相关度: {result.score:.3f}, "
                f"重要性: {result.item.importance:.2f})"
            )
        return "\n".join(lines)

    def _remove(self, parameters: dict[str, Any]) -> str:
        memory_id = parameters.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ToolError("remove 操作必须提供非空 memory_id。")
        memory_type = self._memory_type(parameters["memory_type"])
        removed = self.manager.remove_memory(
            memory_id,
            memory_type=memory_type,
        )
        return f"已删除 {removed.memory_type} 记忆，ID：{removed.id}。"

    def _forget(self, parameters: dict[str, Any]) -> str:
        strategy = parameters.get("strategy")
        if not isinstance(strategy, str) or not strategy.strip():
            raise ToolError("forget 操作必须提供 strategy。")
        memory_type = self._memory_type(parameters["memory_type"])
        threshold = float(parameters["threshold"])
        policy = ForgettingPolicy(
            strategy=strategy.strip().lower(),
            importance_threshold=threshold,
            retention_threshold=threshold,
            max_age_days=float(parameters["max_age_days"]),
            capacity=parameters["capacity"],
            max_removals=parameters.get("max_removals"),
        )
        removed = self.manager.forget_memories(
            policy,
            memory_types=[memory_type],
            idempotency_key=parameters.get("idempotency_key"),
        )
        return f"已从 {memory_type} 遗忘 {len(removed)} 条记忆。"

    def _consolidate(self, parameters: dict[str, Any]) -> str:
        source_type = self._memory_type(parameters["memory_type"])
        raw_target = parameters.get("target_memory_type")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ToolError("consolidate 操作必须提供 target_memory_type。")
        target_type = self._memory_type(raw_target)
        migrated = self.manager.consolidate_memories(
            source_type=source_type,
            target_type=target_type,
            min_importance=float(parameters["consolidation_threshold"]),
            limit=parameters["limit"],
            idempotency_key=parameters.get("idempotency_key"),
        )
        return (
            f"已将 {len(migrated)} 条 {source_type} 记忆整合为 "
            f"{target_type} 记忆。"
        )

    def _reconcile(self, parameters: dict[str, Any]) -> str:
        tasks = self.manager.reconcile_lifecycle_tasks(
            limit=parameters["limit"],
        )
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return json.dumps(
            {"processed": len(tasks), "by_status": counts},
            ensure_ascii=False,
            sort_keys=True,
        )

    def _lifecycle_status(self) -> str:
        tasks = self.manager.list_lifecycle_tasks()
        by_status: dict[str, int] = {}
        by_operation: dict[str, int] = {}
        for task in tasks:
            by_status[task.status] = by_status.get(task.status, 0) + 1
            by_operation[task.operation] = by_operation.get(task.operation, 0) + 1
        return json.dumps(
            {
                "total": len(tasks),
                "by_status": by_status,
                "by_operation": by_operation,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _stats(self) -> str:
        return json.dumps(
            self.manager.get_stats(),
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _memory_type(value: Any) -> MemoryType:
        if not isinstance(value, str):
            raise ToolError("memory_type 必须是字符串。")
        normalized = value.strip().lower()
        if normalized not in {
            "working",
            "episodic",
            "semantic",
            "perceptual",
        }:
            raise ToolError(f"未知记忆类型：{normalized}。")
        return cast(MemoryType, normalized)


__all__ = ["MemoryTool"]
