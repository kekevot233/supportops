"""空间服务：身份来自可信适配器，成员与角色在每次数据库事务中重新检查。"""

from __future__ import annotations

from .database import ApplicationDatabase, new_id, utc_now
from .errors import SupportOpsError
from .identity import IdentityProvider
from .models import AuditView, Identity, MemberView, MembershipChange, WorkspaceCreate, WorkspaceView


class WorkspaceService:
    def __init__(self, database: ApplicationDatabase, identity_provider: IdentityProvider):
        self._database = database
        self._identities = identity_provider

    def _identity(self, credential):
        identity = self._identities.authenticate(credential)
        if not isinstance(identity, Identity):
            raise SupportOpsError("UNAUTHENTICATED")
        return identity

    @staticmethod
    def _active(connection, identity):
        row = connection.execute("SELECT active FROM users WHERE id=?", (identity.subject,)).fetchone()
        if row is None or not row["active"]:
            raise SupportOpsError("UNAUTHENTICATED")

    def _authorize(self, connection, identity, workspace_id, *, admin=False, write=False):
        self._active(connection, identity)
        if not isinstance(workspace_id, str):
            raise SupportOpsError("NOT_FOUND")
        row = connection.execute(
            "SELECT w.id,w.name,w.created_at,m.role FROM workspaces w "
            "JOIN memberships m ON m.workspace_id=w.id WHERE w.id=? AND m.user_id=?",
            (workspace_id, identity.subject)).fetchone()
        # 无成员关系和根本不存在使用相同错误，不透露空间是否存在。
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        if admin and row["role"] != "admin":
            raise SupportOpsError("FORBIDDEN")
        if write and row["role"] not in {"admin", "editor"}:
            raise SupportOpsError("FORBIDDEN")
        return row

    @staticmethod
    def _parse(model, payload):
        if not isinstance(payload, dict):
            raise SupportOpsError("INVALID_REQUEST")
        try:
            return model.model_validate(payload)
        except ValueError:
            raise SupportOpsError("INVALID_REQUEST") from None

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise SupportOpsError("INVALID_REQUEST")
        return limit

    def list_workspaces(self, credential: str | None, *, limit: int = 100) -> list[WorkspaceView]:
        identity = self._identity(credential)
        limit = self._limit(limit)
        with self._database.transaction() as connection:
            self._active(connection, identity)
            rows = connection.execute(
                "SELECT w.id,w.name,w.created_at,m.role FROM workspaces w JOIN memberships m "
                "ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id LIMIT ?",
                (identity.subject, limit)).fetchall()
        return [WorkspaceView(**dict(row)) for row in rows]

    def get_workspace(self, credential: str | None, workspace_id: str) -> WorkspaceView:
        identity = self._identity(credential)
        with self._database.transaction() as connection:
            return WorkspaceView(**dict(self._authorize(connection, identity, workspace_id)))

    def create_workspace(self, credential: str | None, payload: dict) -> WorkspaceView:
        identity = self._identity(credential)
        request = self._parse(WorkspaceCreate, payload)
        workspace_id, now = new_id("ws"), utc_now()
        with self._database.transaction(write=True) as connection:
            self._active(connection, identity)
            connection.execute("INSERT INTO workspaces VALUES(?,?,?,?)",
                               (workspace_id, request.name, identity.subject, now))
            connection.execute("INSERT INTO memberships VALUES(?,?,?,?)",
                               (workspace_id, identity.subject, "admin", now))
            self._database.record_audit(connection, workspace_id, identity.subject, "workspace.created", workspace_id)
        return WorkspaceView(id=workspace_id, name=request.name, role="admin", created_at=now)

    def list_members(self, credential: str | None, workspace_id: str, *, limit: int = 100) -> list[MemberView]:
        identity = self._identity(credential)
        limit = self._limit(limit)
        with self._database.transaction() as connection:
            self._authorize(connection, identity, workspace_id, admin=True)
            rows = connection.execute(
                "SELECT u.id AS user_id,u.display_name,m.role FROM memberships m "
                "JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.id LIMIT ?",
                (workspace_id, limit)).fetchall()
        return [MemberView(**dict(row)) for row in rows]

    def set_member(self, credential: str | None, workspace_id: str, payload: dict) -> MemberView:
        identity = self._identity(credential)
        with self._database.transaction(write=True) as connection:
            self._authorize(connection, identity, workspace_id, admin=True)
            request = self._parse(MembershipChange, payload)
            target = connection.execute("SELECT display_name,active FROM users WHERE id=?",
                                        (request.member_id,)).fetchone()
            if target is None or not target["active"]:
                raise SupportOpsError("NOT_FOUND")
            self._protect_last_admin(connection, workspace_id, request.member_id, request.role)
            old = connection.execute("SELECT role FROM memberships WHERE workspace_id=? AND user_id=?",
                                     (workspace_id, request.member_id)).fetchone()
            if old is None or old["role"] != request.role:
                connection.execute(
                    "INSERT INTO memberships VALUES(?,?,?,?) ON CONFLICT(workspace_id,user_id) "
                    "DO UPDATE SET role=excluded.role",
                    (workspace_id, request.member_id, request.role, utc_now()))
                self._database.record_audit(connection, workspace_id, identity.subject, "membership.changed", request.member_id)
        return MemberView(user_id=request.member_id, display_name=target["display_name"], role=request.role)

    def remove_member(self, credential: str | None, workspace_id: str, member_id: str) -> None:
        identity = self._identity(credential)
        with self._database.transaction(write=True) as connection:
            self._authorize(connection, identity, workspace_id, admin=True)
            if not isinstance(member_id, str):
                raise SupportOpsError("INVALID_REQUEST")
            self._protect_last_admin(connection, workspace_id, member_id, None)
            cursor = connection.execute("DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                                        (workspace_id, member_id))
            if cursor.rowcount:
                self._database.record_audit(connection, workspace_id, identity.subject, "membership.removed", member_id)

    @staticmethod
    def _protect_last_admin(connection, workspace_id, member_id, new_role):
        old = connection.execute("SELECT role FROM memberships WHERE workspace_id=? AND user_id=?",
                                 (workspace_id, member_id)).fetchone()
        if old and old["role"] == "admin" and new_role != "admin":
            count = connection.execute(
                "SELECT count(*) FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.workspace_id=? AND m.role='admin' AND u.active=1", (workspace_id,)).fetchone()[0]
            if count <= 1:
                raise SupportOpsError("CONFLICT")

    def list_audit(self, credential: str | None, workspace_id: str, *, limit: int = 100) -> list[AuditView]:
        identity = self._identity(credential)
        limit = self._limit(limit)
        with self._database.transaction() as connection:
            self._authorize(connection, identity, workspace_id, admin=True)
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE workspace_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (workspace_id, limit)).fetchall()
        return [AuditView(**dict(row)) for row in rows]


__all__ = ["WorkspaceService"]
