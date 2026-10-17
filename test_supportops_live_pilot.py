"""真实试跑入口的离线测试；不读取真实专用配置，不调用真实服务。"""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from supportops.__main__ import main
from supportops.errors import SupportOpsError
from supportops.live_pilot import (AUTHORIZATION_ID, GuardedPilotSDK, PilotCredentials, PilotSpendGuard, _wire_body,
    build_isolated_sdk,
    execute_worker, isolated_environment, load_pilot_credentials, prepare_pilot, run_live_pilot)
from supportops.qa_preflight import EndpointConfig
from supportops.seed import PROJECT_ROOT


FAKE_KEY = "fixture-secret-never-use-real-key"


class LiveFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for method in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            self.stack.enter_context(patch(method, side_effect=AssertionError("network forbidden")))

    def assert_code(self, expected, action):
        with self.assertRaises(SupportOpsError) as caught:
            action()
        self.assertEqual(caught.exception.code, expected)
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def credential_file(self, root, text=None):
        path = Path(root) / ".env.supportops"
        path.write_text(text if text is not None else (
            "LLM_MODEL_ID=deepseek-flash\nLLM_BASE_URL=https://api.deepseek.com\nLLM_API_KEY=" + FAKE_KEY), encoding="utf-8")
        return path

    def body(self, text="Return JSON"):
        return _wire_body([{"role": "user", "content": text}], 1024, 0.0)

    def response(self, **changes):
        result = {"model": "deepseek-flash", "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}
        result.update(changes)
        return result


class TestPilotCredentials(LiveFixture):
    def test_unauthorized_rejected_before_open(self):
        with patch("pathlib.Path.open", side_effect=AssertionError("must not read")):
            self.assert_code("EXTERNAL_CALLS_DISABLED", lambda: load_pilot_credentials(PROJECT_ROOT))

    def test_only_named_file_is_parsed_without_interpolation_or_environment(self):
        with tempfile.TemporaryDirectory() as root:
            self.credential_file(root)
            (Path(root) / ".env").write_text("not authorized", encoding="utf-8")
            with patch("os.environ", {}):
                credentials = load_pilot_credentials(Path(root), authorized=True)
            self.assertEqual(credentials.model, "deepseek-flash")
            self.assertNotIn(FAKE_KEY, repr(credentials))

    def test_empty_duplicate_unknown_bad_model_or_nonofficial_endpoint_rejected(self):
        good = "LLM_MODEL_ID=deepseek-flash\nLLM_BASE_URL=https://api.deepseek.com\nLLM_API_KEY=" + FAKE_KEY
        with tempfile.TemporaryDirectory() as root:
            for text in ("", good + "\nLLM_API_KEY=" + FAKE_KEY, good + "\nSMTP_SECRET=PRIVATE",
                         good.replace("deepseek-flash", "unapproved-model"),
                         good.replace("https://api.deepseek.com", "https://evil.example"),
                         good.replace(FAKE_KEY, "${OTHER_KEY}"), "x" * 16385):
                self.credential_file(root, text)
                self.assert_code("PILOT_CONFIG_INVALID", lambda: load_pilot_credentials(Path(root), authorized=True))

    def test_comments_quotes_bom_and_v1_are_supported(self):
        with tempfile.TemporaryDirectory() as root:
            self.credential_file(root, '\ufeff# private fixture\nLLM_MODEL_ID="deepseek-flash"\n'
                "LLM_BASE_URL='https://api.deepseek.com/v1'\nLLM_API_KEY=" + FAKE_KEY)
            self.assertEqual(load_pilot_credentials(Path(root), authorized=True).base_url, "https://api.deepseek.com/v1")

    def test_safe_child_environment_does_not_copy_secrets_proxies_or_headers(self):
        with patch("os.environ", {"SystemRoot": "C:\\Windows", "OPENAI_API_KEY": FAKE_KEY,
                                  "HTTP_PROXY": "PRIVATE_PROXY", "OPENAI_CUSTOM_HEADERS": FAKE_KEY}):
            env = isolated_environment()
        self.assertEqual(env["SystemRoot"], "C:\\Windows")
        self.assertNotIn(FAKE_KEY, json.dumps(env))
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("OPENAI_CUSTOM_HEADERS", env)

    def test_preflight_accepts_official_root_without_relaxing_other_url_rules(self):
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/"):
            self.assertEqual(EndpointConfig(provider_id="deepseek", region="not-attested", base_url=base,
                allowed_host="api.deepseek.com").base_url, base)
        with self.assertRaises(ValueError):
            EndpointConfig(provider_id="deepseek", region="not-attested", base_url="https://api.deepseek.com?key=x",
                           allowed_host="api.deepseek.com")


class TestPilotFinancialGuard(LiveFixture):
    def test_three_prepared_requests_reserve_less_than_one_cny_but_not_invoice(self):
        guard = PilotSpendGuard([self.body()] * 3)
        snapshot = guard.snapshot()
        self.assertLess(float(snapshot["reserved_plan_cny"]), 1.0)
        self.assertEqual(snapshot["authorized_budget_cny"], "5")
        self.assertEqual(snapshot["prior_unknown_cost_reserved_cny"], "1")
        self.assertEqual(snapshot["remaining_budget_for_this_round_cny"], "4")
        self.assertFalse(snapshot["provider_financial_hard_cap"])
        self.assertFalse(snapshot["provider_tokenizer_verified"])
        self.assertIsNone(snapshot["provider_invoice_cost_cny"])
        self.assertIsNone(snapshot["receipt_priced_upper_estimate_cny"])

    def test_request_or_output_size_is_rejected_before_sdk(self):
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: PilotSpendGuard([self.body("x" * 25000)]))
        oversized = self.body()
        oversized["max_tokens"] = 4096
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: PilotSpendGuard([oversized]))
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: PilotSpendGuard([self.body()] * 4))

    def test_request_payload_change_or_fourth_attempt_cannot_reach_sdk(self):
        body = self.body()
        guard = PilotSpendGuard([body] * 3)
        sdk = Mock(return_value=self.response())
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: guard.invoke(sdk, self.body("changed")))
        sdk.assert_not_called()
        for _ in range(3):
            guard.invoke(sdk, body)
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: guard.invoke(sdk, body))
        self.assertEqual(sdk.call_count, 3)

    def test_sdk_failure_stops_remaining_requests_and_preserves_unknown_cost(self):
        guard = PilotSpendGuard([self.body()] * 3)
        error = RuntimeError(FAKE_KEY)
        sdk = Mock(side_effect=error)
        self.assert_code("MODEL_UNAVAILABLE", lambda: guard.invoke(sdk, self.body()))
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: guard.invoke(sdk, self.body()))
        self.assertEqual(sdk.call_count, 1)
        self.assertIsNone(guard.snapshot()["receipt_priced_upper_estimate_cny"])
        self.assertNotIn(FAKE_KEY, json.dumps(guard.snapshot()))

    def test_missing_usage_stops_without_invented_zero(self):
        guard = PilotSpendGuard([self.body()])
        self.assert_code("MODEL_UNAVAILABLE", lambda: guard.invoke(Mock(return_value={}), self.body()))
        self.assertIsNone(guard.snapshot()["known_receipt_priced_subtotal_cny"])

    def test_usage_beyond_reserved_bound_stops_later_requests(self):
        guard = PilotSpendGuard([self.body()] * 3)
        response = self.response(usage={"prompt_tokens": 12, "completion_tokens": 2048, "total_tokens": 2060})
        guard.invoke(Mock(return_value=response), self.body())
        self.assertTrue(guard.snapshot()["stopped_after_failure_or_bound_violation"])
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: guard.invoke(Mock(), self.body()))

    def test_wrapper_injects_non_thinking_and_preserves_root_ownership(self):
        body = self.body()
        guard = PilotSpendGuard([body])
        root = Mock()
        root.with_options.return_value = root
        root.chat.completions.create.return_value = self.response()
        wrapper = GuardedPilotSDK(root, guard)
        view = wrapper.with_options(max_retries=0, timeout=30)
        kwargs = {key: value for key, value in body.items() if key != "reasoning_effort"}
        view.chat.completions.create(**kwargs)
        self.assertEqual(root.chat.completions.create.call_args.kwargs["reasoning_effort"], "none")
        wrapper.close()
        root.close.assert_called_once()

    def test_journal_event_precedes_sdk_and_write_failure_blocks_network(self):
        order = []
        guard = PilotSpendGuard([self.body()])
        guard.event_sink = lambda event: order.append(event["event"])
        def create(**kwargs):
            order.append("sdk_called")
            return self.response()
        guard.invoke(create, self.body())
        self.assertEqual(order, ["before_sdk_create", "sdk_called", "response_usage_received"])
        blocked = PilotSpendGuard([self.body()])
        blocked.event_sink = Mock(side_effect=OSError("fixture disk failure"))
        sdk = Mock()
        self.assert_code("MODEL_UNAVAILABLE", lambda: blocked.invoke(sdk, self.body()))
        sdk.assert_not_called()


