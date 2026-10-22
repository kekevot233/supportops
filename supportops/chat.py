"""私有会话与可追溯回答：模型调用在写事务外，成功时原子提交完整轮次。"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager

from .answering import AnswerEngine
from .chat_models import ConversationCreate, FeedbackRequest, QuestionRequest
from .database import new_id, utc_now
from .documents import DocumentService
from .errors import SupportOpsError
from .workspaces import WorkspaceService

HIDDEN_ANSWER = "来源已删除，历史回答正文已隐藏。"
INSUFFICIENT = "现有资料不足以回答，请补充相关文档或问题背景。"


class ChatService:
    def __init__(self, database, identity_provider, engine: AnswerEngine, *, documents=None, clock=time.time):
        self._database = database
        self._access = WorkspaceService(database, identity_provider)
        self._documents = documents or DocumentService(database, identity_provider)
        self._engine, self._clock = engine, clock

    @contextmanager
    def _scope(self, credential, workspace_id, *, write=False):
        identity = self._access._identity(credential)
        with self._database.transaction(write=write) as connection:
            self._access._authorize(connection, identity, workspace_id)
            yield connection, identity

    @staticmethod
    def _conversation(connection, workspace_id, conversation_id, identity):
        if not isinstance(conversation_id, str):
            raise SupportOpsError("NOT_FOUND")
        row = connection.execute("SELECT * FROM conversations WHERE workspace_id=? AND id=? AND owner_id=?",
                                 (workspace_id, conversation_id, identity.subject)).fetchone()
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        return row

    def _answer(self, connection, workspace_id, answer_id, identity):
        row = connection.execute("SELECT * FROM answers WHERE workspace_id=? AND id=? AND owner_id=?",
                                 (workspace_id, answer_id, identity.subject)).fetchone()
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        return row

    @staticmethod
    def _snapshot(connection, workspace_id):
        return [(row["id"], row["current_version_id"]) for row in connection.execute(
            "SELECT id,current_version_id FROM documents WHERE workspace_id=? "
            "AND current_version_id IS NOT NULL ORDER BY id", (workspace_id,))]

    @staticmethod
    def _citations(connection, workspace_id, answer_id):
        rows = connection.execute(
            "SELECT c.*,d.current_version_id,t.deleted_at FROM answer_citations c "
            "JOIN documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id "
            "LEFT JOIN document_tombstones t ON t.workspace_id=c.workspace_id AND t.document_id=c.document_id "
            "WHERE c.workspace_id=? AND c.answer_id=? ORDER BY c.label", (workspace_id, answer_id)).fetchall()
        return [{"label": row["label"], "document_id": row["document_id"], "version_id": row["version_id"],
                 "chunk_id": row["chunk_id"], "state": "deleted" if row["deleted_at"] else
                 "current" if row["current_version_id"] == row["version_id"] else "historical"} for row in rows]

    def _answer_view(self, connection, row):
        citations = self._citations(connection, row["workspace_id"], row["id"])
        state = "deleted" if any(item["state"] == "deleted" for item in citations) else \
                "historical" if any(item["state"] == "historical" for item in citations) else "current"
        return {"id": row["id"], "workspace_id": row["workspace_id"], "conversation_id": row["conversation_id"],
                "run_id": row["run_id"], "status": row["status"], "source_state": state,
                "content": HIDDEN_ANSWER if state == "deleted" else row["content"], "citations": citations,
                "created_at": row["created_at"]}

    def _messages(self, connection, workspace_id, conversation_id, *, for_model=False):
        rows = connection.execute("SELECT * FROM chat_messages WHERE workspace_id=? AND conversation_id=? "
                                  "ORDER BY sequence", (workspace_id, conversation_id)).fetchall()
        result = []
        for row in rows:
            content = row["content"]
            if row["role"] == "assistant":
                citations = self._citations(connection, workspace_id, row["answer_id"])
                if any(item["state"] == "deleted" for item in citations):
                    content = HIDDEN_ANSWER
                elif for_model and any(item["state"] == "historical" for item in citations):
                    content = "历史回答的资料已变更，不可作为当前事实。"
            result.append({"role": row["role"], "content": content, **({"answer_id": row["answer_id"]} if not for_model else {})})
        return result[-24:] if for_model else result

    def create_conversation(self, credential, workspace_id, payload: dict) -> dict:
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            request = self._access._parse(ConversationCreate, payload)
            count = connection.execute("SELECT count(*) FROM conversations WHERE workspace_id=? AND owner_id=?",
                                       (workspace_id, identity.subject)).fetchone()[0]
            if count >= 100:
                raise SupportOpsError("CAPACITY_EXCEEDED")
            conversation_id, now = new_id("chat"), utc_now()
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                               (workspace_id, conversation_id, identity.subject, request.title, 0, now))
            self._database.record_audit(connection, workspace_id, identity.subject, "conversation.created", conversation_id)
            return {"id": conversation_id, "title": request.title, "revision": 0, "created_at": now}

    def list_conversations(self, credential, workspace_id, *, limit=100) -> list[dict]:
        with self._scope(credential, workspace_id) as (connection, identity):
            limit = self._access._limit(limit)
            return [dict(row) for row in connection.execute(
                "SELECT id,title,revision,created_at FROM conversations WHERE workspace_id=? AND owner_id=? ORDER BY id LIMIT ?",
                (workspace_id, identity.subject, limit))]

    def get_conversation(self, credential, workspace_id, conversation_id) -> dict:
        with self._scope(credential, workspace_id) as (connection, identity):
            row = self._conversation(connection, workspace_id, conversation_id, identity)
            return {"id": row["id"], "title": row["title"], "revision": row["revision"],
                    "messages": self._messages(connection, workspace_id, conversation_id)}

    def _begin(self, credential, workspace_id, conversation_id, payload, request_id):
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            conversation = self._conversation(connection, workspace_id, conversation_id, identity)
            request = self._access._parse(QuestionRequest, payload)
            fingerprint = hashlib.sha256(request.question.encode("utf-8")).hexdigest()
            connection.execute("UPDATE answer_runs SET state='failed',error_code='RUN_EXPIRED',finished_at=? "
                "WHERE workspace_id=? AND conversation_id=? AND state='running' AND expires_at<=?",
                (utc_now(), workspace_id, conversation_id, self._clock()))
            old = connection.execute("SELECT * FROM answer_runs WHERE workspace_id=? AND conversation_id=? AND idempotency_key=?",
                                     (workspace_id, conversation_id, request.idempotency_key)).fetchone()
            if old:
                if old["request_sha256"] != fingerprint or old["state"] == "running":
                    return {"error": "CONFLICT"}
                if old["state"] == "failed":
                    return {"error": old["error_code"] or "API_UNAVAILABLE", "run_id": old["id"]}
                answer = connection.execute("SELECT id FROM answers WHERE workspace_id=? AND run_id=?", (workspace_id, old["id"])).fetchone()
                if answer is None:
                    raise SupportOpsError("STORAGE_UNAVAILABLE")
                return {"answer_id": answer["id"]}
            if conversation["revision"] >= 100 or connection.execute(
                "SELECT count(*) FROM answer_runs WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id)).fetchone()[0] >= 200:
                return {"error": "CAPACITY_EXCEEDED"}
            if connection.execute(
                "SELECT 1 FROM answer_runs WHERE workspace_id=? AND conversation_id=? AND state='running'",
                (workspace_id, conversation_id)).fetchone():
                return {"error": "CONFLICT"}
            run_id = new_id("run")
            connection.execute("INSERT INTO answer_runs(workspace_id,id,conversation_id,owner_id,idempotency_key,request_sha256,request_id,"
                "state,expires_at,model_mode,created_at) VALUES(?,?,?,?,?,?,?,'running',?,?,?)",
                (workspace_id, run_id, conversation_id, identity.subject, request.idempotency_key, fingerprint,
                 request_id, self._clock() + 300, self._engine.mode, utc_now()))
            return {"run_id": run_id, "owner_id": identity.subject, "revision": conversation["revision"],
                    "question": request.question, "snapshot": self._snapshot(connection, workspace_id),
                    "history": self._messages(connection, workspace_id, conversation_id, for_model=True)}

    def send_message(self, credential, workspace_id, conversation_id, payload: dict, *, request_id=None) -> dict:
        begun = self._begin(credential, workspace_id, conversation_id, payload, request_id or new_id("req"))
        if "error" in begun:
            raise SupportOpsError(begun["error"], run_id=begun.get("run_id"))
        if "answer_id" in begun:
            return self.get_answer(credential, workspace_id, begun["answer_id"])
        started = time.perf_counter()
        diagnostics = {"model_attempts": 0, "estimated_input_tokens": None, "estimated_output_tokens": None,
                       "retrieval_ms": 0.0, "generation_ms": 0.0}
        try:
            step = time.perf_counter()
            try:
                result = self._documents.search(credential, workspace_id, begun["question"], limit=5)
            finally:
                diagnostics["retrieval_ms"] = (time.perf_counter() - step) * 1000
            step = time.perf_counter()
            try:
                def before_invoke():
                    with self._scope(credential, workspace_id, write=True) as (connection, identity):
                        self._conversation(connection, workspace_id, conversation_id, identity)
                        if self._snapshot(connection, workspace_id) != begun["snapshot"]:
                            raise SupportOpsError("SOURCE_CHANGED")
                        run = connection.execute("SELECT state,expires_at FROM answer_runs WHERE workspace_id=? AND id=?",
                                                 (workspace_id, begun["run_id"])).fetchone()
                        if run["state"] != "running" or run["expires_at"] <= self._clock():
                            raise SupportOpsError("RUN_EXPIRED")
                        connection.execute("UPDATE answer_runs SET model_attempts=1,estimated_input_tokens=? WHERE workspace_id=? AND id=?",
                                           (diagnostics["estimated_input_tokens"], workspace_id, begun["run_id"]))
                options = {}
                if self._engine.mode == "local_deepseek":
                    options["invocation_context"] = {"owner": begun["owner_id"], "workspace": workspace_id,
                        "conversation": conversation_id, "run_id": begun["run_id"]}
                generated = self._engine.generate(begun["question"], begun["history"], result, diagnostics,
                                                   on_invoke=before_invoke, **options)
            finally:
                diagnostics["generation_ms"] = (time.perf_counter() - step) * 1000
            with self._scope(credential, workspace_id, write=True) as (connection, identity):
                conversation = self._conversation(connection, workspace_id, conversation_id, identity)
                run = connection.execute("SELECT * FROM answer_runs WHERE workspace_id=? AND id=?", (workspace_id, begun["run_id"])).fetchone()
                if run["state"] != "running" or run["expires_at"] <= self._clock() or conversation["revision"] != begun["revision"]:
                    raise SupportOpsError("RUN_EXPIRED")
                if self._snapshot(connection, workspace_id) != begun["snapshot"]:
                    raise SupportOpsError("SOURCE_CHANGED")
                answer_id, now = new_id("answer"), utc_now()
                content = "\n\n".join(f"{passage.quote} [{passage.label}]" for passage in generated.draft.passages) or INSUFFICIENT
                connection.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)", (workspace_id, answer_id,
                    conversation_id, identity.subject, begun["run_id"], generated.draft.status, content, now))
                for passage in generated.draft.passages:
                    source = generated.evidence[passage.label]
                    connection.execute("INSERT INTO answer_citations VALUES(?,?,?,?,?,?)", (workspace_id, answer_id,
                        passage.label, source["document_id"], source["version_id"], source["chunk_id"]))
                for offset, role, text in ((1, "user", begun["question"]), (2, "assistant", content)):
                    connection.execute("INSERT INTO chat_messages VALUES(?,?,?,?,?,?)", (workspace_id, conversation_id,
                        begun["revision"] * 2 + offset, answer_id, role, text))
                connection.execute("UPDATE conversations SET revision=revision+1 WHERE workspace_id=? AND id=?", (workspace_id, conversation_id))
                self._finish_run(connection, workspace_id, begun["run_id"], "succeeded", None, diagnostics, started)
                self._database.record_audit(connection, workspace_id, identity.subject, "answer.committed", answer_id)
                return self._answer_view(connection, self._answer(connection, workspace_id, answer_id, identity))
        except Exception as error:
            public = SupportOpsError(error.code if isinstance(error, SupportOpsError) else "API_UNAVAILABLE",
                                     run_id=begun["run_id"])
            with self._database.transaction(write=True) as connection:
                self._finish_run(connection, workspace_id, begun["run_id"], "failed", public.code, diagnostics, started)
            raise public from None

    @staticmethod
    def _finish_run(connection, workspace_id, run_id, state, error, diagnostics, started):
        connection.execute("UPDATE answer_runs SET state=?,error_code=?,model_attempts=?,estimated_input_tokens=?,"
            "estimated_output_tokens=?,retrieval_ms=?,generation_ms=?,total_ms=?,finished_at=? "
            "WHERE workspace_id=? AND id=? AND state='running'", (state, error, diagnostics["model_attempts"],
            diagnostics["estimated_input_tokens"], diagnostics["estimated_output_tokens"], diagnostics["retrieval_ms"],
            diagnostics["generation_ms"], max(0.0, (time.perf_counter() - started) * 1000), utc_now(), workspace_id, run_id))

    def get_answer(self, credential, workspace_id, answer_id) -> dict:
        with self._scope(credential, workspace_id) as (connection, identity):
            return self._answer_view(connection, self._answer(connection, workspace_id, answer_id, identity))

    def read_answer_citation(self, credential, workspace_id, answer_id, label) -> dict:
        with self._scope(credential, workspace_id) as (connection, identity):
            self._answer(connection, workspace_id, answer_id, identity)
            row = connection.execute("SELECT * FROM answer_citations WHERE workspace_id=? AND answer_id=? AND label=?",
                                     (workspace_id, answer_id, label)).fetchone()
            if row is None:
                raise SupportOpsError("NOT_FOUND")
            target = dict(row)
        # 原文接口再次验证空间权限/删除状态；没有缓存的正文快照可绕过文档删除。
        return self._documents.read_citation(credential, workspace_id, target["document_id"], target["version_id"], target["chunk_id"])

    def put_feedback(self, credential, workspace_id, answer_id, payload: dict) -> dict:
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            self._answer(connection, workspace_id, answer_id, identity)
            request = self._access._parse(FeedbackRequest, payload)
            connection.execute("INSERT INTO answer_feedback VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id,answer_id,user_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,"
                "comment=excluded.comment,updated_at=excluded.updated_at", (workspace_id, answer_id, identity.subject,
                request.rating, request.reason, request.comment, utc_now()))
            self._database.record_audit(connection, workspace_id, identity.subject, "answer.feedback", answer_id)
            return request.model_dump()

    def get_feedback(self, credential, workspace_id, answer_id):
        with self._scope(credential, workspace_id) as (connection, identity):
            self._answer(connection, workspace_id, answer_id, identity)
            row = connection.execute("SELECT rating,reason,comment FROM answer_feedback WHERE workspace_id=? AND answer_id=? AND user_id=?",
                                     (workspace_id, answer_id, identity.subject)).fetchone()
            return dict(row) if row else None

    def get_run(self, credential, workspace_id, run_id) -> dict:
        with self._scope(credential, workspace_id) as (connection, identity):
            row = connection.execute("SELECT * FROM answer_runs WHERE workspace_id=? AND id=? AND owner_id=?",
                                     (workspace_id, run_id, identity.subject)).fetchone()
            if row is None:
                raise SupportOpsError("NOT_FOUND")
            fields = ("id", "conversation_id", "request_id", "state", "error_code", "model_mode", "model_attempts",
                      "estimated_input_tokens", "estimated_output_tokens", "retrieval_ms", "generation_ms", "total_ms")
            result = {**{field: row[field] for field in fields}, "usage_source": "local_estimate" if row["model_attempts"] else "not_called",
                      "actual_usage": None, "cost": None}
            if self._engine.mode == "local_deepseek":
                result.update(self._engine.llm.budget.run_view(run_id))
            return result
