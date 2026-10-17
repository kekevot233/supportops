"""基于 NoteTool 的长程任务检查点与恢复计划。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..core.exceptions import ContextEngineeringError
from ..tools.builtin.note import NoteTool


TaskStatus = Literal["pending", "running", "blocked", "completed", "failed"]


def _strip_step(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


StepText = Annotated[
    str,
    BeforeValidator(_strip_step),
    Field(min_length=1, max_length=500),
]
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_CHECKPOINT_PREFIX = "<!-- HELLO_AGENTS_TASK_CHECKPOINT_V1 -->\n```json\n"
_CHECKPOINT_SUFFIX = "\n```"


class TaskCheckpoint(BaseModel):
    """恢复长程任务所需的最小、可验证状态。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    task_id: str
    objective: str = Field(min_length=1, max_length=2000)
    status: TaskStatus = "pending"
    current_step: StepText | None = None
    completed_steps: list[StepText] = Field(default_factory=list, max_length=100)
    pending_steps: list[StepText] = Field(default_factory=list, max_length=100)
    blockers: list[StepText] = Field(default_factory=list, max_length=50)
    decisions: dict[str, str] = Field(default_factory=dict)
    artifacts: list[StepText] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=100)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _TASK_ID_PATTERN.fullmatch(value):
            raise ValueError("task_id 只能包含字母、数字、点、下划线和连字符。")
        return value

    @field_validator("objective", mode="before")
    @classmethod
    def normalize_objective(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "completed_steps",
        "pending_steps",
        "blockers",
        "artifacts",
        mode="after",
    )
    @classmethod
    def unique_text_list(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            stripped = item.strip()
            if stripped not in normalized:
                normalized.append(stripped)
        return normalized

    @field_validator("decisions")
    @classmethod
    def validate_decisions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("decisions 最多包含 100 项。")
        normalized: dict[str, str] = {}
        for key, decision in value.items():
            if not isinstance(key, str) or not key.strip() or len(key.strip()) > 100:
                raise ValueError("决策键必须是 1 到 100 字符的字符串。")
            if (
                not isinstance(decision, str)
                or not decision.strip()
                or len(decision.strip()) > 1000
            ):
                raise ValueError("决策内容必须是 1 到 1000 字符的字符串。")
            normalized[key.strip()] = decision.strip()
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(not isinstance(key, str) or not key.strip() for key in value):
            raise ValueError("metadata 的键必须是非空字符串。")
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("metadata 必须可以安全序列化为 JSON。") from error
        if len(encoded) > 20_000:
            raise ValueError("metadata 序列化后不能超过 20000 字符。")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("检查点时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "TaskCheckpoint":
        completed = set(self.completed_steps)
        pending = set(self.pending_steps)
        if completed & pending:
            raise ValueError("已完成步骤与待办步骤不能重叠。")
        if self.current_step in completed or self.current_step in pending:
            raise ValueError("当前步骤不能重复出现在已完成或待办列表中。")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked 状态必须记录至少一个阻塞原因。")
        if self.status == "completed" and (
            self.current_step is not None or self.pending_steps or self.blockers
        ):
            raise ValueError("completed 状态不能保留当前步骤、待办步骤或阻塞项。")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at。")
        return self


class TaskResumePlan(BaseModel):
    """从持久化检查点推导出的安全恢复建议。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    checkpoint_version: int = Field(ge=1)
    status: TaskStatus
    can_resume: bool
    next_step: str | None = None
    blockers: list[str] = Field(default_factory=list)
    reason: str


class TaskCheckpointStore:
    """通过结构化笔记保存任务状态，并提供版本与完整性校验。"""

    _FIXED_TAG = "long_horizon_checkpoint"
    _TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
        "pending": {"pending", "running", "blocked", "completed", "failed"},
        "running": {"running", "blocked", "completed", "failed"},
        "blocked": {"blocked", "running", "failed"},
        "failed": {"failed", "running"},
        "completed": {"completed"},
    }

    def __init__(self, note_tool: NoteTool) -> None:
        if not isinstance(note_tool, NoteTool):
            raise ContextEngineeringError("TaskCheckpointStore 需要 NoteTool。")
        self.note_tool = note_tool
        self._lock = threading.RLock()

    def create(
        self,
        task_id: str,
        objective: str,
        *,
        steps: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskCheckpoint:
        with self._lock:
            normalized_id = self._validate_task_id(task_id)
            if self._find_notes(normalized_id):
                raise ContextEngineeringError("任务检查点已经存在。")
            normalized_steps = self._normalize_steps(steps or [])
            now = datetime.now(timezone.utc)
            checkpoint = TaskCheckpoint(
                task_id=normalized_id,
                objective=objective,
                current_step=normalized_steps[0] if normalized_steps else None,
                pending_steps=normalized_steps[1:],
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
            try:
                self.note_tool.create_note(
                    title=f"任务检查点：{normalized_id}",
                    content=self._serialize(checkpoint),
                    note_type="task_state",
                    tags=[self._FIXED_TAG, self._task_tag(normalized_id)],
                )
            except Exception as error:
                raise ContextEngineeringError("任务检查点创建失败。") from error
            return checkpoint.model_copy(deep=True)

    def load(self, task_id: str) -> TaskCheckpoint:
        with self._lock:
            notes = self._find_notes(self._validate_task_id(task_id))
            if not notes:
                raise ContextEngineeringError("任务检查点不存在。")
            if len(notes) != 1:
                raise ContextEngineeringError("任务检查点不唯一，无法安全恢复。")
            try:
                record = self.note_tool.read_note(notes[0]["id"])
            except Exception as error:
                raise ContextEngineeringError("任务检查点读取失败。") from error
            checkpoint = self._deserialize(record.content)
            if checkpoint.task_id != task_id:
                raise ContextEngineeringError("任务检查点标识不一致。")
            return checkpoint

    def save(
        self,
        checkpoint: TaskCheckpoint,
        *,
        expected_version: int | None = None,
    ) -> TaskCheckpoint:
        if not isinstance(checkpoint, TaskCheckpoint):
            raise ContextEngineeringError("checkpoint 必须是 TaskCheckpoint。")
        with self._lock:
            validated = TaskCheckpoint.model_validate(checkpoint.model_dump())
            current = self.load(validated.task_id)
            expected = current.version if expected_version is None else expected_version
            if expected != current.version or validated.version != current.version:
                raise ContextEngineeringError("检查点版本冲突；请重新加载最新状态。")
            if validated.created_at != current.created_at:
                raise ContextEngineeringError("检查点 created_at 不允许修改。")
            if validated.status not in self._TRANSITIONS[current.status]:
                raise ContextEngineeringError(
                    f"不允许从 {current.status} 转换为 {validated.status}。"
                )
            updated_data = validated.model_dump()
            updated_data["version"] = current.version + 1
            updated_data["updated_at"] = datetime.now(timezone.utc)
            updated = TaskCheckpoint.model_validate(updated_data)
            notes = self._find_notes(updated.task_id)
            if len(notes) != 1:
                raise ContextEngineeringError("任务检查点不唯一，无法安全更新。")
            try:
                self.note_tool.update_note(
                    notes[0]["id"],
                    content=self._serialize(updated),
                )
            except Exception as error:
                raise ContextEngineeringError("任务检查点保存失败。") from error
            return updated.model_copy(deep=True)

    def mark_current_step_completed(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
    ) -> TaskCheckpoint:
        with self._lock:
            current = self.load(task_id)
            if current.status in {"blocked", "failed", "completed"}:
                raise ContextEngineeringError("当前任务状态不允许完成步骤。")
            if current.current_step is None:
                raise ContextEngineeringError("当前没有可完成的步骤。")
            completed = [*current.completed_steps, current.current_step]
            next_step = current.pending_steps[0] if current.pending_steps else None
            pending = current.pending_steps[1:] if current.pending_steps else []
            status: TaskStatus = "running" if next_step else "completed"
            updated = current.model_copy(
                update={
                    "completed_steps": completed,
                    "current_step": next_step,
                    "pending_steps": pending,
                    "status": status,
                    "blockers": [],
                },
                deep=True,
            )
            return self.save(updated, expected_version=expected_version)

    def block(
        self,
        task_id: str,
        blockers: list[str],
        *,
        expected_version: int | None = None,
    ) -> TaskCheckpoint:
        with self._lock:
            current = self.load(task_id)
            if current.status in {"completed", "failed"}:
                raise ContextEngineeringError("终态任务不能标记为 blocked。")
            normalized = self._normalize_steps(blockers)
            if not normalized:
                raise ContextEngineeringError("阻塞原因不能为空。")
            updated = current.model_copy(
                update={"status": "blocked", "blockers": normalized},
                deep=True,
            )
            return self.save(updated, expected_version=expected_version)

    def resume_blocked(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
    ) -> TaskCheckpoint:
        with self._lock:
            current = self.load(task_id)
            if current.status != "blocked":
                raise ContextEngineeringError("只有 blocked 任务可以解除阻塞。")
            updated = current.model_copy(
                update={"status": "running", "blockers": []},
                deep=True,
            )
            return self.save(updated, expected_version=expected_version)

    def plan_resume(self, task_id: str) -> TaskResumePlan:
        checkpoint = self.load(task_id)
        if checkpoint.status == "completed":
            return TaskResumePlan(
                task_id=task_id,
                checkpoint_version=checkpoint.version,
                status=checkpoint.status,
                can_resume=False,
                reason="任务已经完成，无需恢复。",
            )
        if checkpoint.status == "blocked":
            return TaskResumePlan(
                task_id=task_id,
                checkpoint_version=checkpoint.version,
                status=checkpoint.status,
                can_resume=False,
                next_step=checkpoint.current_step,
                blockers=list(checkpoint.blockers),
                reason="必须先解决记录的阻塞项。",
            )
        if checkpoint.current_step is None:
            return TaskResumePlan(
                task_id=task_id,
                checkpoint_version=checkpoint.version,
                status=checkpoint.status,
                can_resume=False,
                reason="检查点没有可执行的当前步骤。",
            )
        return TaskResumePlan(
            task_id=task_id,
            checkpoint_version=checkpoint.version,
            status=checkpoint.status,
            can_resume=True,
            next_step=checkpoint.current_step,
            reason="已通过版本和摘要校验，可以从当前步骤继续。",
        )

    def list_tasks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContextEngineeringError("limit 必须是 1 到 100 之间的整数。")
        try:
            notes = self.note_tool.list_notes(
                limit=limit,
                note_type="task_state",
                tags=[self._FIXED_TAG],
            )
        except Exception as error:
            raise ContextEngineeringError("任务检查点列表读取失败。") from error
        tasks = []
        for note in notes:
            checkpoint = self._deserialize(self.note_tool.read_note(note["id"]).content)
            tasks.append(
                {
                    "task_id": checkpoint.task_id,
                    "objective": checkpoint.objective,
                    "status": checkpoint.status,
                    "current_step": checkpoint.current_step,
                    "version": checkpoint.version,
                    "updated_at": checkpoint.updated_at.isoformat(),
                }
            )
        return tasks

    def _find_notes(self, task_id: str) -> list[dict[str, Any]]:
        try:
            return self.note_tool.list_notes(
                limit=100,
                note_type="task_state",
                tags=[self._task_tag(task_id)],
            )
        except Exception as error:
            raise ContextEngineeringError("任务检查点索引查询失败。") from error

    def _serialize(self, checkpoint: TaskCheckpoint) -> str:
        state = checkpoint.model_dump(mode="json")
        canonical = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        envelope = {
            "schema_version": 1,
            "state_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "checkpoint": state,
        }
        payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
        return f"{_CHECKPOINT_PREFIX}{payload}{_CHECKPOINT_SUFFIX}"

    def _deserialize(self, content: str) -> TaskCheckpoint:
        if not content.startswith(_CHECKPOINT_PREFIX) or not content.endswith(
            _CHECKPOINT_SUFFIX
        ):
            raise ContextEngineeringError("任务检查点格式无效。")
        raw = content[len(_CHECKPOINT_PREFIX) : -len(_CHECKPOINT_SUFFIX)]
        try:
            envelope = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            if set(envelope) != {"schema_version", "state_sha256", "checkpoint"}:
                raise ValueError("envelope fields")
            if envelope["schema_version"] != 1:
                raise ValueError("schema version")
            state = envelope["checkpoint"]
            canonical = json.dumps(
                state,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(actual, envelope["state_sha256"]):
                raise ValueError("digest mismatch")
            return TaskCheckpoint.model_validate(state)
        except ContextEngineeringError:
            raise
        except Exception as error:
            raise ContextEngineeringError("任务检查点校验失败，拒绝恢复。") from error

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        try:
            return TaskCheckpoint(
                task_id=task_id,
                objective="validate",
            ).task_id
        except Exception as error:
            raise ContextEngineeringError("task_id 格式无效。") from error

    @staticmethod
    def _normalize_steps(steps: list[str]) -> list[str]:
        if not isinstance(steps, list) or len(steps) > 100:
            raise ContextEngineeringError("步骤必须是最多 100 项的字符串列表。")
        result: list[str] = []
        for step in steps:
            if not isinstance(step, str) or not step.strip() or len(step.strip()) > 500:
                raise ContextEngineeringError("每个步骤必须是 1 到 500 字符的字符串。")
            normalized = step.strip()
            if normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _task_tag(task_id: str) -> str:
        return f"task:{task_id}"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


__all__ = [
    "TaskCheckpoint",
    "TaskCheckpointStore",
    "TaskResumePlan",
    "TaskStatus",
]
