"""Local persistent cumulative reservations. Unknown costs are never refunded.

Integer micro-CNY accounting and BEGIN IMMEDIATE serialize concurrent runs.
This is a conservative local reservation system, not provider billing.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3

from .errors import SupportOpsError
from .qa_cases import canonical_sha256

APP_ID = 1397706828
LEGACY = (("legacy-first-unknown", 1_000_000), ("legacy-round2-reserved", 228_808))
CAP = 5_000_000


def micros(value):
    try:
        number = Decimal(str(value)) * 1_000_000
        valid = number.is_finite() and number == number.to_integral_value() and 0 < number <= CAP
    except (InvalidOperation, ValueError, TypeError):
        valid = False
    if not valid:
        raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
    return int(number)


class SpendLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise SupportOpsError("PREFLIGHT_INVALID")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            app = connection.execute("PRAGMA application_id").fetchone()[0]
            if app not in (0, APP_ID):
                raise SupportOpsError("STORAGE_VERSION_UNSUPPORTED")
            if not app:
                if connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                    raise SupportOpsError("STORAGE_VERSION_UNSUPPORTED")
                connection.execute(f"PRAGMA application_id={APP_ID}")
                connection.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,reserved INTEGER NOT NULL CHECK(reserved>0),"
                                   "plan_sha TEXT NOT NULL,state TEXT NOT NULL,maximum INTEGER NOT NULL)")
                connection.execute("CREATE TABLE calls(run_id TEXT NOT NULL,slot INTEGER NOT NULL,state TEXT NOT NULL,"
                                   "receipt TEXT,PRIMARY KEY(run_id,slot),FOREIGN KEY(run_id) REFERENCES runs(id))")
                connection.executemany("INSERT INTO runs VALUES(?,?,?,'historical',0)",
                                       [(name, amount, "historical-conservative-reservation") for name, amount in LEGACY])
            for name, amount in LEGACY:
                row = connection.execute("SELECT reserved,state FROM runs WHERE id=?", (name,)).fetchone()
                if row != (amount, "historical"):
                    raise SupportOpsError("PREFLIGHT_INVALID")
            if connection.execute("SELECT coalesce(sum(reserved),0) FROM runs").fetchone()[0] > CAP:
                raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise SupportOpsError("STORAGE_UNAVAILABLE") from None
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            if connection is not None:
                connection.close()

    def snapshot(self):
        with self._transaction() as connection:
            rows = connection.execute("SELECT id,reserved,state,maximum FROM runs ORDER BY id").fetchall()
            attempts = connection.execute("SELECT count(*) FROM calls").fetchone()[0]
        reserved = sum(row[1] for row in rows)
        return {"authorized_total_cny": "5", "encumbered_cny": str(Decimal(reserved)/1_000_000),
                "available_cny": str(Decimal(CAP-reserved)/1_000_000), "recorded_attempts": attempts,
                "historical_attempts": {"first_round_possible_maximum": 3, "round2_confirmed": 3},
                "reservation_not_invoice": True, "provider_invoice_cost_cny": None,
                "runs": [{"id": row[0], "reserved_cny": str(Decimal(row[1])/1_000_000),
                          "state": row[2], "maximum_calls": row[3]} for row in rows]}

    def reserve(self, run_id, plan):
        if not re.fullmatch(r"[a-z0-9-]{1,100}", run_id) or not 1 <= len(plan) <= 32:
            raise SupportOpsError("INVALID_REQUEST")
        amount = sum(micros(row["reserved_cny"]) for row in plan)
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise SupportOpsError("PILOT_ALREADY_RESERVED")
            used = connection.execute("SELECT sum(reserved) FROM runs").fetchone()[0]
            if amount + used > CAP:
                raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
            connection.execute("INSERT INTO runs VALUES(?,?,?,'reserved',?)",
                               (run_id, amount, canonical_sha256(plan), len(plan)))

    def claim(self, run_id, plan):
        with self._transaction() as connection:
            updated = connection.execute("UPDATE runs SET state='started' WHERE id=? AND plan_sha=? AND state='reserved'",
                                         (run_id, canonical_sha256(plan)))
            if updated.rowcount != 1:
                raise SupportOpsError("PILOT_ALREADY_RESERVED")

    def event(self, run_id, event):
        slot = event["request_number"]
        kind = event["event"]
        if type(slot) is not int or kind not in {"before_sdk_create", "response_usage_received", "failure_or_usage_unknown"}:
            raise SupportOpsError("INVALID_REQUEST")
        with self._transaction() as connection:
            run = connection.execute("SELECT state,maximum FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run[0] != "started" or not 1 <= slot <= run[1]:
                raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
            if kind == "before_sdk_create":
                prior = connection.execute("SELECT slot,state FROM calls WHERE run_id=? ORDER BY slot", (run_id,)).fetchall()
                if slot != len(prior) + 1 or any(row[1] != "received" for row in prior):
                    raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
                connection.execute("INSERT INTO calls VALUES(?,?,'attempted',NULL)", (run_id, slot))
            else:
                from .qa_runtime import ProviderUsage
                usage = event["record"].get("usage")
                receipt = ProviderUsage.model_validate(usage).model_dump() if usage is not None else None
                state = "received" if kind == "response_usage_received" and receipt else "unknown"
                updated = connection.execute("UPDATE calls SET state=?,receipt=? WHERE run_id=? AND slot=? AND state='attempted'",
                    (state, json.dumps(receipt), run_id, slot))
                if updated.rowcount != 1:
                    raise SupportOpsError("EVALUATION_LIMIT_EXCEEDED")
                if state == "unknown":
                    connection.execute("UPDATE runs SET state='stopped' WHERE id=?", (run_id,))

    def finish(self, run_id):
        with self._transaction() as connection:
            row = connection.execute("SELECT maximum FROM runs WHERE id=? AND state='started'", (run_id,)).fetchone()
            if row:
                received = connection.execute("SELECT count(*) FROM calls WHERE run_id=? AND state='received'", (run_id,)).fetchone()[0]
                state = "completed" if received == row[0] else "stopped"
                connection.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
