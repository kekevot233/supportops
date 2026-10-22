"""本人私有工单：引用建议 → 人工确认 → 处理记录；不执行外部操作。"""
from contextlib import contextmanager
import time

from .database import new_id, utc_now
from .errors import SupportOpsError
from .qa_cases import canonical_sha256
from .ticket_models import TicketAccept, TicketCreate, TicketNote, TicketOperation, TicketResolve
from .workspaces import WorkspaceService


class TicketService:
    def __init__(self, database, identity_provider, chats, *, clock=time.time):
        self.db, self.chats, self.clock = database, chats, clock
        self.access = WorkspaceService(database, identity_provider)

    @contextmanager
    def _scope(self, credential, workspace, *, write=False):
        identity = self.access._identity(credential)
        with self.db.transaction(write=write) as connection:
            # 工单/对话是成员自己的业务数据；reader 不能写文档，但可以提交本人问题。
            self.access._authorize(connection, identity, workspace)
            yield connection, identity

    @staticmethod
    def _ticket(connection, workspace, ticket_id, identity):
        row = connection.execute("SELECT * FROM tickets WHERE workspace_id=? AND id=? AND owner_id=?",
                                 (workspace, ticket_id, identity.subject)).fetchone()
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        return row

    def _event(self, connection, ticket, actor, kind, content, request=None):
        connection.execute("INSERT INTO ticket_events VALUES(?,?,?,?,?,?,?,?,?)",
            (ticket["workspace_id"], ticket["id"], new_id("tevt"), actor, kind, content,
             request.idempotency_key if request else None,
             canonical_sha256({"kind": kind, **request.model_dump()}) if request else None, utc_now()))
        self.db.record_audit(connection, ticket["workspace_id"], actor, "ticket." + kind, ticket["id"])

    def _view(self, connection, ticket, identity):
        result = {key: ticket[key] for key in ("id", "workspace_id", "title", "description", "category", "priority",
            "state", "revision", "accepted_suggestion_id", "created_at", "updated_at")}
        result["suggestions"] = []
        for row in connection.execute("SELECT * FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=? ORDER BY created_at,id",
                                      (ticket["workspace_id"], ticket["id"])):
            value = {key: row[key] for key in ("id", "state", "error_code", "run_id", "conversation_id", "created_at")}
            value["answer"] = None
            if row["answer_id"]:
                answer = self.chats._answer(connection, ticket["workspace_id"], row["answer_id"], identity)
                value["answer"] = self.chats._answer_view(connection, answer)
            result["suggestions"].append(value)
        result["events"] = [dict(row) for row in connection.execute(
            "SELECT id,kind,content,created_at FROM ticket_events WHERE workspace_id=? AND ticket_id=? ORDER BY created_at,id",
            (ticket["workspace_id"], ticket["id"]))]
        result["classification_source"] = "user_selected"
        return result

    def get(self, credential, workspace, ticket_id):
        with self._scope(credential, workspace) as (connection, identity):
            return self._view(connection, self._ticket(connection, workspace, ticket_id, identity), identity)

    def list(self, credential, workspace, *, limit=100):
        with self._scope(credential, workspace) as (connection, identity):
            return [dict(row) for row in connection.execute("SELECT id,title,category,priority,state,revision,updated_at "
                "FROM tickets WHERE workspace_id=? AND owner_id=? ORDER BY updated_at DESC,id LIMIT ?",
                (workspace, identity.subject, self.access._limit(limit)))]

    def create(self, credential, workspace, payload):
        request = self.access._parse(TicketCreate, payload)
        digest = canonical_sha256(request.model_dump())
        with self._scope(credential, workspace, write=True) as (connection, identity):
            old = connection.execute("SELECT * FROM tickets WHERE workspace_id=? AND owner_id=? AND create_key=?",
                                     (workspace, identity.subject, request.idempotency_key)).fetchone()
            if old:
                if old["request_sha"] != digest:
                    raise SupportOpsError("CONFLICT")
                return self._view(connection, old, identity)
            if connection.execute("SELECT count(*) FROM tickets WHERE workspace_id=? AND owner_id=?",
                                  (workspace, identity.subject)).fetchone()[0] >= 100:
                raise SupportOpsError("CAPACITY_EXCEEDED")
            ticket_id, now = new_id("ticket"), utc_now()
            connection.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,'open',0,NULL,?,?,?,?)",
                (workspace, ticket_id, identity.subject, request.title, request.description, request.category, request.priority,
                 request.idempotency_key, digest, now, now))
            ticket = self._ticket(connection, workspace, ticket_id, identity)
            self._event(connection, ticket, identity.subject, "created", "已提交；分类与优先级由用户选择。")
            return self._view(connection, ticket, identity)

    @staticmethod
    def _revision(ticket, request):
        if ticket["revision"] != request.expected_revision:
            raise SupportOpsError("CONFLICT")
        if ticket["revision"] >= 200:
            raise SupportOpsError("CAPACITY_EXCEEDED")

    @staticmethod
    def _busy(connection, ticket):
        if connection.execute("SELECT 1 FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=? AND state='generating'",
                              (ticket["workspace_id"], ticket["id"])).fetchone():
            raise SupportOpsError("TICKET_BUSY")

    @staticmethod
    def _bump(connection, ticket):
        connection.execute("UPDATE tickets SET revision=revision+1,updated_at=? WHERE workspace_id=? AND id=?",
                           (utc_now(), ticket["workspace_id"], ticket["id"]))

    def suggest(self, credential, workspace, ticket_id, payload, *, request_id=None):
        request = self.access._parse(TicketOperation, payload)
        digest = canonical_sha256(request.model_dump())
        with self._scope(credential, workspace, write=True) as (connection, identity):
            ticket = self._ticket(connection, workspace, ticket_id, identity)
            old = connection.execute("SELECT * FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=? AND idempotency_key=?",
                                     (workspace, ticket_id, request.idempotency_key)).fetchone()
            if old:
                if old["request_sha"] != digest:
                    raise SupportOpsError("CONFLICT")
                # 包括失败/未完成：同键仅返回状态，不重发付费请求。
                return self._view(connection, ticket, identity)
            self._revision(ticket, request)
            if ticket["state"] != "open":
                raise SupportOpsError("TICKET_STATE_INVALID")
            self._busy(connection, ticket)
            if (connection.execute("SELECT count(*) FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=?", (workspace, ticket_id)).fetchone()[0] >= 10
                    or connection.execute("SELECT count(*) FROM conversations WHERE workspace_id=? AND owner_id=?", (workspace, identity.subject)).fetchone()[0] >= 100):
                raise SupportOpsError("CAPACITY_EXCEEDED")
            suggestion_id, conversation_id, now = new_id("suggestion"), new_id("chat"), utc_now()
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,0,?)",
                (workspace, conversation_id, identity.subject, ("工单建议 · " + ticket["title"])[:200], now))
            connection.execute("INSERT INTO ticket_suggestions VALUES(?,?,?,?,?,NULL,NULL,'generating',NULL,?,?,?,?,NULL)",
                (workspace, ticket_id, suggestion_id, identity.subject, conversation_id, request.idempotency_key,
                 digest, self.clock() + 300, now))
            self._bump(connection, ticket)
            self._event(connection, ticket, identity.subject, "suggestion_started", "已请求引用建议；尚未人工确认。")
            # 仅以用户问题检索；不把通用工作流指令加入查询、稀释检索相关性。
            question = f"{ticket['title']}\n{ticket['description']}"
        try:
            answer = self.chats.send_message(credential, workspace, conversation_id,
                {"question": question, "idempotency_key": "ticket-suggestion"}, request_id=request_id)
            with self._scope(credential, workspace, write=True) as (connection, identity):
                ticket = self._ticket(connection, workspace, ticket_id, identity)
                self._attach(connection, ticket, suggestion_id, answer["id"], answer["run_id"], identity.subject)
        except Exception as error:
            code = error.code if isinstance(error, SupportOpsError) else "API_UNAVAILABLE"
            run_id = error.run_id if isinstance(error, SupportOpsError) else None
            # 即使权限刚被撤销也记录失败元数据；不向已撤销用户返回工单正文。
            with self.db.transaction(write=True) as connection:
                changed = connection.execute("UPDATE ticket_suggestions SET state='failed',error_code=?,run_id=?,finished_at=? "
                    "WHERE workspace_id=? AND ticket_id=? AND id=? AND state='generating'",
                    (code, run_id, utc_now(), workspace, ticket_id, suggestion_id))
                if changed.rowcount:
                    self._bump(connection, {"workspace_id": workspace, "id": ticket_id})
                    self._event(connection, {"workspace_id": workspace, "id": ticket_id}, ticket["owner_id"],
                        "suggestion_failed", f"建议失败：{code}；未自动重新请求模型。")
            raise SupportOpsError(code, run_id=run_id) from None
        return self.get(credential, workspace, ticket_id)

    def _attach(self, connection, ticket, suggestion_id, answer_id, run_id, actor):
        if ticket["state"] != "open":
            raise SupportOpsError("TICKET_STATE_INVALID")
        changed = connection.execute("UPDATE ticket_suggestions SET state='ready',answer_id=?,run_id=?,error_code=NULL,finished_at=? "
            "WHERE workspace_id=? AND ticket_id=? AND id=? AND state IN ('generating','failed')",
            (answer_id, run_id, utc_now(), ticket["workspace_id"], ticket["id"], suggestion_id))
        if changed.rowcount != 1:
            raise SupportOpsError("CONFLICT")
        self._bump(connection, ticket)
        self._event(connection, ticket, actor, "suggestion_ready", "引用建议已保存，等待人工核对；不代表问题已解决。")

    def reconcile(self, credential, workspace, ticket_id, suggestion_id):
        """显式只读对账：可关联已提交回答或标记中断，绝不重调模型。"""
        with self._scope(credential, workspace, write=True) as (connection, identity):
            ticket = self._ticket(connection, workspace, ticket_id, identity)
            row = connection.execute("SELECT * FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=? AND id=?",
                                     (workspace, ticket_id, suggestion_id)).fetchone()
            if row is None:
                raise SupportOpsError("NOT_FOUND")
            if row["answer_id"] or row["state"] == "ready":
                return self._view(connection, ticket, identity)
            answer = connection.execute("SELECT a.id,a.run_id FROM answers a JOIN answer_runs r ON r.workspace_id=a.workspace_id AND r.id=a.run_id "
                "WHERE a.workspace_id=? AND a.conversation_id=? AND a.owner_id=? AND r.idempotency_key='ticket-suggestion'",
                                        (workspace, row["conversation_id"], identity.subject)).fetchone()
            if answer:
                self._attach(connection, ticket, suggestion_id, answer["id"], answer["run_id"], identity.subject)
            elif row["state"] == "generating":
                if row["expires_at"] > self.clock():
                    raise SupportOpsError("TICKET_BUSY")
                connection.execute("UPDATE ticket_suggestions SET state='failed',error_code='TICKET_INTERRUPTED',finished_at=? "
                    "WHERE workspace_id=? AND ticket_id=? AND id=?", (utc_now(), workspace, ticket_id, suggestion_id))
                self._bump(connection, ticket)
                self._event(connection, ticket, identity.subject, "suggestion_interrupted", "运行中断；只标记状态，没有重发模型请求。")
            return self._view(connection, self._ticket(connection, workspace, ticket_id, identity), identity)

    def operate(self, credential, workspace, ticket_id, action, payload):
        model = {"accept": TicketAccept, "note": TicketNote, "resolve": TicketResolve, "start": TicketResolve}.get(action)
        if model is None:
            raise SupportOpsError("INVALID_REQUEST")
        request = self.access._parse(model, payload)
        with self._scope(credential, workspace, write=True) as (connection, identity):
            ticket = self._ticket(connection, workspace, ticket_id, identity)
            old = connection.execute("SELECT request_sha FROM ticket_events WHERE workspace_id=? AND ticket_id=? AND idempotency_key=?",
                                     (workspace, ticket_id, request.idempotency_key)).fetchone()
            if old:
                if old[0] != canonical_sha256({"kind": action, **request.model_dump()}):
                    raise SupportOpsError("CONFLICT")
                return self._view(connection, ticket, identity)
            self._revision(ticket, request)
            if action == "start":
                self._busy(connection, ticket)
                if ticket["state"] != "open":
                    raise SupportOpsError("TICKET_STATE_INVALID")
                connection.execute("UPDATE tickets SET state='in_progress' WHERE workspace_id=? AND id=?", (workspace, ticket_id))
                content = "人工接管；未采纳任何模型建议。\n" + request.content
            elif action == "accept":
                self._busy(connection, ticket)
                suggestion = connection.execute("SELECT answer_id FROM ticket_suggestions WHERE workspace_id=? AND ticket_id=? AND id=? AND state='ready'",
                    (workspace, ticket_id, request.suggestion_id)).fetchone()
                if ticket["state"] != "open" or not suggestion or not suggestion[0]:
                    raise SupportOpsError("TICKET_STATE_INVALID")
                answer = self.chats._answer_view(connection, self.chats._answer(connection, workspace, suggestion[0], identity))
                if answer["status"] != "answered" or answer["source_state"] != "current":
                    raise SupportOpsError("TICKET_EVIDENCE_INVALID")
                connection.execute("UPDATE tickets SET state='in_progress',accepted_suggestion_id=? WHERE workspace_id=? AND id=?",
                                   (request.suggestion_id, workspace, ticket_id))
                content = "人工已核对并采纳建议。" + ("\n" + request.note if request.note else "")
            elif action == "resolve":
                if ticket["state"] != "in_progress":
                    raise SupportOpsError("TICKET_STATE_INVALID")
                connection.execute("UPDATE tickets SET state='resolved' WHERE workspace_id=? AND id=?", (workspace, ticket_id))
                content = request.content
            else:
                if ticket["state"] == "resolved":
                    raise SupportOpsError("TICKET_STATE_INVALID")
                content = request.content
            self._bump(connection, ticket)
            self._event(connection, ticket, identity.subject, action, content, request)
            return self._view(connection, self._ticket(connection, workspace, ticket_id, identity), identity)
