import unittest
from pathlib import Path

from run_context_benchmark import ContextBenchmarkCase, evaluate_cases, load_cases


class TestContextBenchmark(unittest.TestCase):
    def test_committed_quality_cases_pass_and_are_repeatable(self):
        cases = load_cases(Path(__file__).parent / "benchmarks" / "context_cases.json")
        report = evaluate_cases(cases)
        self.assertEqual(report, evaluate_cases(cases))
        self.assertEqual(report["case_count"], 6)
        self.assertEqual(report["pass_rate"], 1.0, report)

    def test_missing_fact_fails_even_when_query_coverage_is_perfect(self):
        case = ContextBenchmarkCase(
            name="missing-fact", query="Qdrant", expected_fragments=["6333"],
            packets=[{"source": "docs", "content": "Qdrant 服务。"}],
        )
        result = evaluate_cases([case])["cases"][0]
        self.assertEqual(result["quality"]["query_coverage"], 1.0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_fragments"], ["6333"])

    def test_source_name_does_not_count_as_retained_fact(self):
        case = ContextBenchmarkCase(
            name="source-only", query="Qdrant", expected_fragments=["6333"],
            packets=[{"source": "docs-6333", "content": "Qdrant 服务。"}],
        )
        self.assertFalse(evaluate_cases([case])["cases"][0]["passed"])

    def test_empty_benchmark_cannot_claim_success(self):
        with self.assertRaises(ValueError):
            evaluate_cases([])


if __name__ == "__main__":
    unittest.main()
