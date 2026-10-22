"""仅验证新增工单权限、迁移与闭环；Fake 模型、临时库，禁止外网。"""
import json
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from supportops.answering import AnswerEngine, DemoQuoteLLM
from supportops.chat import ChatService, HIDDEN_ANSWER
from supportops.chat_schema import CHAT_SCHEMA
from supportops.database import APPLICATION_ID, ApplicationDatabase, _P2_SCHEMA, _SCHEMA
from supportops.documents import DocumentService
from supportops.errors import SupportOpsError
from supportops.identity import DemoIdentityProvider
from supportops.tickets import TicketService
from supportops.workspaces import WorkspaceService


class TicketChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tickets.sqlite3"
        self.db = ApplicationDatabase(self.path); self.addCleanup(self.db.close)
        self.db.bootstrap_demo(enabled=True)
        self.identities = DemoIdentityProvider(enabled=True)
        self.docs = DocumentService(self.db, self.identities)
        self.calls, self.response, self.callback = [], None, None
        outer = self
        class LLM:
            def invoke(self, messages, **kwargs):
                outer.calls.append(messages)
                if outer.callback: outer.callback()
                return outer.response if outer.response is not None else DemoQuoteLLM().invoke(messages, **kwargs)
        self.chats = ChatService(self.db, self.identities, AnswerEngine(LLM()), documents=self.docs)
        self.now = 1000.0
        self.service = TicketService(self.db, self.identities, self.chats, clock=lambda: self.now)
        for method in ("connect", "connect_ex"):
            blocker = patch.object(socket.socket, method, side_effect=AssertionError("No network"))
            blocker.start(); self.addCleanup(blocker.stop)

    def code(self, code, action):
        with self.assertRaises(SupportOpsError) as caught: action()
        self.assertEqual(caught.exception.code, code)

    def ticket(self, key="create"):
        return self.service.create("reader", "helloagent", {"title": "服务端口问题", "description": "服务端口应配置为什么？",
            "category": "installation", "priority": "normal", "idempotency_key": key})

    def publish(self, content="# 支持说明\n\n服务端口为8080。", document_id=None):
        job = self.docs.upload("admin", "helloagent", {"filename": "guide.md", "title": "支持说明", "idempotency_key": "doc" + str(len(self.calls))},
            content.encode(), document_id=document_id)
        self.assertEqual(self.docs.process_next("admin", "helloagent").state, "succeeded")
        return job

    def suggest(self, ticket):
        return self.service.suggest("reader", "helloagent", ticket["id"], {"expected_revision": ticket["revision"], "idempotency_key": "suggest"})

    def test_complete_human_workflow_idempotency_and_private_restart(self):
        self.publish(); first = self.ticket()
        self.assertEqual(self.ticket()["id"], first["id"]); self.assertEqual(self.calls, [])
        self.code("TICKET_STATE_INVALID", lambda: self.service.operate("reader", "helloagent", first["id"], "resolve",
            {"expected_revision": 0, "idempotency_key": "early", "content": "不能跳过审核", "confirm": True}))
        ticket = self.suggest(first)
        self.assertEqual(ticket["state"], "open"); self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.suggest(first)["id"], first["id"]); self.assertEqual(len(self.calls), 1)
        suggestion = ticket["suggestions"][0]
        self.assertIn("8080", suggestion["answer"]["content"])
        payload = {"expected_revision": ticket["revision"], "idempotency_key": "accept", "suggestion_id": suggestion["id"], "confirm": True}
        self.code("INVALID_REQUEST", lambda: self.service.operate("reader", "helloagent", first["id"], "accept", {**payload, "confirm": 1}))
        ticket = self.service.operate("reader", "helloagent", first["id"], "accept", payload)
        self.assertEqual(ticket["state"], "in_progress")
        self.assertEqual(self.service.operate("reader", "helloagent", first["id"], "accept", payload)["revision"], ticket["revision"])
        self.code("CONFLICT", lambda: self.service.operate("reader", "helloagent", first["id"], "note",
            {"expected_revision": 0, "idempotency_key": "stale", "content": "过期版本"}))
        ticket = self.service.operate("reader", "helloagent", first["id"], "note",
            {"expected_revision": ticket["revision"], "idempotency_key": "note", "content": "人工核对配置。"})
        ticket = self.service.operate("reader", "helloagent", first["id"], "resolve",
            {"expected_revision": ticket["revision"], "idempotency_key": "resolve", "content": "人工修正配置并确认恢复。", "confirm": True})
        self.assertEqual(ticket["state"], "resolved"); self.assertEqual(len(self.calls), 1)
        self.code("NOT_FOUND", lambda: self.service.get("admin", "helloagent", first["id"]))
        self.code("NOT_FOUND", lambda: self.service.get("reader", "internal-lab", first["id"]))
        self.db.close()
        with ApplicationDatabase(self.path) as restored:
            self.assertEqual(restored._connection.execute("SELECT state FROM tickets").fetchone()[0], "resolved")

    def test_source_deletion_hides_suggestion_and_blocks_acceptance(self):
        job = self.publish(); ticket = self.suggest(self.ticket()); suggestion = ticket["suggestions"][0]
        self.docs.delete_document("admin", "helloagent", job.document_id)
        ticket = self.service.get("reader", "helloagent", ticket["id"])
        self.assertEqual(ticket["suggestions"][0]["answer"]["content"], HIDDEN_ANSWER)
        self.code("TICKET_EVIDENCE_INVALID", lambda: self.service.operate("reader", "helloagent", ticket["id"], "accept",
            {"expected_revision": ticket["revision"], "idempotency_key": "accept", "suggestion_id": suggestion["id"], "confirm": True}))
        self.assertNotIn("8080", json.dumps(ticket, ensure_ascii=False))

    def test_insufficient_evidence_and_failed_calls_never_auto_accept_or_retry(self):
        ticket = self.suggest(self.ticket())
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(ticket["suggestions"][0]["answer"]["status"], "insufficient_evidence")
        self.code("TICKET_EVIDENCE_INVALID", lambda: self.service.operate("reader", "helloagent", ticket["id"], "accept",
            {"expected_revision": ticket["revision"], "idempotency_key": "accept", "suggestion_id": ticket["suggestions"][0]["id"], "confirm": True}))
        manual = self.service.operate("reader", "helloagent", ticket["id"], "start",
            {"expected_revision": ticket["revision"], "idempotency_key": "manual", "content": "无证据，人工排查。", "confirm": True})
        self.assertEqual(manual["state"], "in_progress"); self.assertIsNone(manual["accepted_suggestion_id"])
        self.assertEqual(self.calls, [])
        self.publish(); self.response = "invalid response"
        ticket = self.ticket("second"); payload = {"expected_revision": 0, "idempotency_key": "bad"}
        self.code("MODEL_RESPONSE_INVALID", lambda: self.service.suggest("reader", "helloagent", ticket["id"], payload))
        self.assertEqual(self.service.suggest("reader", "helloagent", ticket["id"], payload)["suggestions"][0]["state"], "failed")
        self.assertEqual(len(self.calls), 1)

    def test_reconcile_links_committed_answer_without_reinvoking_and_marks_empty_interrupt(self):
        self.publish(); ticket = self.ticket()
        with patch.object(self.chats, "send_message", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): self.suggest(ticket)
        ticket = self.service.get("reader", "helloagent", ticket["id"]); suggestion = ticket["suggestions"][0]
        self.chats.send_message("reader", "helloagent", suggestion["conversation_id"],
            {"question": "服务端口是什么？", "idempotency_key": "ticket-suggestion"})
        result = self.service.reconcile("reader", "helloagent", ticket["id"], suggestion["id"])
        self.assertEqual(result["suggestions"][0]["state"], "ready"); self.assertEqual(len(self.calls), 1)
        second = self.ticket("second")
        with patch.object(self.chats, "send_message", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): self.suggest(second)
        second = self.service.get("reader", "helloagent", second["id"]); self.now += 301
        second = self.service.reconcile("reader", "helloagent", second["id"], second["suggestions"][0]["id"])
        self.assertEqual(second["suggestions"][0]["error_code"], "TICKET_INTERRUPTED"); self.assertEqual(len(self.calls), 1)

    def test_revocation_during_generation_never_exposes_or_accepts_result(self):
        self.publish(); ticket = self.ticket()
        self.callback = lambda: WorkspaceService(self.db, self.identities).remove_member("admin", "helloagent", "demo-reader")
        self.code("NOT_FOUND", lambda: self.suggest(ticket))
        self.code("NOT_FOUND", lambda: self.service.get("reader", "helloagent", ticket["id"]))
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT state FROM ticket_suggestions").fetchone()[0], "failed")

    def test_additive_migrations_keep_old_data_and_roles(self):
        for version in (1, 2, 3):
            path = Path(self.temp.name) / f"old{version}.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                for statement in (*_SCHEMA, *(_P2_SCHEMA if version >= 2 else ()), *(CHAT_SCHEMA if version >= 3 else ())): connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}"); connection.execute(f"PRAGMA user_version={version}")
                connection.execute("INSERT INTO users VALUES('kept','kept',0,'old')")
                connection.execute("INSERT INTO workspaces VALUES('kept','kept','kept','old')")
                connection.execute("INSERT INTO memberships VALUES('kept','kept','reader','old')")
                connection.commit()
            with ApplicationDatabase(path) as database:
                with database.transaction() as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                    self.assertEqual(connection.execute("SELECT active FROM users").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT role FROM memberships").fetchone()[0], "reader")
                    self.assertEqual(connection.execute("SELECT count(*) FROM tickets").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
