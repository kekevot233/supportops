"""跨存储记忆生命周期任务的 SQLite 幂等日志。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.exceptions import MemorySystemError
from ..vector_stores.base import validate_metadata


LifecycleOperation = Literal["forget", "consolidate"]
LifecycleTaskStatus = Literal["pending", "running", "completed", "failed"]
LifecycleStepStatus = Literal[
    "pending", "target_written", "completed", "failed"
]


class LifecycleStep(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    key: str = Field(min_length=1)
    status: LifecycleStepStatus = "pending"
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None


class LifecycleTask(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    operation: LifecycleOperation
    status: LifecycleTaskStatus = "pending"
    request: dict[str, Any]
    steps: list[LifecycleStep] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error_code: str | None = None
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("生命周期任务时间必须包含时区。")
        return value


class SQLiteLifecycleStore:
    """使用单行 JSON 检查点持久化每个生命周期任务。"""

    def __init__(self, database_path: str | Path) -> None:
        path_text = str(database_path).strip() if isinstance(database_path, (str, Path)) else ""
        if not path_text:
            raise MemorySystemError("生命周期日志数据库路径不能为空。")
        if path_text != ":memory:":
            Path(path_text).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(path_text, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期日志数据库连接失败。") from error
        self.database_path = path_text
        self._lock = RLock()
        self._closed = False
        self._initialize()

    def create(self, task: LifecycleTask) -> LifecycleTask:
        if not isinstance(task, LifecycleTask):
            raise MemorySystemError("生命周期日志只能保存 LifecycleTask。")
        values = self._values(task)
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO lifecycle_tasks (
                        id, idempotency_key, user_id, operation, status,
                        request_json, steps_json, result_json, error_code,
                        attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
        except sqlite3.IntegrityError as error:
            raise MemorySystemError("生命周期幂等键已经存在。") from error
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期任务日志写入失败。") from error
        return task.model_copy(deep=True)

    def save(self, task: LifecycleTask) -> None:
        if not isinstance(task, LifecycleTask):
            raise MemorySystemError("生命周期日志只能保存 LifecycleTask。")
        values = self._values(task)
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(
                    """
                    UPDATE lifecycle_tasks SET
                        idempotency_key = ?, user_id = ?, operation = ?, status = ?,
                        request_json = ?, steps_json = ?, result_json = ?, error_code = ?,
                        attempts = ?, created_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*values[1:], values[0]),
                )
                if cursor.rowcount == 0:
                    raise MemorySystemError("生命周期任务不存在。")
        except MemorySystemError:
            raise
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期任务日志更新失败。") from error

    def get(self, task_id: str) -> LifecycleTask | None:
        return self._get_one("id", self._normalize_text(task_id, "任务 ID"))

    def get_by_key(self, user_id: str, idempotency_key: str) -> LifecycleTask | None:
        user = self._normalize_text(user_id, "用户 ID")
        key = self._normalize_text(idempotency_key, "幂等键")
        try:
            with self._lock:
                self._require_open()
                row = self._connection.execute(
                    "SELECT * FROM lifecycle_tasks WHERE user_id = ? AND idempotency_key = ?",
                    (user, key),
                ).fetchone()
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期任务日志读取失败。") from error
        return self._row(row) if row is not None else None

    def list_recoverable(self, *, user_id: str, limit: int = 100) -> list[LifecycleTask]:
        user = self._normalize_text(user_id, "用户 ID")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise MemorySystemError("对账任务数量必须在 1 到 1000 之间。")
        try:
            with self._lock:
                self._require_open()
                rows = self._connection.execute(
                    """
                    SELECT * FROM lifecycle_tasks
                    WHERE user_id = ? AND status IN ('pending', 'running', 'failed')
                    ORDER BY created_at ASC, id ASC LIMIT ?
                    """,
                    (user, limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期待对账任务读取失败。") from error
        return [self._row(row) for row in rows]

    def list_tasks(self, *, user_id: str) -> list[LifecycleTask]:
        user = self._normalize_text(user_id, "用户 ID")
        try:
            with self._lock:
                self._require_open()
                rows = self._connection.execute(
                    "SELECT * FROM lifecycle_tasks WHERE user_id = ? ORDER BY created_at, id",
                    (user,),
                ).fetchall()
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期任务列表读取失败。") from error
        return [self._row(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _get_one(self, field: str, value: str) -> LifecycleTask | None:
        try:
            with self._lock:
                self._require_open()
                row = self._connection.execute(
                    f"SELECT * FROM lifecycle_tasks WHERE {field} = ?",
                    (value,),
                ).fetchone()
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期任务日志读取失败。") from error
        return self._row(row) if row is not None else None

    def _initialize(self) -> None:
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_tasks (
                        id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        result_json TEXT,
                        error_code TEXT,
                        attempts INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, idempotency_key)
                    )
                    """
                )
        except sqlite3.Error as error:
            raise MemorySystemError("生命周期日志表初始化失败。") from error

    def _values(self, task: LifecycleTask) -> tuple[object, ...]:
        return (
            task.id,
            task.idempotency_key,
            task.user_id,
            task.operation,
            task.status,
            self._json(task.request),
            self._json([step.model_dump(mode="json") for step in task.steps]),
            self._json(task.result) if task.result is not None else None,
            task.error_code,
            task.attempts,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
        )

    @staticmethod
    def _json(value: object) -> str:
        try:
            if isinstance(value, dict):
                value = validate_metadata(value)
            return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise MemorySystemError("生命周期任务数据必须可序列化为 JSON。") from error

    @staticmethod
    def _row(row: sqlite3.Row) -> LifecycleTask:
        try:
            return LifecycleTask(
                id=row["id"],
                idempotency_key=row["idempotency_key"],
                user_id=row["user_id"],
                operation=row["operation"],
                status=row["status"],
                request=json.loads(row["request_json"]),
                steps=json.loads(row["steps_json"]),
                result=json.loads(row["result_json"]) if row["result_json"] else None,
                error_code=row["error_code"],
                attempts=row["attempts"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MemorySystemError("生命周期任务日志数据损坏。") from error

    def _require_open(self) -> None:
        if self._closed:
            raise MemorySystemError("生命周期日志数据库已经关闭。")

    @staticmethod
    def _normalize_text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MemorySystemError(f"{label}不能为空。")
        return value.strip()


def lifecycle_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "LifecycleOperation",
    "LifecycleStep",
    "LifecycleStepStatus",
    "LifecycleTask",
    "LifecycleTaskStatus",
    "SQLiteLifecycleStore",
]
