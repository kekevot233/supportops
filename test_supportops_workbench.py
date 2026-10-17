"""P4：本机认证/HTTP 边界、静态资源、真实 localhost HTTP 与完整应用流程。"""

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

from supportops import ApplicationDatabase, AnswerEngine, DemoIdentityProvider, DemoQuoteLLM, SupportOpsError
from supportops.api import SupportOpsAPI
from supportops.local_server import LocalSessionIdentityProvider, LocalWorkbench, QuietHandler


class WorkbenchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = ApplicationDatabase(Path(self.tmp.name) / "app.sqlite3")
        self.addCleanup(self.db.close)
        self.db.bootstrap_demo(enabled=True)
        self.identities = LocalSessionIdentityProvider(enabled=True, profile="admin")
        self.reader = LocalSessionIdentityProvider(enabled=True)
        self.app = LocalWorkbench(self.db, self.identities, port=8765)
        self.reader_app = LocalWorkbench(self.db, self.reader, port=8765)
        self.token = self.identities.launch_url(self.app.origin).split("#access=")[1]
        self.reader_token = self.reader.launch_url(self.app.origin).split("#access=")[1]

    def request(self, path, body=None, *, method=None, app=None, token=None, headers=None):
        env = {}; setup_testing_defaults(env)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        env.update(PATH_INFO=path, REQUEST_METHOD=method or ("POST" if body is not None else "GET"),
                   HTTP_HOST="127.0.0.1:8765", REMOTE_ADDR="127.0.0.1", QUERY_STRING="", CONTENT_LENGTH=str(len(raw)),
                   CONTENT_TYPE="application/json", HTTP_AUTHORIZATION="Bearer " + (self.token if token is None else token),
                   **{"wsgi.input": io.BytesIO(raw)})
        env.update(headers or {})
        response = {}
        def start(status, result_headers, exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(result_headers))
        iterable = validator(app or self.app)(env, start)
        try:
            response["raw"] = b"".join(iterable)
        finally:
            if hasattr(iterable, "close"): iterable.close()
        if "application/json" in response["headers"]["Content-Type"]:
            response["json"] = json.loads(response["raw"])
            response["data"] = response["json"].get("data")
        return response

    def reader_request(self, path, body=None, **kwargs):
        return self.request(path, body, app=self.reader_app, token=self.reader_token, **kwargs)

    def upload(self, text="# 测试手册\n\n服务端口为 8080。", *, doc=None, key="v1"):
        url = "/api/workspaces/helloagent/documents" + (f"/{doc}/versions" if doc else "")
        response = self.request(url, {"filename":"guide.md", "title":"测试手册", "content_text":text, "idempotency_key":key})
        self.assertEqual(response["status"], 202)
        return response["data"]

    def work(self):
        return self.request("/api/workspaces/helloagent/jobs/process-next", {})["data"]["job"]

    def ask(self, chat, key="q1"):
        return self.reader_request(f"/api/workspaces/helloagent/conversations/{chat}/messages",
                                   {"question":"服务端口是什么？", "idempotency_key":key})

    def new_chat(self):
        return self.reader_request("/api/workspaces/helloagent/conversations", {"title":"本人会话"})["data"]["id"]


class TestLocalSession(WorkbenchFixture):
    def test_requires_explicit_enable_and_valid_profile(self):
        for action in (lambda: LocalSessionIdentityProvider(), lambda: LocalSessionIdentityProvider(enabled=True, profile="owner")):
            with self.assertRaises(SupportOpsError): action()

    def test_random_tokens_are_not_public_profiles_or_cross_process_credentials(self):
        self.assertNotEqual(self.token, self.reader_token)
        for bad in ("admin", "reader", self.reader_token, "\ud800", None, "x" * 101):
            with self.assertRaises(SupportOpsError) as caught: self.identities.authenticate(bad)
            self.assertEqual(caught.exception.code, "UNAUTHENTICATED")
        self.assertEqual(self.identities.authenticate(self.token).auth_mode, "local_session")

    def test_token_expiry_is_absolute(self):
        now = [0.0]
        provider = LocalSessionIdentityProvider(enabled=True, clock=lambda: now[0])
        token = provider.launch_url(self.app.origin).split("=")[1]
        now[0] = 28799; provider.authenticate(token)
        now[0] = 28800
        with self.assertRaises(SupportOpsError): provider.authenticate(token)

    def test_original_api_still_rejects_cli_and_default_rejects_local_sessions(self):
        with self.assertRaises(SupportOpsError): SupportOpsAPI(self.db, DemoIdentityProvider(enabled=True), AnswerEngine(DemoQuoteLLM()))
        app = SupportOpsAPI(self.db, self.identities, AnswerEngine(DemoQuoteLLM()))
        response = self.request("/api/workspaces", app=app)
        self.assertEqual(response["json"]["code"], "DEMO_DISABLED")

    def test_local_wrapper_does_not_accept_an_arbitrary_provider(self):
        with self.assertRaises(SupportOpsError): LocalWorkbench(self.db, DemoIdentityProvider(enabled=True), port=8765)

    def test_reader_and_admin_fixed_server_identity(self):
        self.assertEqual(self.reader_request("/api/session")["data"]["identity"]["subject"], "demo-reader")
        self.assertEqual(self.request("/api/session")["data"]["identity"]["subject"], "demo-admin")
        self.assertEqual(self.request("/api/session", token="admin")["status"], 401)


