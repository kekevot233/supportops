"""有界原件接口；当前用 SQLite BLOB 与版本/任务原子提交，不读取客户端路径。"""

from __future__ import annotations

from typing import Protocol

MAX_UPLOAD_BYTES = 256_000


def decode_document(content: bytes) -> str:
    text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "\x00" in text:
        raise ValueError("empty or binary content")
    return text


class OriginalStore(Protocol):
    """可信事务参与者；替换为外部存储时必须重新设计提交与清理补偿。"""

    def put(self, connection, workspace_id, document_id, version_id, filename, title, content): ...
    def get(self, connection, workspace_id, document_id, version_id): ...
    def delete(self, connection, workspace_id, document_id): ...


class SQLiteOriginalStore:
    def put(self, connection, workspace_id, document_id, version_id, filename, title, content):
        connection.execute("INSERT INTO document_originals VALUES(?,?,?,?,?,?)",
                           (workspace_id, document_id, version_id, filename, title, content))

    def get(self, connection, workspace_id, document_id, version_id):
        row = connection.execute(
            "SELECT filename,title,content FROM document_originals "
            "WHERE workspace_id=? AND document_id=? AND version_id=?",
            (workspace_id, document_id, version_id)).fetchone()
        if row is None:
            raise ValueError("original unavailable")
        return dict(row)

    def delete(self, connection, workspace_id, document_id):
        connection.execute("DELETE FROM document_originals WHERE workspace_id=? AND document_id=?",
                           (workspace_id, document_id))


__all__ = ["OriginalStore", "SQLiteOriginalStore"]
