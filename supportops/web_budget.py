"""网页真实请求的持久化限额；共享原累计账本，不退款、不重置历史。"""
import json
import re
from decimal import Decimal

from .errors import SupportOpsError
from .qa_cases import canonical_sha256
from .qa_runtime import ProviderUsage
from .spend_ledger import CAP, SpendLedger, micros


class WebSessionBudget(SpendLedger):
    def __init__(self, path, *, session_id, budget_cny, request_budget_cny, max_requests, binding):
        if (not isinstance(session_id, str) or not re.fullmatch(r"web-[a-z0-9-]{1,60}", session_id)
                or type(max_requests) is not int or not 1 <= max_requests <= 32):
            raise SupportOpsError("INVALID_REQUEST")
        self.session_id = session_id
        self.cap, self.per_request = micros(budget_cny), micros(request_budget_cny)
        if self.per_request > self.cap:
            raise SupportOpsError("INVALID_REQUEST")
        # 不允许缺失旧账本时隐式创建并遗忘 full32 占用。
        if not path.is_file():
            raise SupportOpsError("WEB_BUDGET_NOT_READY")
        super().__init__(path)
        policy = canonical_sha256({"cap": self.cap, "per_request": self.per_request,
            "maximum": max_requests, "binding": binding})
        with self._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS web_sessions ("
                "id TEXT PRIMARY KEY,policy_sha TEXT NOT NULL,cap INTEGER NOT NULL,"
                "per_request INTEGER NOT NULL,maximum INTEGER NOT NULL,stopped INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE IF NOT EXISTS web_attempts ("
                "run_id TEXT PRIMARY KEY REFERENCES runs(id),session_id TEXT NOT NULL REFERENCES web_sessions(id),"
                "owner TEXT NOT NULL,conversation TEXT NOT NULL,application_run TEXT NOT NULL UNIQUE)")
            old = connection.execute("SELECT policy_sha FROM web_sessions WHERE id=?", (session_id,)).fetchone()
            if old and old[0] != policy:
                raise SupportOpsError("CONFLICT")
            if not old:
                connection.execute("INSERT INTO web_sessions(id,policy_sha,cap,per_request,maximum) VALUES(?,?,?,?,?)",
                    (session_id, policy, self.cap, self.per_request, max_requests))

    def begin_call(self, context, reservation):
        if (not isinstance(context, dict) or set(context) != {"owner", "conversation", "run_id", "workspace"}
                or context["owner"] != "demo-reader" or context["workspace"] != "helloagent"
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value)
                       for value in context.values())):
            raise SupportOpsError("FORBIDDEN")
        amount = micros(reservation["reserved_cny"])
        # application_run 绑定幂等业务请求，换启动会话也不能重放同一调用。
        run_id = "webcall-" + canonical_sha256(context)[:48]
        with self._transaction() as connection:
            session = connection.execute("SELECT stopped,maximum FROM web_sessions WHERE id=?", (self.session_id,)).fetchone()
            rows = connection.execute("SELECT r.reserved,c.state FROM web_attempts w JOIN runs r ON r.id=w.run_id "
                "JOIN calls c ON c.run_id=r.id WHERE w.session_id=?", (self.session_id,)).fetchall()
            if session[0] or any(row[1] != "received" for row in rows):
                raise SupportOpsError("WEB_SESSION_STOPPED")
            total = connection.execute("SELECT coalesce(sum(reserved),0) FROM runs").fetchone()[0]
            if (amount > self.per_request or len(rows) >= session[1]
                    or sum(row[0] for row in rows) + amount > self.cap or total + amount > CAP):
                raise SupportOpsError("WEB_BUDGET_EXCEEDED")
            if connection.execute("SELECT 1 FROM web_attempts WHERE application_run=?", (context["run_id"],)).fetchone():
                raise SupportOpsError("CONFLICT")
            # 预留、claim 和 attempted 同一事务提交，子进程启动失败也按可能发送保留。
            connection.execute("INSERT INTO runs VALUES(?,?,?,'started',1)",
                               (run_id, amount, canonical_sha256([reservation])))
            connection.execute("INSERT INTO calls VALUES(?,1,'attempted',NULL)", (run_id,))
            connection.execute("INSERT INTO web_attempts VALUES(?,?,?,?,?)",
                (run_id, self.session_id, context["owner"], context["conversation"], context["run_id"]))
        return run_id

    def finish_call(self, run_id, usage, *, failed=False):
        receipt = ProviderUsage.model_validate(usage).model_dump() if usage is not None else None
        with self._transaction() as connection:
            updated = connection.execute("UPDATE calls SET state=?,receipt=? WHERE run_id=? AND slot=1 AND state='attempted' "
                "AND EXISTS(SELECT 1 FROM web_attempts WHERE run_id=? AND session_id=?)",
                ("received" if receipt else "unknown", json.dumps(receipt), run_id, run_id, self.session_id))
            if updated.rowcount != 1:
                raise SupportOpsError("CONFLICT")
            stop = failed or receipt is None
            connection.execute("UPDATE runs SET state=? WHERE id=?", ("stopped" if stop else "completed", run_id))
            if stop:
                connection.execute("UPDATE web_sessions SET stopped=1 WHERE id=?", (self.session_id,))

    def session_view(self):
        with self._transaction() as connection:
            session = connection.execute("SELECT stopped,maximum FROM web_sessions WHERE id=?", (self.session_id,)).fetchone()
            rows = connection.execute("SELECT r.reserved,c.state FROM web_attempts w JOIN runs r ON r.id=w.run_id "
                "JOIN calls c ON c.run_id=r.id WHERE w.session_id=?", (self.session_id,)).fetchall()
            total = connection.execute("SELECT coalesce(sum(reserved),0) FROM runs").fetchone()[0]
        return {"session_id": self.session_id, "request_attempts": len(rows), "max_requests": session[1],
            "session_budget_cny": str(Decimal(self.cap) / 1_000_000),
            "request_budget_cny": str(Decimal(self.per_request) / 1_000_000),
            "session_reserved_cny": str(Decimal(sum(row[0] for row in rows)) / 1_000_000),
            "cumulative_reserved_cny": str(Decimal(total) / 1_000_000),
            "cumulative_available_cny": str(Decimal(CAP - total) / 1_000_000),
            "stopped": bool(session[0]) or any(row[1] != "received" for row in rows),
            "reservation_not_invoice": True, "provider_invoice_cost_cny": None}

    def stop_session(self):
        with self._transaction() as connection:
            connection.execute("UPDATE web_sessions SET stopped=1 WHERE id=?", (self.session_id,))

    def run_view(self, application_run):
        with self._transaction() as connection:
            row = connection.execute("SELECT c.state,c.receipt,r.reserved FROM web_attempts w JOIN calls c ON c.run_id=w.run_id "
                "JOIN runs r ON r.id=w.run_id WHERE w.application_run=?", (application_run,)).fetchone()
        if row is None:
            return {"external_model_attempts": 0, "usage_source": "not_called", "actual_usage": None, "cost": None}
        usage = json.loads(row[1]) if row[1] else None
        estimate = ((Decimal(usage["input_tokens"]) * 2 + Decimal(usage["output_tokens"]) * 8) / 1_000_000) if usage else None
        return {"external_model_attempts": 1, "usage_source": "client_reported" if usage else "unknown",
            "actual_usage": usage, "reserved_cny": str(Decimal(row[2]) / 1_000_000),
            "receipt_priced_upper_estimate_cny": str(estimate) if estimate is not None else None, "cost": None}