class TestPilotExecution(LiveFixture):
    def test_prepare_uses_only_three_fixed_public_cases_without_credentials(self):
        with patch("supportops.live_pilot.load_pilot_credentials", side_effect=AssertionError("no secrets")):
            config, corpus, selected, preflight, fake, guard = prepare_pilot(PROJECT_ROOT)
        self.assertEqual([case.id for case in selected.cases], ["RET-02", "REF-01", "INJ-01"])
        self.assertIsNone(config.embedding)
        self.assertTrue(preflight["offline_checks_passed"])
        self.assertEqual(len(guard.plan), 3)
        self.assertEqual(fake["summary"]["case_count"], 3)
        self.assertEqual(corpus.sha256, config.scope.corpus_sha256)

    def test_worker_fake_sdk_uses_actual_adapter_and_reports_receipts_without_key(self):
        from test_supportops_provider_adapters import FakeSDK
        with tempfile.TemporaryDirectory() as root:
            self.credential_file(root)
            prepared = prepare_pilot(PROJECT_ROOT)
            with patch("supportops.live_pilot.prepare_pilot", return_value=prepared):
                report = execute_worker(Path(root), authorized=True,
                    sdk_factory=lambda credentials, guard: GuardedPilotSDK(FakeSDK(), guard))
        self.assertEqual(report["financial_guard"]["sdk_create_attempts"], 3)
        self.assertEqual(report["external_embedding_calls"], 0)
        self.assertEqual(report["provider_calls"]["chat"]["usage_receipt_count"], 3)
        self.assertNotIn(FAKE_KEY, json.dumps(report))

    def test_unauthorized_parent_and_worker_never_prepare_or_read_credentials(self):
        with patch("supportops.live_pilot.prepare_pilot", side_effect=AssertionError("must not prepare")):
            self.assert_code("EXTERNAL_CALLS_DISABLED", lambda: execute_worker(PROJECT_ROOT))
            self.assert_code("EXTERNAL_CALLS_DISABLED", lambda: run_live_pilot(PROJECT_ROOT, Path("outputs/no.json")))

    def test_parent_exclusive_authorization_and_safe_process_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "outputs" / "result.json"
            response = {"authorization_id": AUTHORIZATION_ID, "financial_guard": {"sdk_create_attempts": 3},
                        "summary": {"quality_gate_passed": False}}
            process = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response).encode()))
            with patch("supportops.live_pilot.prepare_pilot"), patch("supportops.live_pilot.subprocess.run", process):
                self.assertEqual(run_live_pilot(root, target, authorized=True), response)
                self.assert_code("PILOT_ALREADY_RESERVED", lambda: run_live_pilot(root, root / "outputs" / "another.json", authorized=True))
            self.assertEqual(process.call_count, 1)
            options = process.call_args.kwargs
            self.assertNotIn(FAKE_KEY, repr(process.call_args))
            self.assertEqual(process.call_args.args[0][1], "-I")
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(options["timeout"], 150)

    def test_failed_process_keeps_reservation_and_never_auto_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = Mock(side_effect=subprocess.TimeoutExpired("private-command", 150))
            with patch("supportops.live_pilot.prepare_pilot"), patch("supportops.live_pilot.subprocess.run", process):
                self.assert_code("PILOT_PROCESS_FAILED", lambda: run_live_pilot(root, root / "outputs" / "r.json", authorized=True))
                self.assert_code("PILOT_ALREADY_RESERVED", lambda: run_live_pilot(root, root / "outputs" / "r.json", authorized=True))
            self.assertEqual(process.call_count, 1)

    def test_output_escape_is_rejected_before_preparation_or_grant(self):
        with patch("supportops.live_pilot.prepare_pilot", side_effect=AssertionError("must not prepare")):
            with self.assertRaises(ValueError):
                run_live_pilot(PROJECT_ROOT, PROJECT_ROOT / "README.md", authorized=True)

    def test_cli_requires_flag_and_uses_sanitized_errors(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["run-deepseek-pilot", "--output", "outputs/never-created.json"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(err.getvalue())["code"], "EXTERNAL_CALLS_DISABLED")

    def test_worker_json_is_safe_on_gbk_stdout_with_chinese_and_emoji(self):
        from scripts.supportops_live_pilot_worker import emit_payload
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk")
        payload = {"message": "中文报告🧪", "authorization_id": AUTHORIZATION_ID}
        with redirect_stdout(stream):
            emit_payload(payload)
        stream.flush()
        self.assertEqual(json.loads(raw.getvalue().decode("utf-8")), payload)
        stream.detach()

    def test_parent_recovers_worker_report_even_if_stdout_encoding_is_bad(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = {"authorization_id": AUTHORIZATION_ID, "financial_guard": {"sdk_create_attempts": 3},
                        "summary": {"quality_gate_passed": False}, "message": "中文"}
            def run(*args, **kwargs):
                recovery = root / "outputs" / "supportops" / (AUTHORIZATION_ID + ".grant.worker-report.json")
                recovery.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="中文报告".encode("gbk"))
            with patch("supportops.live_pilot.prepare_pilot"), patch("supportops.live_pilot.subprocess.run", side_effect=run):
                self.assertEqual(run_live_pilot(root, root / "outputs" / "r.json", authorized=True), response)


class TestPilotInstalledSDK(LiveFixture):
    def sdk(self, handler):
        import openai
        import httpx2
        self.stack.enter_context(patch("openai._client.os", SimpleNamespace(environ={})))
        self.stack.enter_context(patch("supportops.live_pilot.worker_isolation_valid", return_value=True))
        transport_factory = self.stack.enter_context(patch("httpx2.HTTPTransport", return_value=httpx2.MockTransport(handler)))
        guard = PilotSpendGuard([self.body()])
        client = build_isolated_sdk(PilotCredentials("deepseek-flash", "https://api.deepseek.com", FAKE_KEY), guard)
        self.addCleanup(client.close)
        return client, guard, transport_factory, httpx2

    def test_installed_sdk_wire_request_is_guarded_and_non_thinking(self):
        requests = []
        def handler(request):
            requests.append(request)
            return lib.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
                **self.response(), "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"status":"insufficient_evidence","passages":[]}'}}]})
        sdk, guard, transport_factory, lib = self.sdk(handler)
        kwargs = {key: value for key, value in self.body().items() if key != "reasoning_effort"}
        sdk.with_options(max_retries=0, timeout=30).chat.completions.create(**kwargs)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(json.loads(requests[0].content)["reasoning_effort"], "none")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(guard.snapshot()["sdk_create_attempts"], 1)
        transport_factory.assert_called_once_with(trust_env=False, retries=0, verify=True)

    def test_installed_sdk_500_is_single_attempt_and_never_echoes_body(self):
        requests = []
        def handler(request):
            requests.append(request)
            return lib.Response(500, json={"error": {"message": FAKE_KEY}})
        sdk, guard, _, lib = self.sdk(handler)
        kwargs = {key: value for key, value in self.body().items() if key != "reasoning_effort"}
        self.assert_code("MODEL_UNAVAILABLE", lambda: sdk.with_options(max_retries=0, timeout=30).chat.completions.create(**kwargs))
        self.assertEqual(len(requests), 1)
        self.assertEqual(guard.snapshot()["calls"][0]["http_status"], 500)
        self.assertNotIn(FAKE_KEY, json.dumps(guard.snapshot()))


if __name__ == "__main__":
    unittest.main()
