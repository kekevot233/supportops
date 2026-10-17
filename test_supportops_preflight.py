"""P5c-1：预检永不联网、不获取凭据、不把可审阅配置当成运行授权。"""

import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from supportops.__main__ import main
from supportops.errors import SupportOpsError
from supportops.provider_adapters import EmbeddingSDKConfig, plan_embedding_batches
from supportops.provider_runtime import compatible_runtime
from supportops.qa_cases import canonical_sha256, load_answer_dataset
from supportops.qa_evaluation import evaluate_answers
from supportops.qa_preflight import (
    EndpointConfig, PilotConfig, load_pilot_config, preflight_pilot, select_pilot_dataset,
)
from supportops.seed import PROJECT_ROOT, load_seed_corpus


class PreflightFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_seed_corpus()
        cls.dataset = load_answer_dataset(cls.corpus)
        cls.example = load_pilot_config(Path("configs/supportops/pilot.example.json"), project_root=PROJECT_ROOT)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("network forbidden")))

    def config(self, *, online_embedding=False, **changes):
        data = self.example.model_dump()
        if online_embedding:
            data["embedding"] = {"model": "fixture-embedding", "dimension": 384, "batch_size": 2,
                                 "max_batch_chars": 900}
            data["embedding_endpoint"] = {"provider_id": "fixture", "region": "fixture-region",
                "base_url": "https://embedding.pilot.invalid/compatible-mode/v1",
                "allowed_host": "embedding.pilot.invalid"}
            data["runtime"]["embedding_id"] = "fixture-embedding"
            data["runtime"]["embedding_revision"] = "fixture-v1"
            data["limits"]["max_embedding_requests"] = 200
        data.update(changes)
        return PilotConfig.model_validate(data)

    def report(self, config=None):
        return preflight_pilot(config or self.config(), self.corpus, self.dataset)