class TestHttpBoundary(WorkbenchFixture):
    def test_exact_host_origin_and_remote_address(self):
        for headers in ({"HTTP_HOST":"evil.test:8765"}, {"HTTP_HOST":"localhost:8765"},
                        {"HTTP_ORIGIN":"https://evil.test"}, {"HTTP_ORIGIN":"null"},
                        {"HTTP_SEC_FETCH_SITE":"cross-site"}, {"REMOTE_ADDR":"192.168.1.5"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/", headers=headers)["status"], 403)
        self.assertEqual(self.request("/", headers={"HTTP_ORIGIN":self.app.origin, "HTTP_SEC_FETCH_SITE":"same-origin"})["status"], 200)

    def test_static_assets_have_security_headers_and_no_auth_token(self):
        for path in ("/", "/app.js", "/styles.css"):
            response = self.request(path, token="")
            self.assertEqual(response["status"], 200)
            self.assertIn("frame-ancestors 'none'", response["headers"]["Content-Security-Policy"])
            self.assertEqual(response["headers"]["Referrer-Policy"], "no-referrer")
            self.assertEqual(response["headers"]["Cache-Control"], "no-store")
            self.assertNotIn(self.token.encode(), response["raw"])

    def test_no_filesystem_browsing(self):
        for path in ("/../HANDOFF.md", "/.env", "/web/dist/index.html", "/favicon.ico"):
            self.assertEqual(self.request(path)["status"], 404)

    def test_api_no_credential_and_cookie_only_are_rejected(self):
        for headers in ({}, {"HTTP_COOKIE":"access="+self.token}):
            self.assertEqual(self.request("/api/workspaces", token="", headers=headers)["status"], 401)

    def test_process_next_body_and_role_are_checked(self):
        self.assertEqual(self.reader_request("/api/workspaces/helloagent/jobs/process-next", {})["status"], 403)
        self.assertEqual(self.request("/api/workspaces/helloagent/jobs/process-next", {"command":"anything"})["status"], 400)
        self.assertEqual(self.request("/api/workspaces/internal-lab/jobs/process-next", {}, token=self.reader_token,
                                     app=self.reader_app)["status"], 404)
        self.assertEqual(self.request("/api/workspaces/helloagent/jobs/process-next")["status"], 404)

    def test_live_http_loopback_server(self):
        with make_server("127.0.0.1", 0, self.app, handler_class=QuietHandler) as server:
            self.app.origin=f"http://127.0.0.1:{server.server_port}"; self.app.authority=f"127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with urlopen(Request(self.app.origin+"/api/session", headers={"Authorization":"Bearer "+self.token}), timeout=3) as response:
                    self.assertEqual(json.load(response)["data"]["external_model_calls"], 0)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(self.app.origin+"/api/workspaces", headers={"Origin":"https://evil.test", "Authorization":"Bearer "+self.token}), timeout=3)
                self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown(); thread.join(timeout=3)


class TestApplicationHttpFlow(WorkbenchFixture):
    def test_independent_space_corpus_answers_and_runs_cannot_be_read_or_spoofed(self):
        prefix="/api/workspaces/internal-lab"
        job=self.request(prefix+"/documents",{"filename":"internal.md","title":"B 独立资料",
            "content_text":"# B 空间\n\nB_ONLY_TEST_CANARY 端口为 7070。","idempotency_key":"b-doc"})["data"]
        self.assertEqual(self.request(prefix+"/jobs/process-next",{})["data"]["job"]["state"],"succeeded")
        chat=self.request(prefix+"/conversations",{"title":"B 会话"})["data"]["id"]
        answer=self.request(prefix+f"/conversations/{chat}/messages",{"question":"B 的端口？","idempotency_key":"b-q"})["data"]
        self.assertIn("B_ONLY_TEST_CANARY",answer["content"])
        for path in ("/documents", f'/documents/{job["document_id"]}', f'/jobs/{job["id"]}',
                     f"/conversations/{chat}", f'/answers/{answer["id"]}/citations/D1', f'/runs/{answer["run_id"]}'):
            response=self.reader_request(prefix+path)
            self.assertEqual(response["status"],404)
            self.assertNotIn("B_ONLY_TEST_CANARY",response["raw"].decode())
        denied=self.reader_request(prefix+f"/conversations/{chat}/messages",{"question":"B", "idempotency_key":"spoof", "user_id":"demo-admin"})
        self.assertEqual(denied["status"],404)

    def test_upload_publish_question_quote_feedback_and_replay(self):
        job = self.upload(); self.assertEqual(self.work()["state"], "succeeded")
        self.assertEqual(self.request("/api/workspaces/helloagent/jobs")["data"][0]["id"], job["id"])
        chat = self.new_chat(); answer = self.ask(chat)["data"]
        self.assertIn("8080", answer["content"])
        source = self.reader_request(f'/api/workspaces/helloagent/answers/{answer["id"]}/citations/D1')["data"]
        self.assertEqual(source["version_id"], job["version_id"])
        self.assertTrue(source["is_current"])
        feedback_url=f'/api/workspaces/helloagent/answers/{answer["id"]}/feedback'
        feedback={"rating":"not_useful", "reason":"irrelevant", "comment":"需要语义核对"}
        self.assertEqual(self.reader_request(feedback_url, feedback, method="PUT")["status"], 200)
        self.assertEqual(self.reader_request(feedback_url)["data"], feedback)
        self.assertEqual(self.ask(chat)["data"]["id"], answer["id"])
        history=self.reader_request(f"/api/workspaces/helloagent/conversations/{chat}")["data"]["messages"]
        self.assertEqual(len(history), 2); self.assertEqual(history[1]["answer_id"], answer["id"])
        run=self.reader_request(f'/api/workspaces/helloagent/runs/{answer["run_id"]}')["data"]
        self.assertEqual(run["model_mode"], "fake_extractive"); self.assertIsNone(run["cost"])

    def test_workspace_and_private_resource_denials(self):
        self.upload(); self.work(); chat=self.new_chat(); answer=self.ask(chat)["data"]
        for path in (f"/conversations/{chat}",f'/answers/{answer["id"]}',f'/answers/{answer["id"]}/citations/D1',f'/runs/{answer["run_id"]}'):
            self.assertEqual(self.request("/api/workspaces/helloagent"+path)["status"],404)
            self.assertEqual(self.reader_request("/api/workspaces/internal-lab"+path)["status"],404)
        response=self.reader_request(f"/api/workspaces/helloagent/conversations/{chat}/messages",
                                    {"question":"spoof", "idempotency_key":"q2", "user_id":"demo-admin"})
        self.assertEqual(response["status"],400)

    def test_failed_version_preserves_old_then_publish_and_delete_invalidate(self):
        first=self.upload(); self.work(); chat=self.new_chat(); old=self.ask(chat)["data"]
        second=self.upload("# 测试手册\n\n服务端口为 9090。",doc=first["document_id"],key="v2")
        with patch.object(self.app.api.documents._index,"build",side_effect=RuntimeError("private-backend-detail")):
            failed=self.work()
        self.assertEqual(failed["state"],"failed")
        self.assertIn("8080",self.ask(chat,key="q2")["data"]["content"])
        self.request(f'/api/workspaces/helloagent/jobs/{second["id"]}/retry',{})
        self.assertEqual(self.work()["state"],"succeeded")
        self.assertIn("9090",self.ask(chat,key="q3")["data"]["content"])
        answer_path=f'/api/workspaces/helloagent/answers/{old["id"]}'
        self.assertEqual(self.reader_request(answer_path)["data"]["source_state"],"historical")
        self.assertFalse(self.reader_request(answer_path+"/citations/D1")["data"]["is_current"])
        self.request(f'/api/workspaces/helloagent/documents/{first["document_id"]}',{"confirm":True},method="DELETE")
        self.assertEqual(self.reader_request(answer_path)["data"]["source_state"],"deleted")
        self.assertEqual(self.reader_request(answer_path+"/citations/D1")["status"],410)
        self.assertEqual(self.ask(chat,key="q4")["data"]["status"],"insufficient_evidence")

    def test_model_failure_is_not_insufficient_evidence_and_history_rolls_back(self):
        self.upload();self.work();chat=self.new_chat();self.ask(chat)
        with patch.object(self.reader_app.api.chats._engine.llm,"invoke",side_effect=RuntimeError("secret-provider-detail")):
            failed=self.ask(chat,key="q2")
        self.assertEqual(failed["status"],503);self.assertEqual(failed["json"]["code"],"MODEL_UNAVAILABLE")
        self.assertNotIn("secret-provider-detail",failed["raw"].decode())
        run=self.reader_request(f'/api/workspaces/helloagent/runs/{failed["json"]["run_id"]}')["data"]
        self.assertEqual(run["state"],"failed")
        self.assertEqual(len(self.reader_request(f'/api/workspaces/helloagent/conversations/{chat}')["data"]["messages"]),2)

    def test_empty_workspace_short_circuits_without_client_call(self):
        chat=self.new_chat()
        with patch.object(self.reader_app.api.chats._engine.llm,"invoke",side_effect=AssertionError("must not call")):
            answer=self.ask(chat)["data"]
        self.assertEqual(answer["status"],"insufficient_evidence")

    def test_revocation_invalidates_existing_token(self):
        chat=self.new_chat()
        self.app.api.workspaces.remove_member(self.token,"helloagent","demo-reader")
        self.assertEqual(self.reader_request(f"/api/workspaces/helloagent/conversations/{chat}")["status"],404)
        self.assertEqual(self.reader_request("/api/workspaces")["data"],[])


if __name__ == "__main__":
    unittest.main()
