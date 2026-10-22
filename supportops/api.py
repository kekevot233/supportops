"""零新增依赖的 WSGI JSON 适配器；仅构造应用，不启动网络监听或提供演示登录。"""

from __future__ import annotations

import json
from http import HTTPStatus
from urllib.parse import parse_qs

from .chat import ChatService
from .database import new_id
from .documents import DocumentService
from .errors import SupportOpsError
from .identity import DemoIdentityProvider
from .workspaces import WorkspaceService
from .tickets import TicketService

MAX_BODY = 300_000


class SupportOpsAPI:
    def __init__(self, database, identity_provider, engine, *, allow_local_sessions=False):
        if isinstance(identity_provider, DemoIdentityProvider):
            raise SupportOpsError("DEMO_DISABLED")
        self.database, self.identities = database, identity_provider
        self.allow_local_sessions = allow_local_sessions is True
        self.workspaces = WorkspaceService(database, identity_provider)
        self.documents = DocumentService(database, identity_provider)
        self.chats = ChatService(database, identity_provider, engine, documents=self.documents)
        self.tickets = TicketService(database, identity_provider, self.chats)

    def __call__(self, environ, start_response):
        request_id = new_id("req")
        try:
            status, payload = self._dispatch(environ, request_id)
        except SupportOpsError as error:
            status, payload = error.status, error.to_dict()
        except Exception:
            status, payload = 503, SupportOpsError("API_UNAVAILABLE").to_dict()
        data = json.dumps({"request_id": request_id, "data": payload} if status < 400 else
                          {"request_id": request_id, **payload}, ensure_ascii=False).encode("utf-8")
        start_response(f"{status} {HTTPStatus(status).phrase}", [
            ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Request-ID", request_id)])
        return [data]

    @staticmethod
    def _body(environ):
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
            if length < 0:
                raise ValueError("invalid length")
            if length > MAX_BODY:
                raise SupportOpsError("REQUEST_TOO_LARGE")
            if environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
                raise ValueError("json required")
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise ValueError("truncated request")
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            def invalid(_):
                raise ValueError("invalid JSON number")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
            if not isinstance(body, dict):
                raise ValueError("object required")
            return body
        except SupportOpsError:
            raise
        except (ValueError, UnicodeError, KeyError):
            raise SupportOpsError("INVALID_REQUEST") from None

    def _dispatch(self, environ, request_id):
        method, path = environ.get("REQUEST_METHOD", ""), environ.get("PATH_INFO", "")
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok"}
        if method == "GET" and path == "/readyz":
            with self.database.transaction() as connection:
                connection.execute("SELECT 1").fetchone()
            return 200, {"status": "ready", "scope": "local_database_only"}
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(header, str) or not header.startswith("Bearer ") or not header[7:].strip():
            raise SupportOpsError("UNAUTHENTICATED")
        credential = header[7:]
        identity = self.workspaces._identity(credential)
        if identity.auth_mode != "external" and not (self.allow_local_sessions and identity.auth_mode == "local_session"):
            raise SupportOpsError("DEMO_DISABLED")
        if not isinstance(path, str) or not path.startswith("/") or "//" in path:
            raise SupportOpsError("NOT_FOUND")
        parts = path[1:].split("/")
        if parts[:2] != ["api", "workspaces"]:
            raise SupportOpsError("NOT_FOUND")
        try:
            query_text = environ.get("QUERY_STRING", "")
            if len(query_text) > 2000:
                raise ValueError("query too large")
            query = parse_qs(query_text, keep_blank_values=True, max_num_fields=5)
        except (ValueError, TypeError):
            raise SupportOpsError("INVALID_REQUEST") from None
        if set(query) - {"limit"} or any(len(values) != 1 for values in query.values()):
            raise SupportOpsError("INVALID_REQUEST")
        try:
            limit = int(query.get("limit", ["100"])[0])
        except ValueError:
            raise SupportOpsError("INVALID_REQUEST") from None
        limit = self.workspaces._limit(limit)
        if len(parts) == 2 and method == "GET":
            return 200, [row.model_dump() for row in self.workspaces.list_workspaces(credential, limit=limit)]
        if len(parts) < 4:
            raise SupportOpsError("NOT_FOUND")
        workspace, resource = parts[2:4]
        body = self._body(environ) if method in {"POST", "PUT", "DELETE"} else {}
        if resource == "tickets":
            if len(parts) == 4:
                if method == "GET":
                    return 200, self.tickets.list(credential, workspace, limit=limit)
                if method == "POST":
                    return 201, self.tickets.create(credential, workspace, body)
            if len(parts) == 5 and method == "GET":
                return 200, self.tickets.get(credential, workspace, parts[4])
            if len(parts) == 6 and method == "POST":
                if parts[5] == "suggestions":
                    return 200, self.tickets.suggest(credential, workspace, parts[4], body, request_id=request_id)
                if parts[5] in {"accept", "note", "resolve", "start"}:
                    return 200, self.tickets.operate(credential, workspace, parts[4], parts[5], body)
            if len(parts) == 8 and parts[5] == "suggestions" and parts[7] == "reconcile" and method == "POST":
                if body:
                    raise SupportOpsError("INVALID_REQUEST")
                return 200, self.tickets.reconcile(credential, workspace, parts[4], parts[6])
            raise SupportOpsError("NOT_FOUND")
        if resource == "conversations":
            if len(parts) == 4 and method == "POST":
                return 201, self.chats.create_conversation(credential, workspace, body)
            if len(parts) == 4 and method == "GET":
                return 200, self.chats.list_conversations(credential, workspace, limit=limit)
            if len(parts) == 5 and method == "GET":
                return 200, self.chats.get_conversation(credential, workspace, parts[4])
            if len(parts) == 6 and parts[5] == "messages" and method == "POST":
                return 200, self.chats.send_message(credential, workspace, parts[4], body, request_id=request_id)
        if resource == "answers":
            if len(parts) == 5 and method == "GET":
                return 200, self.chats.get_answer(credential, workspace, parts[4])
            if len(parts) == 7 and parts[5] == "citations" and method == "GET":
                return 200, self.chats.read_answer_citation(credential, workspace, parts[4], parts[6])
            if len(parts) == 6 and parts[5] == "feedback":
                if method == "PUT":
                    return 200, self.chats.put_feedback(credential, workspace, parts[4], body)
                if method == "GET":
                    return 200, self.chats.get_feedback(credential, workspace, parts[4])
        if resource == "runs" and len(parts) == 5 and method == "GET":
            return 200, self.chats.get_run(credential, workspace, parts[4])
        if resource == "documents":
            if len(parts) == 4 and method == "GET":
                return 200, [row.model_dump() for row in self.documents.list_documents(credential, workspace, limit=limit)]
            if len(parts) == 5 and method == "GET":
                return 200, self.documents.get_document(credential, workspace, parts[4]).model_dump()
            if method == "POST" and (len(parts) == 4 or (len(parts) == 6 and parts[5] == "versions")):
                body = dict(body)
                text = body.pop("content_text", None)
                if not isinstance(text, str):
                    raise SupportOpsError("INVALID_REQUEST")
                job = self.documents.upload(credential, workspace, body, text.encode("utf-8"),
                                            document_id=parts[4] if len(parts) == 6 else None)
                return 202, job.model_dump()
            if len(parts) == 5 and method == "DELETE":
                if body != {"confirm": True} or type(body["confirm"]) is not bool:
                    raise SupportOpsError("INVALID_REQUEST")
                return 200, self.documents.delete_document(credential, workspace, parts[4])
        if resource == "jobs":
            if len(parts) == 4 and method == "GET":
                return 200, [row.model_dump() for row in self.documents.list_jobs(credential, workspace, limit=limit)]
            if len(parts) == 5 and method == "GET":
                return 200, self.documents.get_job(credential, workspace, parts[4]).model_dump()
            if len(parts) == 6 and parts[5] == "retry" and method == "POST":
                if body:
                    raise SupportOpsError("INVALID_REQUEST")
                return 200, self.documents.retry_job(credential, workspace, parts[4]).model_dump()
        raise SupportOpsError("NOT_FOUND")