class TestEndpointConfiguration(PreflightFixture):
    def endpoint(self, base_url, allowed_host="api.provider.example"):
        return EndpointConfig(provider_id="fixture", region="fixture-region", base_url=base_url,
                              allowed_host=allowed_host)

    def test_exact_https_host_and_compatible_path(self):
        for path in ("/v1", "/compatible-mode/v1", "/gateway_test/v1"):
            self.assertEqual(self.endpoint("https://api.provider.example" + path).base_url,
                             "https://api.provider.example" + path)

    def test_credentials_query_fragment_port_and_escapes_rejected(self):
        for url in ("http://api.provider.example/v1", "https://secret@api.provider.example/v1",
                    "https://api.provider.example:443/v1", "https://api.provider.example/v1?key=PRIVATE",
                    "https://api.provider.example/v1#PRIVATE", "https://api.provider.example/v1?",
                    "https://api.provider.example/v1#", "https://api.provider.example/%2ev1",
                    "https://api.provider.example/../v1", "https://api.provider.example//v1",
                    "https://api.provider.example/v1/", "https://api.provider.example/v1\n/evil",
                    "https://api.provider.example/v\n1", "https://api.provider.example/v\t1",
                    "https://api.provi\rder.example/v1", "HTTPS://api.provider.example/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.endpoint(url)

    def test_ip_local_wildcard_unicode_and_noncanonical_hosts_rejected(self):
        for host in ("127.0.0.1", "[::1]", "localhost", "api.localhost", "api.local", "api.internal",
                     "api.lan", "api.home", "api.arpa", "*.example.com", "API.example.com",
                     "api.example.com.", "中文.example", "-api.example.com", "api..example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.endpoint("https://" + host + "/v1", host)

    def test_hostname_suffix_matching_is_not_allowed(self):
        for host in ("api.provider.example.evil.example", "another.provider.example"):
            with self.assertRaises(ValueError):
                self.endpoint("https://" + host + "/v1")

    def test_unknown_fields_and_embedded_secret_are_rejected(self):
        data = self.example.model_dump()
        for section, key in ((None, "api_key"), (None, "allow_external_calls"),
                             ("chat_endpoint", "headers"), ("chat_endpoint", "api_key_env"),
                             ("chat_endpoint", "proxy"), ("chat", "default_headers")):
            candidate = deepcopy(data)
            (candidate[section] if section else candidate)[key] = "PRIVATE_CONFIG_VALUE"
            with self.subTest(key=key), self.assertRaises(ValueError):
                PilotConfig.model_validate(candidate)

    def test_models_and_execution_must_match(self):
        for change in ({"execution": "offline_local"}, {"model_id": "other-model"},
                       {"embedding_id": "other-embedding"}, {"embedding_revision": "other-revision"}):
            data = self.example.model_dump()
            data["runtime"].update(change)
            with self.assertRaises(ValueError):
                PilotConfig.model_validate(data)

    def test_online_embedding_requires_endpoint_and_matching_model(self):
        for key in ("embedding_endpoint", "embedding"):
            data = self.config(online_embedding=True).model_dump()
            data[key] = None
            with self.assertRaises(ValueError):
                PilotConfig.model_validate(data)
        data = self.config(online_embedding=True).model_dump()
        data["embedding"]["model"] = "wrong-model"
        with self.assertRaises(ValueError):
            PilotConfig.model_validate(data)

    def test_pilot_requires_one_to_three_unique_explicit_cases_and_small_call_cap(self):
        for ids in ([], ["RET-01"] * 2, ["RET-01", "RET-02", "RET-03", "RET-04"]):
            data = self.example.model_dump()
            data["scope"]["case_ids"] = ids
            with self.assertRaises(ValueError):
                PilotConfig.model_validate(data)
        data = self.example.model_dump()
        data["limits"]["max_chat_requests"] = 32
        with self.assertRaises(ValueError):
            PilotConfig.model_validate(data)

    def test_strict_types_finite_numbers_and_explicit_limits(self):
        for change in ({"limits": None}, {"runtime": {**self.example.runtime.model_dump(), "output_tokens": True}},
                       {"chat": {**self.example.chat.model_dump(), "json_mode": "yes"}},
                       {"chat": {**self.example.chat.model_dump(), "timeout_seconds": float("nan")}}):
            with self.assertRaises(ValueError):
                self.config(**change)


class TestPilotBindings(PreflightFixture):
    def test_selection_preserves_explicit_order_and_is_deeply_isolated(self):
        selected = select_pilot_dataset(self.config(), self.corpus, self.dataset)
        self.assertEqual([row.id for row in selected.cases], ["RET-02", "REF-01", "INJ-01"])
        selected.cases[0].expected_fragments.append("only-a-local-test")
        self.assertNotIn("only-a-local-test", self.dataset.cases[1].expected_fragments)

    def test_all_dataset_labels_are_bound_not_just_selected_cases(self):
        modified = self.dataset.model_copy(deep=True)
        # 非试跑题目的 gold 改动也必须导致全题集摘要不匹配。
        modified.cases[0].expected_fragments.pop()
        with self.assertRaises(ValueError):
            select_pilot_dataset(self.config(), self.corpus, modified)

    def test_changed_corpus_content_cannot_reuse_old_digest(self):
        doc = self.corpus.documents[0].model_copy(update={"content": self.corpus.documents[0].content + "\nnew"})
        corpus = replace(self.corpus, documents=(doc, *self.corpus.documents[1:]))
        with self.assertRaises(ValueError):
            preflight_pilot(self.config(), corpus, self.dataset)

    def test_wrong_pinned_hash_or_absent_case_is_rejected(self):
        for changes in ({"corpus_sha256": "0" * 64}, {"dataset_sha256": "0" * 64}, {"case_ids": ["RET-99"]}):
            scope = {**self.example.scope.model_dump(), **changes}
            with self.assertRaises(ValueError):
                self.report(self.config(scope=scope))

    def test_copied_or_mutated_models_cannot_bypass_validation(self):
        modified = self.config().model_copy(deep=True)
        modified.scope.case_ids.extend(["RET-03", "RET-04"])
        with self.assertRaises(ValueError):
            self.report(modified)
        with self.assertRaises(ValueError):
            self.report(self.config().model_copy(update={"runtime": self.example.runtime.model_copy(update={"execution": "offline_fake"})}))

    def test_plan_is_deterministic_and_hash_binds_endpoint_limits_and_settings(self):
        report = self.report()
        self.assertEqual(report, self.report())
        self.assertEqual(report["review_sha256"], canonical_sha256(report["binding"]))
        for changes in ({"chat_endpoint": {**self.example.chat_endpoint.model_dump(), "region": "different-region"}},
                        {"limits": {**self.example.limits.model_dump(), "max_input_chars": 700000}},
                        {"chat": {**self.example.chat.model_dump(), "json_mode": True}}):
            self.assertNotEqual(report["review_sha256"], self.report(self.config(**changes))["review_sha256"])

    def test_report_never_contains_questions_gold_raw_content_or_pollution(self):
        text = json.dumps(self.report(), ensure_ascii=False)
        for row in self.dataset.cases:
            self.assertNotIn(row.question, text)
            for fragment in row.expected_fragments:
                if len(fragment) > 35:
                    self.assertNotIn(fragment, text)
            for marker in row.forbidden_output:
                self.assertNotIn(marker, text)
        self.assertNotIn(self.corpus.documents[0].content, text)
        self.assertNotIn(str(PROJECT_ROOT), text)


class TestRequestPreflight(PreflightFixture):
    def test_hashing_has_no_external_embedding_requests_and_no_invented_usage(self):
        report = self.report()
        plan = report["request_plan"]
        self.assertTrue(report["offline_checks_passed"])
        self.assertEqual(plan["embedding_requests_if_all_stages_reached"], 0)
        self.assertEqual(plan["embedding_input_chars_if_all_stages_reached"], 0)
        self.assertEqual(plan["chat_request_upper_bound"], 3)
        self.assertEqual(plan["combined_input_chars_safety_ceiling"], 600000)
        self.assertEqual(plan["reserved_output_token_upper_bound"], 3072)
        self.assertIsNone(plan["actual_usage"])
        self.assertIsNone(plan["cost"])
        self.assertFalse(plan["monetary_cap_enforced"])

    def test_clean_cases_share_corpus_but_injection_rebuilds_entire_corpus(self):
        report = self.report()
        self.assertEqual(len(report["corpus_variants"]), 2)
        self.assertEqual(len(report["corpus_variants"][0]["documents"]), len(self.corpus.documents))
        self.assertEqual(len(report["corpus_variants"][1]["documents"]), len(self.corpus.documents))
        rows = report["selected_cases"]
        self.assertEqual(rows[0]["effective_corpus_sha256"], rows[1]["effective_corpus_sha256"])
        self.assertNotEqual(rows[0]["effective_corpus_sha256"], rows[2]["effective_corpus_sha256"])
        self.assertEqual(load_seed_corpus().sha256, self.corpus.sha256)

    def test_online_embedding_counts_match_actual_adapter_fake_transport(self):
        # 复用 P5b 的纯内存 SDK 夹具，走实际适配器/评测链路比较计数，而非自证公式。
        from test_supportops_provider_adapters import FakeSDK
        config = self.config(online_embedding=True)
        preflight = self.report(config)
        self.assertTrue(preflight["offline_checks_passed"])
        chat_sdk, embedding_sdk = FakeSDK(), FakeSDK()
        runtime = compatible_runtime(config.runtime, config.chat, chat_sdk_factory=lambda: chat_sdk,
            embedding=config.embedding, embedding_sdk_factory=lambda: embedding_sdk, limits=config.limits)
        result = evaluate_answers(self.corpus, select_pilot_dataset(config, self.corpus, self.dataset),
                                  runtime=runtime, allow_external_calls=True)
        plan = preflight["request_plan"]
        self.assertEqual(plan["embedding_requests_if_all_stages_reached"], len(embedding_sdk.embedding_calls))
        self.assertEqual(plan["embedding_input_chars_if_all_stages_reached"],
                         sum(len(text) for call in embedding_sdk.embedding_calls for text in call["input"]))
        self.assertEqual(len(chat_sdk.chat_calls), plan["chat_request_upper_bound"])
        self.assertLessEqual(result["provider_calls"]["input_chars_reserved"], plan["combined_input_chars_safety_ceiling"])
        # 首次整库构建最后一项失败，后续干净样本会再次构建；成功路径计划不是故障硬上界。
        class FailOnceSDK(FakeSDK):
            def _embedding(inner, **kwargs):
                response = super()._embedding(**kwargs)
                if len(inner.embedding_calls) == 10:
                    raise RuntimeError("fixture build failure")
                return response

        failing_sdk = FailOnceSDK()
        failure_runtime = compatible_runtime(config.runtime, config.chat, chat_sdk_factory=FakeSDK,
            embedding=config.embedding, embedding_sdk_factory=lambda: failing_sdk, limits=config.limits)
        failure = evaluate_answers(self.corpus, select_pilot_dataset(config, self.corpus, self.dataset),
                                  runtime=failure_runtime, allow_external_calls=True)
        self.assertEqual(failure["cases"][0]["error_code"], "INDEX_UNAVAILABLE")
        self.assertGreater(len(failing_sdk.embedding_calls), plan["embedding_requests_if_all_stages_reached"])
        self.assertFalse(plan["failure_rebuild_attempts_included"])

    def test_per_document_embedding_batches_do_not_merge_across_documents(self):
        config = self.config(online_embedding=True, scope={**self.example.scope.model_dump(), "case_ids": ["RET-02"]})
        embedding = {**config.embedding.model_dump(), "batch_size": 128, "max_batch_chars": 256000}
        report = self.report(self.config(online_embedding=True, scope=config.scope, embedding=embedding))
        self.assertEqual(report["request_plan"]["embedding_requests_if_all_stages_reached"], len(self.corpus.documents) + 1)

    def test_limits_are_checked_before_any_potential_runtime(self):
        for key, value, check in (("max_chat_requests", 1, "chat_request_limit"),
                                 ("max_input_chars", 10, "combined_input_char_limit"),
                                 ("max_reserved_output_tokens", 1, "reserved_output_token_limit")):
            config = self.config(limits={**self.example.limits.model_dump(), key: value})
            report = self.report(config)
            self.assertEqual(report["status"], "preflight_rejected")
            self.assertFalse(report["checks"][check])

    def test_embedding_request_budget_covers_documents_and_queries(self):
        config = self.config(online_embedding=True, limits={**self.example.limits.model_dump(), "max_embedding_requests": 1})
        report = self.report(config)
        self.assertFalse(report["checks"]["embedding_request_limit"])
        self.assertFalse(report["offline_checks_passed"])

    def test_unsupported_embedding_text_length_reports_unknown_plan_not_zero(self):
        config = self.config(online_embedding=True)
        config = self.config(online_embedding=True, embedding={**config.embedding.model_dump(), "max_text_chars": 1})
        report = self.report(config)
        self.assertFalse(report["checks"]["embedding_input_lengths"])
        for key in ("embedding_requests_if_all_stages_reached", "embedding_input_chars_if_all_stages_reached",
                    "combined_input_chars_safety_ceiling"):
            self.assertIsNone(report["request_plan"][key])

    def test_shared_batch_planner_strips_counts_splits_and_rejects_all_upfront(self):
        config = EmbeddingSDKConfig(model="fixture", dimension=4, batch_size=2, max_batch_chars=4)
        self.assertEqual(plan_embedding_batches([" a ", "bb", "cc", "d"], config), [["a", "bb"], ["cc", "d"]])
        self.assertEqual(plan_embedding_batches([], config), [])
        for texts in (["ok", "longer"], "not a list", ["ok", ""]):
            with self.assertRaises(SupportOpsError):
                plan_embedding_batches(texts, config)

    def test_offline_preflight_does_not_read_environment_or_construct_runtime(self):
        class NoEnvironment:
            def __getitem__(self, key):
                raise AssertionError("environment access forbidden")
            def get(self, *args):
                raise AssertionError("environment access forbidden")
            def __iter__(self):
                raise AssertionError("environment enumeration forbidden")

        with patch("os.environ", NoEnvironment()), patch("supportops.qa_runtime.EvaluationRuntime.open", side_effect=AssertionError):
            report = self.report()
        self.assertEqual(report["external_model_calls"], 0)
        self.assertFalse(report["credentials_accessed"])
        self.assertFalse(report["execution_authorized"])
        self.assertFalse(report["live_execution_supported"])


class TestPreflightCLI(PreflightFixture):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["preflight-pilot", *args])
        return code, out.getvalue(), err.getvalue()

    def write_config(self, root, data):
        path = Path(root) / "configs" / "supportops" / "pilot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_example_cli_succeeds_without_implying_live_permission(self):
        code, text, err = self.invoke("--config", "configs/supportops/pilot.example.json")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(text)["status"], "awaiting_review")
        self.assertFalse(json.loads(text)["execution_authorized"])

    def test_budget_failure_is_exit_one_and_writes_report_only_under_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.example.model_dump()
            data["limits"]["max_input_chars"] = 1
            path = self.write_config(root, data)
            with patch("supportops.__main__.__file__", str(root / "supportops" / "__main__.py")):
                code, text, err = self.invoke("--config", str(path), "--output", str(root / "outputs" / "preflight.json"))
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            self.assertEqual(json.loads((root / "outputs" / "preflight.json").read_text(encoding="utf-8")), json.loads(text))

    def test_output_escape_is_rejected_before_config_or_corpus_loading(self):
        with patch("supportops.qa_preflight.load_pilot_config", side_effect=AssertionError("must not read")):
            code, text, err = self.invoke("--config", "configs/supportops/pilot.example.json", "--output", "README.md")
        self.assertEqual(code, 2)
        self.assertEqual(text, "")
        self.assertEqual(json.loads(err)["code"], "PREFLIGHT_INVALID")

    def test_secret_config_values_and_parser_details_never_enter_cli_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(root, {**self.example.model_dump(), "api_key": "PRIVATE_CONFIG_VALUE"})
            with patch("supportops.__main__.__file__", str(root / "supportops" / "__main__.py")):
                code, text, err = self.invoke("--config", str(path))
            self.assertEqual(code, 2)
            self.assertEqual(text, "")
            self.assertNotIn("PRIVATE_CONFIG_VALUE", err)
            self.assertNotIn(str(root), err)
            self.assertEqual(json.loads(err)["code"], "PREFLIGHT_INVALID")

    def test_env_or_outside_file_is_rejected_without_opening_it(self):
        with patch("pathlib.Path.open", side_effect=AssertionError("file must not open")):
            for value in (".env", "outputs/secret.json", "configs/supportops/../../../.env"):
                with self.assertRaises(ValueError):
                    load_pilot_config(Path(value), project_root=PROJECT_ROOT)

    def test_invalid_duplicate_nonfinite_oversized_or_nonobject_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.write_config(root, {})
            for raw in ('{"schema_version":1,"schema_version":1}', '{"x": NaN}', '[]', 'x' * 65537):
                path.write_text(raw, encoding="utf-8")
                with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                    load_pilot_config(path, project_root=root)

    def test_no_credentials_or_external_call_flags_are_exposed(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["preflight-pilot", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertNotIn("--api-key", out.getvalue())
        self.assertNotIn("--allow-external", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["preflight-pilot", "--config", "configs/supportops/pilot.example.json",
                  "--api-key", "PRIVATE_ARG_VALUE"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("PRIVATE_ARG_VALUE", err.getvalue())


if __name__ == "__main__":
    unittest.main()
