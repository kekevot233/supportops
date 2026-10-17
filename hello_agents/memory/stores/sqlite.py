"""使用 SQLite 持久化完整 MemoryItem。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock

from ...core.exceptions import MemorySystemError
from ..base import MemoryItem, MemoryType


class SQLiteMemoryStore:
    """保存记忆正文与元数据，向量由独立后端负责。"""

    def __init__(self, database_path: str | Path) -> None:
        if isinstance(database_path, Path):
            path_text = str(database_path)
        elif isinstance(database_path, str):
            path_text = database_path.strip()
        else:
            raise MemorySystemError("SQLite 数据库路径必须是字符串或 Path。")
        if not path_text:
            raise MemorySystemError("SQLite 数据库路径不能为空。")

        self.database_path = path_text
        if path_text != ":memory:":
            Path(path_text).expanduser().resolve().parent.mkdir(
                parents=True,
                exist_ok=True,
            )
        try:
            self._connection = sqlite3.connect(
                path_text,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆数据库连接失败。") from error
        self._lock = RLock()
        self._closed = False
        self._initialize_schema()

    def add(self, item: MemoryItem) -> str:
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("SQLite 只能保存 MemoryItem。")
        metadata_json = self._serialize_metadata(item.metadata)
        values = (
            item.id,
            item.memory_type,
            item.content,
            item.importance,
            item.created_at.isoformat(),
            item.accessed_at.isoformat(),
            item.expires_at.isoformat() if item.expires_at else None,
            item.access_count,
            metadata_json,
        )
        try:
            with self._lock, self._connection:
                self._require_open()
                self._connection.execute(
                    """
                    INSERT INTO memories (
                        id, memory_type, content, importance,
                        created_at, accessed_at, expires_at,
                        access_count, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
        except sqlite3.IntegrityError as error:
            raise MemorySystemError(f"记忆 ID 已存在：{item.id}。") from error
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆写入失败。") from error
        return item.id

    def get(self, memory_id: str) -> MemoryItem | None:
        normalized_id = self._normalize_id(memory_id)
        try:
            with self._lock:
                self._require_open()
                row = self._connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (normalized_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆读取失败。") from error
        return self._row_to_item(row) if row is not None else None

    def list_items(
        self,
        *,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
        min_importance: float = 0.0,
    ) -> list[MemoryItem]:
        if (
            isinstance(min_importance, bool)
            or not isinstance(min_importance, (int, float))
            or not 0.0 <= float(min_importance) <= 1.0
        ):
            raise MemorySystemError("最低重要性必须在 0 到 1 之间。")
        query = "SELECT * FROM memories WHERE importance >= ?"
        parameters: list[object] = [min_importance]
        if memory_type is not None:
            query += " AND memory_type = ?"
            parameters.append(memory_type)
        query += " ORDER BY created_at ASC, id ASC"
        try:
            with self._lock:
                self._require_open()
                rows = self._connection.execute(query, parameters).fetchall()
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆列表读取失败。") from error

        items = [self._row_to_item(row) for row in rows]
        if user_id is not None:
            items = [
                item
                for item in items
                if item.metadata.get("user_id") == user_id
            ]
        return items

    def update_access(
        self,
        memory_id: str,
        *,
        accessed_at: datetime,
        access_count: int,
    ) -> None:
        normalized_id = self._normalize_id(memory_id)
        if accessed_at.utcoffset() is None:
            raise MemorySystemError("访问时间必须包含时区信息。")
        if (
            isinstance(access_count, bool)
            or not isinstance(access_count, int)
            or access_count < 0
        ):
            raise MemorySystemError("访问次数必须是非负整数。")
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(
                    """
                    UPDATE memories
                    SET accessed_at = ?, access_count = ?
                    WHERE id = ?
                    """,
                    (accessed_at.isoformat(), access_count, normalized_id),
                )
                if cursor.rowcount == 0:
                    raise MemorySystemError(f"未找到记忆：{normalized_id}。")
        except MemorySystemError:
            raise
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 访问状态更新失败。") from error

    def delete(self, memory_id: str) -> bool:
        normalized_id = self._normalize_id(memory_id)
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(
                    "DELETE FROM memories WHERE id = ?",
                    (normalized_id,),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆删除失败。") from error

    def clear(
        self,
        *,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
    ) -> int:
        if user_id is not None:
            items = self.list_items(
                memory_type=memory_type,
                user_id=user_id,
            )
            ids = [item.id for item in items]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            query = f"DELETE FROM memories WHERE id IN ({placeholders})"
            parameters: list[object] = ids
        elif memory_type is not None:
            query = "DELETE FROM memories WHERE memory_type = ?"
            parameters = [memory_type]
        else:
            query = "DELETE FROM memories"
            parameters = []
        try:
            with self._lock, self._connection:
                self._require_open()
                cursor = self._connection.execute(query, parameters)
                return cursor.rowcount
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆清空失败。") from error

    def count(
        self,
        *,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
    ) -> int:
        return len(
            self.list_items(memory_type=memory_type, user_id=user_id)
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _initialize_schema(self) -> None:
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        memory_type TEXT NOT NULL,
                        content TEXT NOT NULL,
                        importance REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        accessed_at TEXT NOT NULL,
                        expires_at TEXT,
                        access_count INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memories_type_created
                    ON memories(memory_type, created_at)
                    """
                )
        except sqlite3.Error as error:
            raise MemorySystemError("SQLite 记忆表初始化失败。") from error

    def _require_open(self) -> None:
        if self._closed:
            raise MemorySystemError("SQLite 记忆数据库已经关闭。")

    @staticmethod
    def _normalize_id(memory_id: object) -> str:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemorySystemError("记忆 ID 不能为空。")
        return memory_id.strip()

    @staticmethod
    def _serialize_metadata(metadata: dict[str, object]) -> str:
        try:
            return json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise MemorySystemError("记忆元数据必须可以序列化为 JSON。") from error

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        try:
            metadata = json.loads(row["metadata_json"])
            return MemoryItem(
                id=row["id"],
                memory_type=row["memory_type"],
                content=row["content"],
                importance=row["importance"],
                created_at=datetime.fromisoformat(row["created_at"]),
                accessed_at=datetime.fromisoformat(row["accessed_at"]),
                expires_at=(
                    datetime.fromisoformat(row["expires_at"])
                    if row["expires_at"]
                    else None
                ),
                access_count=row["access_count"],
                metadata=metadata,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise MemorySystemError("SQLite 中的记忆数据格式无效。") from error


__all__ = ["SQLiteMemoryStore"]
