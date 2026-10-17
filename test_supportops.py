"""SupportOps P0：真实本地检索与显式验收边界，不调用模型或外部服务。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hello_agents import DocumentRAGResult, RAGError, SQLiteMemoryStore
from supportops import load_seed_corpus, open_seed_index
from supportops.__main__ import main
from supportops.evaluation import evaluate_retrieval, load_acceptance_dataset
from supportops.seed import PROJECT_ROOT, SeedDocumentSpec


class TestSupportOpsCorpus(unittest.TestCase):
    def test_manifest_has_ten_traceable_documents_and_stable_digest(self):
        corpus = load_seed_corpus()
        self.assertEqual(len(corpus.documents), 10)
        self.assertEqual(corpus.sha256, load_seed_corpus().sha256)
        self.assertEqual(len(corpus.sha256), 64)
        for spec, document in zip(corpus.manifest.documents, corpus.documents, strict=True):
            self.assertEqual(document.id, spec.id)
            self.assertEqual(document.source, f"data/supportops/corpus/{spec.path}")
            self.assertFalse(Path(document.source).is_absolute())
            for reference in spec.source_refs:
                self.assertTrue((PROJECT_ROOT / reference).is_file(), reference)

    def test_rejects_traversal_absolute_sensitive_and_noncanonical_paths(self):
        valid = load_seed_corpus().manifest.documents[0].model_dump()
        for path in ("../private.md", "C:/private.md", "/private.md", ".env",
                     "folder/.private.md", "folder\\file.md", "./file.md", "file.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                SeedDocumentSpec.model_validate({**valid, "path": path})
        for reference in (".env", "secret.env", "../README.md", "C:/README.md"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                SeedDocumentSpec.model_validate({**valid, "source_refs": [reference]})

    def make_corpus(self, root):
        manifest = load_seed_corpus().manifest.model_dump(mode="json")
        manifest["documents"] = [manifest["documents"][0]]
        (root / "overview.md").write_text("# 测试指南\n\n离线资料。", encoding="utf-8")
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_rejects_duplicate_manifest_ids_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_corpus(root)
            entry = manifest["documents"][0]
            for duplicate in ({**entry, "path": "other.md"}, {**entry, "id": "kb-other"}):
                manifest["documents"] = [entry, duplicate]
                (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                    load_seed_corpus(root)

    def test_only_manifest_is_ingested_and_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_corpus(root)
            # 非 UTF-8、未列入清单的文件不应被读取，包括旁边的验收资料。
            (root / "unlisted.md").write_bytes(b"\xff\xfe")
            before = load_seed_corpus(root)
            self.assertEqual(len(before.documents), 1)
            (root / "overview.md").write_text("# 测试指南\n\n更新后的离线资料。", encoding="utf-8")
            self.assertNotEqual(before.sha256, load_seed_corpus(root).sha256)

    def test_index_closes_store_when_ingestion_fails(self):
        store = SQLiteMemoryStore(":memory:")
        with patch("supportops.seed.SQLiteMemoryStore", return_value=store), \
             patch.object(store, "close", wraps=store.close) as close, \
             patch("supportops.seed.DocumentKnowledgeBase.ingest", side_effect=RAGError("fake")):
            with self.assertRaises(RAGError):
                with open_seed_index(load_seed_corpus()):
                    self.fail("不应返回部分摄取的索引")
            close.assert_called_once()


class TestSupportOpsEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_seed_corpus()
        cls.dataset = load_acceptance_dataset(cls.corpus)

    def validate_modified_dataset(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return load_acceptance_dataset(self.corpus, path)

    def test_dataset_has_separate_retrieval_and_pending_application_cases(self):
        self.assertEqual(len(self.dataset.retrieval_cases), 25)
        self.assertEqual(len(self.dataset.application_cases), 5)
        self.assertEqual(self.dataset.split, "development_smoke")
        self.assertTrue(all(case.status == "pending_application"
                            for case in self.dataset.application_cases))

    def test_rejects_unknown_gold_document(self):
        data = self.dataset.model_dump(mode="json")
        data["retrieval_cases"][0]["expected_document_ids"] = ["not-ingested"]
        with self.assertRaises(ValueError):
            self.validate_modified_dataset(data)

    def test_rejects_missing_or_blank_gold_fragment(self):
        for fragment in ("未被知识来源支持的测试断言", "   "):
            data = self.dataset.model_dump(mode="json")
            data["retrieval_cases"][0]["expected_fragments"] = [fragment]
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                self.validate_modified_dataset(data)

    def test_rejects_duplicate_case_ids(self):
        data = self.dataset.model_dump(mode="json")
        data["retrieval_cases"][1]["id"] = data["retrieval_cases"][0]["id"]
        with self.assertRaises(ValueError):
            self.validate_modified_dataset(data)

    def test_real_offline_pipeline_is_repeatable_and_application_not_counted(self):
        with open_seed_index(self.corpus) as index:
            first = evaluate_retrieval(index, self.corpus, self.dataset)
            self.assertEqual(first, evaluate_retrieval(index, self.corpus, self.dataset))
        self.assertEqual(first["retrieval_case_count"], 25)
        self.assertTrue(first["retrieval_gate_passed"])
        self.assertEqual(first["citation_span_accuracy"], 1.0)
        self.assertFalse(first["application_ready"])
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(len(first["application_cases"]), 5)

    def test_empty_retrieval_cannot_vacuously_pass_citations_or_gate(self):
        with open_seed_index(self.corpus) as index:
            with patch.object(index, "query", side_effect=lambda query, **_: DocumentRAGResult(query=query)):
                report = evaluate_retrieval(index, self.corpus, self.dataset)
        self.assertFalse(report["retrieval_gate_passed"])
        self.assertEqual(report["passed_case_count"], 0)
        self.assertEqual(report["citation_span_accuracy"], 0.0)
        self.assertTrue(all(not case["citations_valid"] for case in report["cases"]))

    def test_missing_extra_or_wrong_citations_fail_validation(self):
        dataset = self.dataset.model_copy(update={"retrieval_cases": self.dataset.retrieval_cases[:1]})
        with open_seed_index(self.corpus) as index:
            result = index.query(dataset.retrieval_cases[0].question, limit=5)
            wrong = result.citations[0].model_copy(update={"title": "错误标题"})
            variants = [result.citations[:-1], result.citations + [result.citations[0]],
                        [wrong, *result.citations[1:]]]
            for citations in variants:
                broken = result.model_copy(update={"citations": citations})
                with self.subTest(citations=len(citations)), patch.object(index, "query", return_value=broken):
                    report = evaluate_retrieval(index, self.corpus, dataset)
                self.assertFalse(report["retrieval_gate_passed"])
                self.assertFalse(report["cases"][0]["passed"])
                self.assertLess(report["citation_span_accuracy"], 1.0)

    def test_rejects_invalid_top_k_before_search(self):
        for top_k in (True, 0, 101, 1.5, "5"):
            with self.subTest(top_k=top_k), self.assertRaises(ValueError):
                evaluate_retrieval(None, self.corpus, self.dataset, top_k=top_k)


class TestSupportOpsCLI(unittest.TestCase):
    def test_search_returns_evidence_json_without_invoking_llm(self):
        output = io.StringIO()
        with patch("hello_agents.HelloAgentsLLM.invoke", side_effect=AssertionError("禁止模型调用")), \
             redirect_stdout(output):
            self.assertEqual(main(["search", "扫描版 PDF 为什么无法检索？"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "offline_retrieval_only")
        self.assertEqual(payload["model_calls"], 0)
        self.assertEqual(payload["result"]["hits"][0]["chunk"]["document_id"], "kb-documents")

    def test_invalid_search_has_sanitized_error_and_nonzero_exit(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["search", "问题", "--top-k", "0"]), 2)
        self.assertNotIn("Traceback", output.getvalue())

    def test_report_cannot_overwrite_corpus_or_arbitrary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "protected.json"
            target.write_text("keep", encoding="utf-8")
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["evaluate", "--output", str(target)]), 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_report_is_saved_as_utf8_json_under_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "outputs" / "baseline.json"
            with patch("supportops.__main__.__file__", str(root / "supportops" / "__main__.py")), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(main(["evaluate", "--output", str(target)]), 0)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["retrieval_case_count"], 25)
            self.assertIn("扫描版", report["cases"][4]["question"])


if __name__ == "__main__":
    unittest.main()
