"""仅供定点浏览器检查：全新临时库、模拟付费回执，不读取真实凭据。"""
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
from wsgiref.simple_server import make_server

from supportops.answering import DemoQuoteLLM
from supportops.database import ApplicationDatabase
from supportops.local_server import LocalSessionIdentityProvider, LocalWorkbench, QuietHandler
from supportops.spend_ledger import SpendLedger
from supportops.web_budget import WebSessionBudget
from supportops.web_live import IsolatedWebLLM, WebAnswerEngine, prepare_public_database


def main():
    directory = Path(__file__).resolve().parents[1] / "outputs/supportops/browser"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live-mock-", dir=directory) as name:
        root = Path(name)
        ledger = root / "mock-budget.sqlite3"
        SpendLedger(ledger)
        budget = WebSessionBudget(ledger, session_id="web-browser-mock", budget_cny="0.2",
            request_budget_cny="0.2", max_requests=1, binding="offline-browser-fixture")
        def worker(body):
            return {"text": DemoQuoteLLM().invoke(body["messages"]),
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                "error_code": None, "bound_violation": False}
        with ApplicationDatabase(root / "app.sqlite3") as database:
            corpus = prepare_public_database(database)
            identities = LocalSessionIdentityProvider(enabled=True)
            with make_server("127.0.0.1", 0, None, handler_class=QuietHandler) as server:
                engine = WebAnswerEngine(IsolatedWebLLM(root, budget, runner=worker), corpus)
                app = LocalWorkbench(database, identities, port=server.server_port, engine=engine, live_budget=budget)
                server.set_app(app)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    print(json.dumps({"ready": identities.launch_url(app.origin)}), flush=True)
                    for line in sys.stdin:
                        if line.strip() == "stop":
                            break
                finally:
                    server.shutdown()
                    thread.join(timeout=3)


if __name__ == "__main__":
    main()
