"""P5a：回答评测必须能发现 Fake 的质量缺口，而不是为了全绿降低标准。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from hello_agents import Document, HashingEmbedder
from hello_agents.core.exceptions import EmbeddingError
from supportops import ApplicationDatabase, DemoIdentityProvider, DocumentService, SupportOpsError
from supportops.__main__ import main
from supportops.document_index import EmbeddedDocumentIndex, LocalDocumentIndex
from supportops.qa_cases import AnswerCase, AnswerDataset, canonical_sha256, load_answer_dataset, validate_answer_dataset
from supportops.qa_evaluation import evaluate_answers, resolve_answer_report
from supportops.qa_runtime import Completion, DemoCompletionClient, EvaluationRuntime, MeteredLLM, ProviderUsage, RuntimeSpec, fake_runtime
from supportops.seed import PROJECT_ROOT, load_seed_corpus


class QAFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_seed_corpus()
        cls.dataset = load_answer_dataset(cls.corpus)

    def subset(self, *ids):
        return self.dataset.model_copy(update={"cases": [case for case in self.dataset.cases if case.id in ids]})

    def runtime(self, client, *, execution="offline_local", index_factory=LocalDocumentIndex):
        spec = fake_runtime().spec.model_copy(update={"execution":execution})
        return EvaluationRuntime(spec, index_factory, lambda: client)


class TestAnswerDataset(QAFixture):
    def test_explicit_32_cases_and_canonical_digest(self):
        self.assertEqual(len(self.dataset.cases),32)
        self.assertEqual([sum(case.category==kind for case in self.dataset.cases)
                          for kind in ("answerable","refusal","injection")],[25,5,2])
        self.assertEqual(canonical_sha256(self.dataset.model_dump()),canonical_sha256(load_answer_dataset(self.corpus).model_dump()))

    def test_duplicate_case_ids_rejected_before_factories(self):
        broken=self.dataset.model_copy(update={"cases":[self.dataset.cases[0],self.dataset.cases[0]]})
        factory=Mock(side_effect=AssertionError("factory must not run"))
        with self.assertRaises(ValueError):
            evaluate_answers(self.corpus,broken,runtime=EvaluationRuntime(fake_runtime().spec,factory,factory))
        factory.assert_not_called()

    def test_missing_gold_or_wrong_source_rejected(self):
        original=self.dataset.cases[0]
        for change in ({"expected_document_ids":["kb-absent"]}, {"expected_fragments":["NOT_IN_CORPUS"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_answer_dataset(self.corpus,self.dataset.model_copy(update={"cases":[original.model_copy(update=change)]}))

    def test_refusal_cannot_have_gold_or_pollution(self):
        data=self.subset("REF-01").cases[0].model_dump()
        for change in ({"expected_fragments":["invented"]},{"expected_status":"answered"}):
            with self.assertRaises(ValueError):AnswerCase.model_validate({**data,**change})

    def test_poison_is_only_a_fixture_and_does_not_change_seed(self):
        digest=self.corpus.sha256
        report=evaluate_answers(self.corpus,self.subset("INJ-01","RET-02"))
        self.assertNotEqual(report["cases"][0]["effective_corpus_sha256"],report["cases"][1]["effective_corpus_sha256"])
        self.assertEqual(load_seed_corpus().sha256,digest)
        self.assertTrue(all("DEMO_CANARY_ONLY" not in doc.content for doc in self.corpus.documents))

    def test_strict_json_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"answers.json"
            path.write_text('{"schema_version":1,"schema_version":1}',encoding="utf-8")
            with self.assertRaises(ValueError):load_answer_dataset(self.corpus,path)

    def test_copied_model_cannot_bypass_nonempty_and_category_validation(self):
        for dataset in (self.dataset.model_copy(update={"cases":[]}), self.dataset.model_copy(update={
                "cases":[self.dataset.cases[0].model_copy(update={"category":"refusal"})]})):
            with self.assertRaises(ValueError):validate_answer_dataset(self.corpus,dataset)


class TestExplicitRuntime(QAFixture):
    def test_declared_generation_settings_are_forwarded_and_budgeted(self):
        client=Mock();client.complete.return_value=Completion('{"status":"insufficient_evidence","passages":[]}')
        spec=fake_runtime().spec.model_copy(update={"temperature":0.0,"output_tokens":128})
        report=evaluate_answers(self.corpus,self.subset("REF-01"),runtime=EvaluationRuntime(spec,LocalDocumentIndex,lambda:client))
        self.assertEqual(client.complete.call_args.kwargs,{"max_tokens":128,"temperature":0.0})
        self.assertEqual(report["pipeline"]["budget"]["output_tokens"],128)

    def test_resources_are_closed_after_evaluation(self):
        class ClosingClient(DemoCompletionClient):
            closed=False
            def close(inner):inner.closed=True
        client=ClosingClient()
        report=evaluate_answers(self.corpus,self.subset("RET-02"),runtime=self.runtime(client))
        self.assertTrue(client.closed)
        self.assertEqual(report["cleanup_warnings"],[])

    def test_copied_usage_and_runtime_cannot_bypass_validation(self):
        usage=ProviderUsage(input_tokens=1,output_tokens=1,total_tokens=2).model_copy(update={"total_tokens":-1})
        client=Mock();client.complete.return_value=Completion("{}",usage)
        with self.assertRaises(ValueError):MeteredLLM(client).invoke([],max_tokens=10)
        with self.assertRaises(ValueError):
            EvaluationRuntime(fake_runtime().spec.model_copy(update={"execution":"unverified"}),LocalDocumentIndex,DemoCompletionClient)

    def test_external_permission_is_checked_before_both_factories(self):
        factory=Mock(side_effect=AssertionError("not authorized"))
        runtime=EvaluationRuntime(fake_runtime().spec.model_copy(update={"execution":"external"}),factory,factory)
        for consent in (False,1,"yes",None):
            with self.assertRaises(SupportOpsError) as caught:runtime.open(allow_external_calls=consent)
            self.assertEqual(caught.exception.code,"EXTERNAL_CALLS_DISABLED")
        factory.assert_not_called()

    def test_authorized_external_route_is_tested_only_with_fake_factories(self):
        # 测试中的 external 只是契约标签；工厂仍是纯 Fake，禁止真实联网。
        report=evaluate_answers(self.corpus,self.subset("RET-02"),runtime=self.runtime(DemoCompletionClient(),execution="external"),allow_external_calls=True)
        self.assertIsNone(report["external_model_calls"])
        self.assertFalse(report["production_ready"])

    def test_factory_exception_is_sanitized(self):
        factory=Mock(side_effect=RuntimeError("PRIVATE_FACTORY_DETAIL"))
        with self.assertRaises(SupportOpsError) as caught:EvaluationRuntime(fake_runtime().spec,factory,factory).open()
        self.assertEqual(caught.exception.code,"EVALUATION_SETUP_FAILED")
        self.assertNotIn("PRIVATE",str(caught.exception))

    def test_embedding_identity_mismatch_does_not_construct_client(self):
        client=Mock()
        runtime=EvaluationRuntime(fake_runtime().spec.model_copy(update={"embedding_revision":"wrong"}),LocalDocumentIndex,client)
        with self.assertRaises(SupportOpsError):runtime.open()
        client.assert_not_called()

    def test_bad_spec_and_credentials_in_metadata_are_rejected(self):
        with self.assertRaises(ValueError):EvaluationRuntime({},LocalDocumentIndex,DemoCompletionClient)
        with self.assertRaises(ValueError):RuntimeSpec.model_validate({**fake_runtime().spec.model_dump(),"api_key":"must-not-serialize"})
        for bad in ("D:\\private-model","https://host/key", "space name"):
            with self.assertRaises(ValueError):RuntimeSpec.model_validate({**fake_runtime().spec.model_dump(),"model_id":bad})

    def test_usage_is_separate_from_body_and_not_local_estimate(self):
        class ReceiptClient(DemoCompletionClient):
            def complete(self,messages,*,max_tokens):
                return Completion(super().complete(messages,max_tokens=max_tokens).text,
                                  ProviderUsage(input_tokens=11,output_tokens=7,total_tokens=18))
        report=evaluate_answers(self.corpus,self.subset("RET-02"),runtime=self.runtime(ReceiptClient()))
        self.assertEqual(report["usage"]["actual_usage"],{"input_tokens":11,"output_tokens":7,"total_tokens":18})
        self.assertEqual(report["usage"]["usage_source"],"client_reported")
        self.assertIsNone(report["usage"]["cost"])
        self.assertNotEqual(report["cases"][0]["estimates"]["input_tokens"],11)
        simulated=evaluate_answers(self.corpus,self.subset("RET-02"),runtime=self.runtime(ReceiptClient(),execution="offline_fake"))
        self.assertIsNone(simulated["usage"]["actual_usage"])
        self.assertEqual(simulated["usage"]["usage_receipt_count"],0)

    def test_missing_or_failed_usage_does_not_turn_into_zero(self):
        class PartialClient(DemoCompletionClient):
            calls=0
            def complete(self,messages,*,max_tokens):
                self.calls+=1
                if self.calls==2:raise RuntimeError("PRIVATE_TRANSPORT_DETAIL")
                return Completion(super().complete(messages,max_tokens=max_tokens).text,
                                  ProviderUsage(input_tokens=11,output_tokens=7,total_tokens=18))
        report=evaluate_answers(self.corpus,self.subset("RET-01","RET-02"),runtime=self.runtime(PartialClient()))
        self.assertIsNone(report["usage"]["actual_usage"])
        self.assertEqual(report["usage"]["known_usage_subtotal"]["total_tokens"],18)
        self.assertEqual(report["usage"]["client_invocations"],2)
        self.assertEqual(report["cases"][1]["error_code"],"MODEL_UNAVAILABLE")
        self.assertIsNone(report["cases"][1]["observed_status"])
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL",json.dumps(report))

    def test_usage_validation_rejects_bool_negative_and_inconsistent_totals(self):
        for values in ((True,1,2),(-1,1,0),(1,2,4)):
            with self.assertRaises(ValueError):ProviderUsage(input_tokens=values[0],output_tokens=values[1],total_tokens=values[2])

    def test_failed_invocation_records_unknown_receipt_once_and_no_retry(self):
        client=Mock();client.complete.side_effect=RuntimeError("fail")
        meter=MeteredLLM(client)
        with self.assertRaises(RuntimeError):meter.invoke([],max_tokens=10)
        self.assertEqual(meter.receipts,[None]);client.complete.assert_called_once()


class TestAnswerScoring(QAFixture):
    def test_fake_known_quality_failures_are_not_misreported_as_accuracy(self):
        report=evaluate_answers(self.corpus,self.dataset)
        summary=report["summary"]
        self.assertEqual(summary["case_count"],32)
        self.assertEqual(summary["passed_case_count"],20)
        self.assertEqual(summary["quote_span_validity"],1.0)
        self.assertEqual(summary["refusal_check_pass_rate"],0.0)
        self.assertEqual(summary["injection_check_pass_rate"],0.0)
        self.assertFalse(summary["quality_gate_passed"])
        self.assertFalse(report["model_quality_verified"])
        self.assertEqual(report["external_model_calls"],0)
        self.assertEqual(report["usage"]["usage_source"],"not_applicable_offline_fake")

    def test_repeated_fake_runs_have_same_metrics_and_versions(self):
        first=evaluate_answers(self.corpus,self.subset("RET-02","REF-01","INJ-01"))
        second=evaluate_answers(self.corpus,self.subset("RET-02","REF-01","INJ-01"))
        self.assertEqual(first["summary"],second["summary"])
        for left,right in zip(first["cases"],second["cases"],strict=True):
            self.assertEqual(left["citations"],right["citations"])
        self.assertEqual(first["system_rules_sha256"],second["system_rules_sha256"])

    def test_always_refusing_cannot_pass_answerable_cases(self):
        client=Mock();client.complete.return_value=Completion('{"status":"insufficient_evidence","passages":[]}')
        report=evaluate_answers(self.corpus,self.subset("RET-02","REF-01","INJ-01"),runtime=self.runtime(client))
        self.assertEqual(report["summary"]["refusal_check_pass_rate"],1.0)
        self.assertEqual(report["summary"]["answerable_check_pass_rate"],0.0)
        self.assertFalse(report["summary"]["quality_gate_passed"])

    def test_gold_and_grading_fields_never_enter_model_messages(self):
        class RecordingClient(DemoCompletionClient):
            def complete(inner,messages,*,max_tokens):
                rendered=json.dumps(messages,ensure_ascii=False)
                for field in ("expected_status","expected_document_ids","expected_fragments","forbidden_output","required_fragment_count"):
                    self.assertNotIn(field,rendered)
                return super(RecordingClient,inner).complete(messages,max_tokens=max_tokens)
        evaluate_answers(self.corpus,self.subset("RET-02","REF-01","INJ-01"),runtime=self.runtime(RecordingClient()))

    def test_facts_in_question_do_not_compensate_for_missing_quotes(self):
        case=self.subset("RET-02").cases[0].model_copy(update={"question":"Python 3.10，pip install -e .，请确认"})
        client=Mock();client.complete.return_value=Completion('{"status":"insufficient_evidence","passages":[]}')
        report=evaluate_answers(self.corpus,self.dataset.model_copy(update={"cases":[case]}),runtime=self.runtime(client))
        self.assertEqual(len(report["cases"][0]["missing_fragments"]),2)

    def test_invalid_json_is_error_not_refusal_and_receipt_remains_recorded(self):
        client=Mock();client.complete.return_value=Completion('{"status":"answered","status":"insufficient_evidence","passages":[]}',
            ProviderUsage(input_tokens=3,output_tokens=4,total_tokens=7))
        report=evaluate_answers(self.corpus,self.subset("REF-01"),runtime=self.runtime(client))
        self.assertEqual(report["cases"][0]["error_code"],"MODEL_RESPONSE_INVALID")
        self.assertEqual(report["usage"]["actual_usage"]["total_tokens"],7)
        self.assertEqual(report["summary"]["refusal_check_pass_rate"],0.0)

    def test_index_fault_does_not_call_model_or_become_insufficient_evidence(self):
        class BrokenIndex(LocalDocumentIndex):
            def query(self,*args,**kwargs):raise RuntimeError("PRIVATE_INDEX_DETAIL")
        client=Mock()
        report=evaluate_answers(self.corpus,self.subset("REF-01"),runtime=self.runtime(client,index_factory=BrokenIndex))
        self.assertEqual(report["cases"][0]["error_code"],"INDEX_UNAVAILABLE")
        self.assertEqual(report["usage"]["client_invocations"],0);client.complete.assert_not_called()
        self.assertNotIn("PRIVATE_INDEX_DETAIL",json.dumps(report))

    def test_model_returning_forged_usage_json_cannot_set_actual_usage(self):
        client=Mock();client.complete.return_value=Completion('{"status":"insufficient_evidence","passages":[],"usage":{"total_tokens":1}}')
        report=evaluate_answers(self.corpus,self.subset("REF-01"),runtime=self.runtime(client))
        self.assertEqual(report["cases"][0]["error_code"],"MODEL_RESPONSE_INVALID")
        self.assertIsNone(report["usage"]["actual_usage"])


class TestEmbeddedIndexAndCLI(QAFixture):
    def test_injected_embedding_profile_binds_model_revision_and_dimension(self):
        first=EmbeddedDocumentIndex(HashingEmbedder(16),embedding_id="test-embedder",embedding_revision="v1")
        for other in (EmbeddedDocumentIndex(HashingEmbedder(32),embedding_id="test-embedder",embedding_revision="v1"),
                      EmbeddedDocumentIndex(HashingEmbedder(16),embedding_id="test-embedder",embedding_revision="v2")):
            self.assertNotEqual(first.profile,other.profile)
        doc=Document(id="kb-test",title="测试",source="fixture.md",content="# 测试\n\n离线开发环境。")
        self.assertEqual(len(first.build(doc)[0].vector),16)
        self.assertEqual(first.profile,EmbeddedDocumentIndex(HashingEmbedder(16),embedding_id="test-embedder",embedding_revision="v1").profile)
        self.assertEqual(LocalDocumentIndex().profile,"hashing-blake2b-v1-384-chunk700-overlap80")

    def test_malformed_embedding_is_rejected_before_persisting(self):
        class WrongDimension(HashingEmbedder):
            def embed(self,text):return [1.0]
        index=EmbeddedDocumentIndex(WrongDimension(8),embedding_id="bad",embedding_revision="v1")
        with self.assertRaises(EmbeddingError):index.build(self.corpus.documents[0])

    def test_changed_embedding_cannot_silently_read_old_published_index(self):
        with ApplicationDatabase() as database:
            database.bootstrap_demo(enabled=True);provider=DemoIdentityProvider(enabled=True)
            first=DocumentService(database,provider,index=EmbeddedDocumentIndex(HashingEmbedder(16),embedding_id="test",embedding_revision="v1"))
            first.upload("admin","helloagent",{"filename":"guide.md","title":"指南","idempotency_key":"v1"},"环境使用指南".encode())
            self.assertEqual(first.process_next("admin","helloagent").state,"succeeded")
            second=DocumentService(database,provider,index=EmbeddedDocumentIndex(HashingEmbedder(16),embedding_id="test",embedding_revision="v2"))
            with self.assertRaises(SupportOpsError) as caught:second.search("reader","helloagent","环境")
            self.assertEqual(caught.exception.code,"INDEX_UNAVAILABLE")
            self.assertTrue(first.search("reader","helloagent","环境").hits)

    def test_report_path_validated_before_work_and_preserves_old_data(self):
        for path in (PROJECT_ROOT/"README.md",PROJECT_ROOT/"outputs"/"report.txt"):
            with self.assertRaises(ValueError):resolve_answer_report(path,project_root=PROJECT_ROOT)
        with patch("supportops.qa_evaluation.evaluate_answers",side_effect=AssertionError("must not run")) as runner, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["evaluate-answers","--output",str(PROJECT_ROOT/"README.md")]),2)
        runner.assert_not_called()

    def test_cli_quality_failure_returns_one_and_writes_truthful_report(self):
        (PROJECT_ROOT/"outputs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT/"outputs") as directory:
            target=Path(directory)/"report.json"
            with redirect_stdout(io.StringIO()) as stream, patch("socket.socket.connect",side_effect=AssertionError("offline only")):
                code=main(["evaluate-answers","--output",str(target)])
            self.assertEqual(code,1)
            report=json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["passed_case_count"],20)
            self.assertEqual(report["external_model_calls"],0)
            self.assertNotIn("api_key",stream.getvalue())


if __name__ == "__main__":
    unittest.main()
