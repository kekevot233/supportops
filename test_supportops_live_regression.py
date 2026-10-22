"""Offline cumulative budget/concurrency/isolation tests; no real config/network."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from supportops.errors import SupportOpsError
from supportops.live_pilot import GuardedPilotSDK, PilotCredentials, PilotSpendGuard, _wire_body
from supportops.live_regression import RUN_ID, execute_regression, prepare_regression, run_regression
from supportops.spend_ledger import SpendLedger, micros


def plan(amount="0.1"):
    return [{"reserved_cny": amount, "request_sha256": "a" * 64}]


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "budget.sqlite3"
        self.ledger = SpendLedger(self.path)

    def assert_code(self, code, fn):
        with self.assertRaises(SupportOpsError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_legacy_unknown_and_round2_are_reserved_without_refunds(self):
        state = self.ledger.snapshot()
        self.assertEqual(Decimal(state["encumbered_cny"]), Decimal("1.228808"))
        self.assertEqual(Decimal(state["available_cny"]), Decimal("3.771192"))
        self.assertIsNone(state["provider_invoice_cost_cny"])
        self.assertEqual(SpendLedger(self.path).snapshot(), state)

    def test_fractional_nan_negative_and_excess_amounts_rejected(self):
        for amount in ["NaN", "Infinity", "-1", "0", "5.000001", "0.0000001", "invalid-amount"]:
            self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: micros(amount))

    def test_cumulative_limit_before_reserve(self):
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: self.ledger.reserve("too-large", plan("4")))
        self.assertEqual(len(self.ledger.snapshot()["runs"]), 2)

    def test_reserve_and_claim_cannot_be_reused_after_reopen(self):
        self.ledger.reserve("run-one", plan())
        self.assert_code("PILOT_ALREADY_RESERVED", lambda: SpendLedger(self.path).reserve("run-one", plan()))
        self.ledger.claim("run-one", plan())
        self.assert_code("PILOT_ALREADY_RESERVED", lambda: SpendLedger(self.path).claim("run-one", plan()))

    def test_wrong_plan_rejected_without_consuming_claim(self):
        self.ledger.reserve("run-one", plan())
        self.assert_code("PILOT_ALREADY_RESERVED", lambda: self.ledger.claim("run-one", plan("0.2")))
        self.ledger.claim("run-one", plan())

    def test_concurrent_reservations_do_not_overspend(self):
        def attempt(name):
            try:
                SpendLedger(self.path).reserve(name, plan("2.5"))
                return True
            except SupportOpsError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ["left", "right"])), 1)
        self.assertLessEqual(Decimal(self.ledger.snapshot()["encumbered_cny"]), Decimal("5"))

    def test_missing_before_event_cannot_create_receipt(self):
        self.ledger.reserve("run-one", plan())
        self.ledger.claim("run-one", plan())
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: self.ledger.event("run-one",
            {"request_number": 1, "event": "response_usage_received", "record": {"usage": None}}))

    def test_crash_after_before_is_durable_and_prevents_followup(self):
        two = plan() * 2
        self.ledger.reserve("run-one", two)
        self.ledger.claim("run-one", two)
        self.ledger.event("run-one", {"request_number": 1, "event": "before_sdk_create"})
        restarted = SpendLedger(self.path)
        self.assertEqual(restarted.snapshot()["recorded_attempts"], 1)
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: restarted.event("run-one",
            {"request_number": 2, "event": "before_sdk_create"}))

    def test_unknown_usage_stops_run_without_refund(self):
        two = plan() * 2
        self.ledger.reserve("run-one", two)
        self.ledger.claim("run-one", two)
        self.ledger.event("run-one", {"request_number": 1, "event": "before_sdk_create"})
        self.ledger.event("run-one", {"request_number": 1, "event": "failure_or_usage_unknown", "record": {"usage": None}})
        self.assert_code("EVALUATION_LIMIT_EXCEEDED", lambda: self.ledger.event("run-one",
            {"request_number": 2, "event": "before_sdk_create"}))
        self.ledger.finish("run-one")
        row = next(row for row in self.ledger.snapshot()["runs"] if row["id"] == "run-one")
        self.assertEqual(row["state"], "stopped")
        self.assertEqual(Decimal(row["reserved_cny"]), Decimal("0.2"))

    def test_receipts_have_only_validated_numbers_not_arbitrary_fields(self):
        self.ledger.reserve("run-one", plan())
        self.ledger.claim("run-one", plan())
        self.ledger.event("run-one", {"request_number": 1, "event": "before_sdk_create"})
        self.ledger.event("run-one", {"request_number": 1, "event": "response_usage_received", "record": {
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}, "ignored_private": "not-persisted"}})
        self.ledger.finish("run-one")
        self.assertNotIn(b"not-persisted", self.path.read_bytes())

    def test_partial_run_is_not_completed_and_does_not_free_budget(self):
        self.ledger.reserve("run-one", plan() * 2)
        self.ledger.claim("run-one", plan() * 2)
        self.ledger.finish("run-one")
        row = next(row for row in self.ledger.snapshot()["runs"] if row["id"] == "run-one")
        self.assertEqual(row["state"], "stopped")
        self.assertEqual(Decimal(row["reserved_cny"]), Decimal("0.2"))


class RegressionTests(unittest.TestCase):
    def test_full_worker_uses_fake_sdk_and_persistent_receipts_without_real_credentials(self):
        from test_supportops_provider_adapters import FakeSDK
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_regression()
            ledger = SpendLedger(root / "outputs/supportops/cumulative_chat_budget.sqlite3")
            ledger.reserve(RUN_ID, prepared[-1].plan)
            private_fixture = "fixture" + "-not-a-real-key-123456789"
            sdk = FakeSDK()
            with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                 patch("supportops.live_regression.prepare_regression", return_value=prepared), \
                 patch("supportops.live_regression.load_pilot_credentials", return_value=PilotCredentials(
                     "deepseek-flash", "https://api.deepseek.com", private_fixture)):
                result = execute_regression(root, authorized=True,
                    sdk_factory=lambda credentials, guard: GuardedPilotSDK(sdk, guard))
            self.assertEqual(result["financial_guard"]["sdk_create_attempts"], 32)
            self.assertEqual(result["cumulative_budget"]["recorded_attempts"], 32)
            self.assertEqual(result["summary"]["passed_case_count"], 20)
            self.assertEqual(result["external_embedding_calls"], 0)
            self.assertEqual(sdk.embedding_calls, [])
            self.assertEqual(sdk.closed, 1)
            self.assertNotIn(private_fixture, json.dumps(result))
            with self.assertRaises(SupportOpsError):
                execute_regression(root, authorized=True, sdk_factory=lambda *_: sdk)

    def test_unauthorized_before_any_prepare_or_credentials(self):
        with patch("supportops.live_regression.prepare_regression", side_effect=AssertionError("must not prepare")):
            with self.assertRaises(SupportOpsError) as caught:
                run_regression(Path("unused"), Path("outputs/no.json"))
        self.assertEqual(caught.exception.code, "EXTERNAL_CALLS_DISABLED")

    def test_complete_offline_plan_is_fixed32_and_affordable(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")), \
             patch("supportops.live_regression.load_pilot_credentials", side_effect=AssertionError("no credentials")):
            corpus, dataset, fake, guard = prepare_regression()
        self.assertEqual(len(dataset.cases), 32)
        self.assertEqual(fake["summary"]["passed_case_count"], 20)
        self.assertEqual(len(guard.plan), 32)
        self.assertLessEqual(guard.reserved + Decimal("1.228808"), Decimal("5"))

    def test_original_three_call_guard_is_not_silently_expanded(self):
        body = _wire_body([{"role": "user", "content": "Return JSON"}], 1024)
        with self.assertRaises(SupportOpsError):
            PilotSpendGuard([body] * 4)

    def test_subprocess_failure_keeps_reservation_and_rejects_second_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("supportops.live_regression.subprocess.run", side_effect=TimeoutError()), \
                 patch("supportops.live_regression.load_pilot_credentials", side_effect=AssertionError("no parent credentials")):
                with self.assertRaises(SupportOpsError) as caught:
                    run_regression(root, root / "outputs/result.json", authorized=True)
                self.assertEqual(caught.exception.code, "PILOT_PROCESS_FAILED")
                with self.assertRaises(SupportOpsError) as second:
                    run_regression(root, root / "outputs/result2.json", authorized=True)
                self.assertEqual(second.exception.code, "PILOT_ALREADY_RESERVED")
            state = SpendLedger(root / "outputs/supportops/cumulative_chat_budget.sqlite3").snapshot()
            self.assertIn(RUN_ID, [row["id"] for row in state["runs"]])


if __name__ == "__main__":
    unittest.main()
