"""Offline completeness contracts. The real holdout is never model-evaluated."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from hello_agents import (Document, DocumentKnowledgeBase, EpisodicMemory, HashingEmbedder,
                          InMemoryVectorStore, MemoryItem, SQLiteMemoryStore)
from hello_agents.core.exceptions import RAGError

from supportops.__main__ import main
from supportops.answering import (AnswerEngine, COMPLETENESS_PROMPT_PROFILE, COMPLETENESS_RULES,
                                  DEFAULT_PROMPT_PROFILE, SYSTEM_RULES, system_rules_for)
from supportops.document_index import LocalDocumentIndex
from supportops.errors import SupportOpsError
from supportops.qa_cases import canonical_sha256, load_answer_dataset
from supportops.qa_evaluation import evaluate_answers
from supportops.qa_quality import (FROZEN_MANIFEST_SHA, ORIGINAL_CORPUS_SHA, ORIGINAL_DATASET_SHA,
    QUALITY_ROOT, _read_json, evaluate_quality_suite, inspect_frozen_holdout, load_development, quality_plan)
from supportops.qa_runtime import Completion, EvaluationRuntime, fake_runtime
from supportops.seed import load_seed_corpus


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("network forbidden")))

    def assert_code(self, code, fn):
        with self.assertRaises(SupportOpsError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def runtime(self, client):
        spec = fake_runtime().spec.model_copy(update={"runtime_id":"completeness-scripted-fixture",
            "model_id":"scripted-fixture", "execution":"offline_local"})
        return EvaluationRuntime(spec, LocalDocumentIndex, lambda: client)

    def client(self, quote_transform=None):
        class Scripted:
            def __init__(inner):
                inner.messages = []
            def complete(inner, messages, **kwargs):
                inner.messages.append(deepcopy(messages))
                # Contract fixture, not a semantic model or quality measurement.
                desired = "kb-ingestion" if "文档 ID" in messages[-1]["content"] else "kb-memory"
                entries = [json.loads(line) for message in messages if message["role"] == "system"
                           for line in message["content"].splitlines() if line.startswith('{"label":')]
                entry = next(row for row in entries if row["document_id"] == desired)
                quote = entry["quote"].split("\n\n")[2]
                if quote_transform:
                    quote = quote_transform(quote)
                return Completion(json.dumps({"status":"answered","passages":[{"label":entry["label"],"quote":quote}]},ensure_ascii=False))
        return Scripted()

    def test_legacy_prompt_and_dataset_digests_are_unchanged(self):
        corpus = load_seed_corpus()
        self.assertEqual(corpus.sha256, ORIGINAL_CORPUS_SHA)
        self.assertEqual(canonical_sha256(load_answer_dataset(corpus).model_dump(mode="json")), ORIGINAL_DATASET_SHA)
        self.assertEqual(hashlib.sha256(SYSTEM_RULES.encode()).hexdigest(),
                         "68791634cc1e4810d70851fb2db682a720d20c656557b4b32890e4c8084648b2")
        self.assertEqual(AnswerEngine(Mock()).prompt_profile, DEFAULT_PROMPT_PROFILE)

    def test_new_profile_has_no_case_specific_answers_or_ids(self):
        self.assertTrue(COMPLETENESS_RULES.startswith(SYSTEM_RULES))
        for text in ["replace=True", "unchanged", "RET-08", "RET-19", "内存向量库需要重建"]:
            self.assertNotIn(text, COMPLETENESS_RULES)
        self.assertIn("必要前提", COMPLETENESS_RULES)
        self.assertIn("不跨原文不连续位置拼接", COMPLETENESS_RULES)

    def test_unknown_prompt_rejected_before_factories(self):
        corpus, dataset = load_development()
        factory = Mock(side_effect=AssertionError("must not construct"))
        runtime = EvaluationRuntime(fake_runtime().spec, factory, factory)
        self.assert_code("INVALID_REQUEST", lambda: evaluate_answers(corpus, dataset,
            runtime=runtime, prompt_profile="untrusted-profile"))
        factory.assert_not_called()

    def test_separate_development_questions_explicitly_ask_boundaries(self):
        _, dataset = load_development()
        self.assertEqual([case.id for case in dataset.cases], ["RET-81", "RET-82"])
        self.assertIn("正文相同", dataset.cases[0].question)
        self.assertIn("内存向量库", dataset.cases[1].question)

    def test_full_quotes_pass_contract_without_extra_calls_or_gold_leak(self):
        client = self.client()
        report = evaluate_quality_suite(runtime=self.runtime(client))
        self.assertEqual(report["summary"]["passed_case_count"], 2)
        self.assertEqual(len(client.messages), 2)
        self.assertEqual(report["prompt_profile"], COMPLETENESS_PROMPT_PROFILE)
        self.assertFalse(report["semantic_improvement_verified"])
        for messages in client.messages:
            self.assertEqual(messages[0]["content"], COMPLETENESS_RULES)
            rendered = json.dumps(messages)
            for field in ["expected_fragments", "expected_document_ids", "required_fragment_count"]:
                self.assertNotIn(field, rendered)

    def test_short_quotes_remain_incomplete_not_silently_padded(self):
        client = self.client(lambda quote: quote.split("。", 1)[0] + "。")
        report = evaluate_quality_suite(runtime=self.runtime(client))
        self.assertLess(report["summary"]["passed_case_count"], 2)
        self.assertTrue(any(row["missing_fragments"] for row in report["cases"]))
        self.assertEqual(report["summary"]["error_case_count"], 0)

    def test_hallucinated_quote_is_still_rejected(self):
        client = self.client(lambda quote: quote + "这句话不存在于原文。")
        report = evaluate_quality_suite(runtime=self.runtime(client))
        self.assertEqual(report["summary"]["error_case_count"], 2)
        self.assertTrue(all(row["error_code"] == "MODEL_RESPONSE_INVALID" for row in report["cases"]))

    def test_original_fake_score_stays_twenty_of_thirty_two(self):
        corpus = load_seed_corpus()
        report = evaluate_answers(corpus, load_answer_dataset(corpus))
        self.assertEqual(report["summary"]["passed_case_count"], 20)
        self.assertEqual(report["prompt_profile"], DEFAULT_PROMPT_PROFILE)

    def test_review_retains_strict_failures_and_only_supported_quotes(self):
        review = _read_json(QUALITY_ROOT / "failure_review.json")
        corpus = {doc.id:doc.content for doc in load_seed_corpus().documents}
        original = {case.id:case for case in load_answer_dataset(load_seed_corpus()).cases}
        self.assertEqual(review["original_real_result"]["passed"], 30)
        for row in review["cases"]:
            self.assertFalse(row["strict_baseline_passed"])
            self.assertIn(row["observed_quote"], corpus[row["source_document_id"]])
            self.assertTrue(all(part in original[row["id"]].expected_fragments for part in row["missing_fragments"]))
            self.assertTrue(all(part not in row["observed_quote"] for part in row["missing_fragments"]))

    def test_ret08_boundary_matches_actual_ingest_behavior(self):
        store = SQLiteMemoryStore(":memory:")
        self.addCleanup(store.close)
        embedder = HashingEmbedder()
        kb = DocumentKnowledgeBase(document_store=store,
            vector_store=InMemoryVectorStore(embedder.dimension), embedder=embedder)
        document = Document(id="quality-audit", content="公开测试内容", source="quality-fixture", title="摄取复核")
        kb.ingest(document)
        with self.assertRaises(RAGError):
            kb.ingest(document)
        self.assertTrue(kb.ingest(document, replace=True).unchanged)

    def test_ret19_body_survives_but_new_memory_index_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            embedder = HashingEmbedder()
            store = SQLiteMemoryStore(path)
            first = EpisodicMemory(document_store=store, vector_store=InMemoryVectorStore(embedder.dimension), embedder=embedder)
            item = MemoryItem(content="公开测试事件", memory_type="episodic")
            first.add(item)
            store.close()
            restarted = SQLiteMemoryStore(path)
            try:
                fresh_vectors = InMemoryVectorStore(embedder.dimension)
                second = EpisodicMemory(document_store=restarted, vector_store=fresh_vectors, embedder=embedder)
                self.assertEqual(restarted.get(item.id).content, item.content)
                self.assertEqual(fresh_vectors.count(), 0)
                self.assertEqual(second.retrieve("公开测试事件", limit=3), [])
            finally:
                restarted.close()

    def test_holdout_is_new_synthetic_corpus_and_frozen_not_blind_claim(self):
        corpus, dataset, record = inspect_frozen_holdout()
        self.assertEqual(len(dataset.cases), 12)
        self.assertEqual(len(corpus.documents), 3)
        self.assertEqual([sum(case.category==kind for case in dataset.cases)
                          for kind in ("answerable","refusal","injection")], [6,3,3])
        self.assertEqual(corpus.manifest.origin, "synthetic_support_fixture")
        self.assertTrue(all(doc.source.startswith("data/supportops/quality/holdout/corpus/") for doc in corpus.documents))
        self.assertTrue(set(doc.id for doc in corpus.documents).isdisjoint(doc.id for doc in load_seed_corpus().documents))
        self.assertFalse(record["independent_human_blind_test"])
        self.assertEqual(canonical_sha256(record), FROZEN_MANIFEST_SHA)

    def test_holdout_disabled_before_loading_or_factory(self):
        with patch("supportops.qa_quality.inspect_frozen_holdout", side_effect=AssertionError("sealed")):
            self.assert_code("HOLDOUT_OPEN_REQUIRED", lambda: evaluate_quality_suite("holdout"))

    def test_external_runtime_rejected_without_opening_holdout(self):
        factory = Mock(side_effect=AssertionError("no SDK"))
        spec = fake_runtime().spec.model_copy(update={"execution":"external"})
        runtime = EvaluationRuntime(spec, factory, factory)
        with patch("supportops.qa_quality.inspect_frozen_holdout", side_effect=AssertionError("sealed")):
            self.assert_code("EXTERNAL_CALLS_DISABLED", lambda: evaluate_quality_suite("holdout",open_holdout=True,runtime=runtime))
        factory.assert_not_called()

    def test_frozen_files_and_manifest_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "quality"
            shutil.copytree(QUALITY_ROOT, root)
            target = root / "holdout/corpus/release.md"
            target.write_text(target.read_text(encoding="utf-8") + "\nChanged", encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_frozen_holdout(quality_root=root)
            freeze = root / "holdout.freeze.json"
            content = _read_json(freeze)
            content["files"]["holdout/corpus/release.md"] = hashlib.sha256(target.read_bytes()).hexdigest()
            freeze.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_frozen_holdout(quality_root=root)

    def test_opening_failure_retains_receipt_no_retry(self):
        # Simulated opening only; no holdout answer generation or scores are run.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluate = Mock(side_effect=SupportOpsError("MODEL_UNAVAILABLE"))
            with patch("supportops.qa_quality.evaluate_answers", evaluate):
                self.assert_code("MODEL_UNAVAILABLE", lambda: evaluate_quality_suite("holdout",open_holdout=True,project_root=root))
                self.assert_code("HOLDOUT_ALREADY_OPENED", lambda: evaluate_quality_suite("holdout",open_holdout=True,project_root=root))
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(len(list((root / "outputs/supportops").glob("*.opened.json"))), 1)

    def test_plan_does_not_evaluate_or_open_actual_holdout(self):
        with patch("supportops.qa_quality.evaluate_answers", side_effect=AssertionError("no generation")):
            plan = quality_plan()
        self.assertEqual(plan["holdout_case_count"], 12)
        self.assertFalse(plan["holdout_evaluated_by_this_command"])
        self.assertFalse(plan["semantic_improvement_verified"])

    def test_quality_json_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"case_count":1,"case_count":2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_json(path)
        with patch.object(Path, "is_symlink", return_value=True), \
             patch("supportops.qa_quality.read_small_text", side_effect=AssertionError("must not follow link")):
            with self.assertRaises(ValueError):
                _read_json(Path("linked-artifact.json"))

    def test_cli_holdout_requires_explicit_open_and_output_path_precheck(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["evaluate-answer-quality", "--suite", "holdout"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue())["code"], "HOLDOUT_OPEN_REQUIRED")
        with patch("supportops.qa_quality.evaluate_quality_suite", side_effect=AssertionError("output first")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["evaluate-answer-quality", "--output", "README.md"]), 2)


if __name__ == "__main__":
    unittest.main()
