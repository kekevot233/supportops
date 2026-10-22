"""本轮费用/隔离/网页契约定点验证；临时库，禁止真实网络和凭据读取。"""
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from supportops.answering import DemoQuoteLLM
from supportops.database import ApplicationDatabase
from supportops.errors import SupportOpsError
from supportops.live_pilot import GuardedPilotSDK, PilotCredentials, _wire_body
from supportops.local_server import LocalSessionIdentityProvider, LocalWorkbench
from supportops.spend_ledger import SpendLedger
from supportops.web_budget import WebSessionBudget
from supportops.web_live import IsolatedWebLLM, WebAnswerEngine, execute_web_worker, prepare_public_database, run_live_server

USAGE = {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}


def mock_worker(body):
    return {"text": DemoQuoteLLM().invoke(body["messages"]), "usage": USAGE,
            "error_code": None, "bound_violation": False}


class LiveWebChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "budget.sqlite3"
        SpendLedger(self.path)
        for name in ("connect", "connect_ex"):
            blocker = patch.object(socket.socket, name, side_effect=AssertionError("network forbidden"))
            blocker.start(); self.addCleanup(blocker.stop)
        self.credentials = patch("supportops.web_live.load_pilot_credentials", side_effect=AssertionError("real credentials forbidden"))
        self.credentials.start(); self.addCleanup(self.credentials.stop)

    def budget(self, **options):
        return WebSessionBudget(self.path, **{"session_id": "web-check", "budget_cny": "0.3",
            "request_budget_cny": "0.2", "max_requests": 2, "binding": "fixture", **options})

    def context(self, number=1):
        return {"owner": "demo-reader", "workspace": "helloagent", "conversation": "chat_one", "run_id": "run_" + str(number)}

    def code(self, expected, action):
        with self.assertRaises(SupportOpsError) as caught:
            action()
        self.assertEqual(caught.exception.code, expected)

    def test_limits_idempotency_and_restart_do_not_refund(self):
        budget = self.budget(max_requests=1)
        row = {"reserved_cny": "0.15", "request_sha256": "a" * 64}
        run = budget.begin_call(self.context(), row)
        self.code("WEB_SESSION_STOPPED", lambda: budget.begin_call(self.context(2), row))
        budget.finish_call(run, USAGE)
        restored = self.budget(max_requests=1)
        self.assertEqual(restored.session_view()["session_reserved_cny"], "0.15")
        self.code("WEB_BUDGET_EXCEEDED", lambda: restored.begin_call(self.context(2), row))
        self.code("CONFLICT", lambda: self.budget(max_requests=2))
        self.assertEqual(restored.run_view("run_1")["actual_usage"], USAGE)

    def test_cumulative_request_and_session_caps_before_send(self):
        budget = self.budget()
        self.code("WEB_BUDGET_EXCEEDED", lambda: budget.begin_call(self.context(), {"reserved_cny": "0.21"}))
        SpendLedger(self.path).reserve("prior-full32-fixture", [{"reserved_cny": "3.7"}])
        self.code("WEB_BUDGET_EXCEEDED", lambda: budget.begin_call(self.context(), {"reserved_cny": "0.15"}))
        self.assertEqual(budget.session_view()["request_attempts"], 0)

    def test_unknown_failure_stops_and_does_not_repeat_worker(self):
        budget, calls = self.budget(), []
        def fail(body):
            calls.append(body)
            raise TimeoutError("TEST_PRIVATE_PROVIDER_DETAIL")
        llm = IsolatedWebLLM(self.root, budget, runner=fail)
        messages = [{"role": "user", "content": "public question"}]
        self.code("MODEL_UNAVAILABLE", lambda: llm.invoke_for_run(messages, max_tokens=1024, context=self.context()))
        self.code("WEB_SESSION_STOPPED", lambda: llm.invoke_for_run(messages, max_tokens=1024, context=self.context(2)))
        self.assertEqual(len(calls), 1)
        self.assertIsNone(budget.run_view("run_1")["actual_usage"])
        self.assertTrue(self.budget().session_view()["stopped"])

    def test_explicit_flags_and_existing_ledger_required(self):
        self.code("EXTERNAL_CALLS_DISABLED", lambda: run_live_server(
            SimpleNamespace(enable_live=False, ack_public_data=True), project_root=self.root))
        self.code("WEB_BUDGET_NOT_READY", lambda: WebSessionBudget(self.root / "absent.sqlite3",
            session_id="web-new", budget_cny="1", request_budget_cny="0.2", max_requests=1, binding="x"))
        self.assertFalse((self.root / "absent.sqlite3").exists())

    def test_worker_fixed_non_thinking_sdk_contract_and_usage_no_real_key(self):
        body = _wire_body([{"role": "user", "content": "public"}], 1024, 0.0)
        calls = []
        class Root:
            def with_options(self, **options):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
                return self
            def create(self, **payload):
                calls.append(payload)
                return {"model": "deepseek-flash", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"status":"insufficient_evidence","passages":[]}'}}]}
            def close(self): pass
        with patch("supportops.web_live.worker_isolation_valid", return_value=True), patch(
                "supportops.web_live.load_pilot_credentials", return_value=PilotCredentials("deepseek-flash", "https://api.deepseek.com", "FAKE_ONLY_PRIVATE_KEY")):
            result = execute_web_worker(self.root, body, sdk_factory=lambda credentials, guard: GuardedPilotSDK(Root(), guard))
        self.assertEqual(calls, [body])
        self.assertEqual(result["usage"], USAGE)
        self.assertFalse(result["bound_violation"])
        self.assertNotIn("FAKE_ONLY_PRIVATE_KEY", json.dumps(result))

    def test_web_question_citation_run_feedback_and_idempotent_paid_slot(self):
        with ApplicationDatabase(self.root / "app.sqlite3") as database:
            corpus = prepare_public_database(database)
            budget, calls = self.budget(), []
            def worker(body):
                calls.append(body)
                return mock_worker(body)
            engine = WebAnswerEngine(IsolatedWebLLM(self.root, budget, runner=worker), corpus)
            identities = LocalSessionIdentityProvider(enabled=True)
            app = LocalWorkbench(database, identities, port=8766, engine=engine, live_budget=budget)
            token = identities.launch_url(app.origin).split("#access=")[1]
            def request(path, body=None, method=None):
                environ = {}; setup_testing_defaults(environ)
                raw = json.dumps(body).encode() if body is not None else b""
                environ.update(PATH_INFO=path, REQUEST_METHOD=method or ("POST" if body is not None else "GET"), HTTP_HOST=app.authority,
                    REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION="Bearer " + token, CONTENT_TYPE="application/json",
                    CONTENT_LENGTH=str(len(raw)), **{"wsgi.input": io.BytesIO(raw)})
                status = []
                value = json.loads(b"".join(app(environ, lambda code, headers, exc_info=None: status.append(int(code.split()[0])))))
                return status[0], value.get("data", value)
            base = "/api/workspaces/helloagent"
            self.assertEqual(request("/api/session")[1]["model_mode"], "local_deepseek")
            self.assertEqual(calls, [])
            self.assertEqual(request(base + "/documents", {})[0], 403)
            chat = request(base + "/conversations", {"title": "public support"})[1]["id"]
            question = {"question": "扫描版 PDF 为什么无法检索？", "idempotency_key": "once"}
            status, answer = request(base + f"/conversations/{chat}/messages", question)
            self.assertEqual(status, 200, answer)
            self.assertEqual(request(base + f"/conversations/{chat}/messages", question)[1]["id"], answer["id"])
            self.assertEqual(len(calls), 1)
            label = answer["citations"][0]["label"]
            self.assertEqual(request(base + f'/answers/{answer["id"]}/citations/{label}')[0], 200)
            run = request(base + f'/runs/{answer["run_id"]}')[1]
            self.assertEqual(run["actual_usage"], USAGE)
            self.assertEqual(run["external_model_attempts"], 1)
            self.assertIsNone(run["cost"])
            self.assertEqual(request(base + f'/answers/{answer["id"]}/feedback',
                {"rating": "useful", "reason": None, "comment": "offline fixture"}, method="PUT")[0], 200)
            self.assertLessEqual(float(budget.session_view()["session_reserved_cny"]), 0.2)
            # 完成态预算可重启，公开资料导入也保持幂等。
            self.assertEqual(prepare_public_database(database).sha256, corpus.sha256)

    def test_invalid_quote_retains_receipt_and_stops_without_history(self):
        with ApplicationDatabase(self.root / "app.sqlite3") as database:
            corpus = prepare_public_database(database)
            budget = self.budget()
            def worker(body):
                return {**mock_worker(body), "text": '{"status":"answered","passages":[{"label":"D1","quote":"NOT_IN_PUBLIC_CORPUS_123"}]}'}
            from supportops.chat import ChatService
            identities = LocalSessionIdentityProvider(enabled=True)
            token = identities.launch_url("http://127.0.0.1").split("#access=")[1]
            chat = ChatService(database, identities, WebAnswerEngine(IsolatedWebLLM(self.root, budget, runner=worker), corpus))
            conversation = chat.create_conversation(token, "helloagent", {"title": "public"})
            self.code("MODEL_RESPONSE_INVALID", lambda: chat.send_message(token, "helloagent", conversation["id"],
                {"question": "扫描版 PDF 为什么无法检索？", "idempotency_key": "bad"}))
            self.assertEqual(chat.get_conversation(token, "helloagent", conversation["id"])["messages"], [])
            self.assertTrue(budget.session_view()["stopped"])


if __name__ == "__main__":
    unittest.main()
