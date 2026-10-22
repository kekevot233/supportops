"""独立应用数据库：版本化建表、外键、短事务；不复用框架记忆表。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Iterator
from uuid import uuid4

from .errors import SupportOpsError
from .chat_schema import CHAT_SCHEMA, CHAT_TABLES
from .ticket_schema import TICKET_SCHEMA, TICKET_TABLES

APPLICATION_ID = 0x53555050
SCHEMA_VERSION = 4

_SCHEMA = (
    """CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE users (
        id TEXT PRIMARY KEY, display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 200),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL)""",
    """CREATE TABLE workspaces (
        id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
        created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL)""",
    """CREATE TABLE memberships (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id),
        role TEXT NOT NULL CHECK(role IN ('reader','editor','admin')), created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,user_id))""",
    """CREATE INDEX memberships_by_user ON memberships(user_id, workspace_id)""",
    # 文档/版本/任务预留数据约束；P1 不创建上传、索引、发布工作流。
    """CREATE TABLE documents (
        id TEXT NOT NULL, workspace_id TEXT NOT NULL REFERENCES workspaces(id), title TEXT NOT NULL,
        current_version_id TEXT, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,id,current_version_id)
          REFERENCES document_versions(workspace_id,document_id,id) DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE document_versions (
        id TEXT NOT NULL, workspace_id TEXT NOT NULL, document_id TEXT NOT NULL,
        version_number INTEGER NOT NULL CHECK(version_number > 0), content_sha256 TEXT NOT NULL,
        object_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','ready','failed','deleted')),
        created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,document_id,id),
        UNIQUE(workspace_id,document_id,version_number),
        FOREIGN KEY(workspace_id,document_id) REFERENCES documents(workspace_id,id))""",
    """CREATE TABLE ingestion_jobs (
        id TEXT NOT NULL, workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), error_code TEXT,
        created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,idempotency_key),
        FOREIGN KEY(workspace_id,document_id,version_id)
          REFERENCES document_versions(workspace_id,document_id,id))""",
    """CREATE TABLE audit_events (
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL, target_id TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE INDEX audit_by_workspace ON audit_events(workspace_id,created_at,id)""",
)
_V1_TABLES = {"app_meta", "users", "workspaces", "memberships", "documents", "document_versions",
              "ingestion_jobs", "audit_events"}
_P2_SCHEMA = (
    """CREATE TABLE document_originals (
        workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
        filename TEXT NOT NULL, title TEXT NOT NULL, content BLOB NOT NULL CHECK(length(content)<=256000),
        PRIMARY KEY(workspace_id,document_id,version_id),
        FOREIGN KEY(workspace_id,document_id,version_id)
          REFERENCES document_versions(workspace_id,document_id,id))""",
    """CREATE TABLE indexed_chunks (
        workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL, profile TEXT NOT NULL, data_json TEXT NOT NULL,
        PRIMARY KEY(workspace_id,document_id,version_id,chunk_id),
        FOREIGN KEY(workspace_id,document_id,version_id)
          REFERENCES document_versions(workspace_id,document_id,id))""",
    """CREATE TABLE document_tombstones (
        workspace_id TEXT NOT NULL, document_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,document_id),
        FOREIGN KEY(workspace_id,document_id) REFERENCES documents(workspace_id,id))""",
    """CREATE TABLE job_leases (
        workspace_id TEXT NOT NULL, job_id TEXT NOT NULL, token TEXT NOT NULL, expires_at REAL NOT NULL,
        PRIMARY KEY(workspace_id,job_id),
        FOREIGN KEY(workspace_id,job_id) REFERENCES ingestion_jobs(workspace_id,id))""",
)
_V2_TABLES = _V1_TABLES | {"document_originals", "indexed_chunks", "document_tombstones", "job_leases"}
_V3_TABLES = _V2_TABLES | CHAT_TABLES
_TABLES = _V3_TABLES | TICKET_TABLES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class ApplicationDatabase:
    """可信仓储，不是权限入口；业务调用应通过 WorkspaceService。

    单连接由进程内 RLock 串行化；SQLite 提供跨连接写事务互斥。
    P2 任务领取与超时令牌由 DocumentService 实现，本类不运行后台线程。
    """

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = RLock()
        self._closed = True
        self._connection = None
        try:
            if str(path) != ":memory:":
                resolved = Path(path).resolve()
                if resolved.suffix != ".sqlite3":
                    raise SupportOpsError("INVALID_REQUEST")
                resolved.parent.mkdir(parents=True, exist_ok=True)
                path = resolved
            self._connection = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
            self._closed = False
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
        except (OSError, sqlite3.Error):
            self.close()
            raise SupportOpsError("STORAGE_UNAVAILABLE") from None
        except BaseException:
            self.close()
            raise

    def _initialize(self) -> None:
        with self.transaction(write=True) as connection:
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = connection.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            tables = {row["name"] for row in objects if row["type"] == "table"}
            if app_id == 0 and version == 0 and not objects:
                for statement in (*_SCHEMA, *_P2_SCHEMA, *CHAT_SCHEMA, *TICKET_SCHEMA):
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id == APPLICATION_ID and version == 1 and tables == _V1_TABLES:
                # 唯一已知的附加迁移；DDL 与版本标记处于同一事务，不重建旧表或重置权限。
                for statement in (*_P2_SCHEMA, *CHAT_SCHEMA, *TICKET_SCHEMA):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id == APPLICATION_ID and version == 2 and tables == _V2_TABLES:
                for statement in (*CHAT_SCHEMA, *TICKET_SCHEMA):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id == APPLICATION_ID and version == 3 and tables == _V3_TABLES:
                for statement in TICKET_SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id != APPLICATION_ID or version != SCHEMA_VERSION or tables != _TABLES:
                raise SupportOpsError("STORAGE_VERSION_UNSUPPORTED")

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise SupportOpsError("STORAGE_UNAVAILABLE")
            connection = self._connection
            if connection.in_transaction:
                # 不允许嵌套事务；内部 BEGIN 失败不能误回滚调用方尚在执行的事务。
                raise SupportOpsError("STORAGE_UNAVAILABLE")
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
            except BaseException as error:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except sqlite3.Error:
                    raise SupportOpsError("STORAGE_UNAVAILABLE") from None
                if isinstance(error, sqlite3.Error):
                    code = "CONFLICT" if isinstance(error, sqlite3.IntegrityError) else "STORAGE_UNAVAILABLE"
                    raise SupportOpsError(code) from None
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._connection is not None:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> "ApplicationDatabase":
        return self

    def __exit__(self, *_):
        self.close()

    def bootstrap_demo(self, *, enabled: bool = False) -> bool:
        """显式可信本地初始化；只执行一次，重复启动不恢复已经撤销的成员权限。"""
        if enabled is not True:
            raise SupportOpsError("DEMO_DISABLED")
        with self.transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM app_meta WHERE key='demo_seed_v1'").fetchone():
                return False
            # 仅允许向空应用库初始化，不能用固定 ID 合并覆盖未知业务数据。
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise SupportOpsError("CONFLICT")
            now = utc_now()
            connection.executemany("INSERT INTO users(id,display_name,created_at) VALUES(?,?,?)", [
                ("demo-admin", "演示管理员", now), ("demo-reader", "演示只读成员", now)])
            connection.executemany("INSERT INTO workspaces VALUES(?,?,?,?)", [
                ("helloagent", "HelloAgent 项目知识", "demo-admin", now),
                ("internal-lab", "隔离测试空间", "demo-admin", now)])
            connection.executemany("INSERT INTO memberships VALUES(?,?,?,?)", [
                ("helloagent", "demo-admin", "admin", now),
                ("helloagent", "demo-reader", "reader", now),
                ("internal-lab", "demo-admin", "admin", now)])
            connection.execute("INSERT INTO app_meta VALUES('demo_seed_v1','1')")
            for workspace in ("helloagent", "internal-lab"):
                self.record_audit(connection, workspace, "demo-admin", "demo.initialized", workspace)
            return True

    @staticmethod
    def record_audit(connection, workspace_id, actor_id, action, target_id):
        connection.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?)", (
            new_id("evt"), workspace_id, actor_id, action, target_id, utc_now()))


__all__ = ["ApplicationDatabase"]
