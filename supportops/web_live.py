"""本机真实问答：公开种子库、独立业务库、逐请求隔离 worker。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from hello_agents import RequestBudgetConfig, RequestBudgetManager

from .answering import AnswerEngine, DEFAULT_PROMPT_PROFILE, system_rules_for
from .documents import DocumentService
from .errors import SupportOpsError
from .identity import DemoIdentityProvider
from .live_pilot import (PilotSpendGuard, _reservation, _wire_body, build_isolated_sdk,
                         isolated_environment, load_pilot_credentials, worker_isolation_valid)
from .provider_adapters import ChatSDKConfig, CompatibleChatClient
from .qa_cases import canonical_sha256
from .qa_runtime import ProviderUsage
from .seed import load_seed_corpus
from .web_budget import WebSessionBudget


def prepare_public_database(database):
    """仅在专用 live 库初始化公开资料；不读取/复制原工作台历史。"""
    database.bootstrap_demo(enabled=True)
    provider = DemoIdentityProvider(enabled=True)
    documents = DocumentService(database, provider)
    corpus = load_seed_corpus()
    for doc in corpus.documents:
        documents.upload("admin", "helloagent", {"filename": doc.id + ".md", "title": doc.title,
            "idempotency_key": "live-seed-" + corpus.sha256[:16] + "-" + doc.id}, doc.content.encode("utf-8"))
    for _ in range(len(corpus.documents)):
        job = documents.process_next("admin", "helloagent")
        if job is None:
            break
        if job.state != "succeeded":
            raise SupportOpsError("INDEX_UNAVAILABLE")
    expected = sorted(hashlib.sha256(doc.content.encode("utf-8")).hexdigest() for doc in corpus.documents)
    with database.transaction() as connection:
        originals = connection.execute("SELECT o.content FROM document_originals o JOIN documents d ON "
            "o.workspace_id=d.workspace_id AND o.document_id=d.id AND o.version_id=d.current_version_id "
            "WHERE d.workspace_id='helloagent'").fetchall()
        count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
    if count != len(expected) or sorted(hashlib.sha256(row[0]).hexdigest() for row in originals) != expected:
        raise SupportOpsError("WEB_PUBLIC_SCOPE_REQUIRED")
    return corpus


def execute_web_worker(project_root, body, *, sdk_factory=None):
    """只由 -I / 最小环境 worker 调用；一请求一进程，无后台自动请求。"""
    if not worker_isolation_valid():
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    if not isinstance(body, dict) or body != _wire_body(body.get("messages"), body.get("max_tokens"), 0.0):
        raise SupportOpsError("INVALID_REQUEST")
    _reservation(body)
    guard = PilotSpendGuard([body], max_requests=1)
    credentials = load_pilot_credentials(project_root, authorized=True)
    client = CompatibleChatClient((sdk_factory or build_isolated_sdk)(credentials, guard),
        ChatSDKConfig(model="deepseek-flash", token_parameter="max_tokens", json_mode=True, timeout_seconds=30),
        owns_client=True)
    try:
        try:
            completed = client.complete(body["messages"], max_tokens=body["max_tokens"], temperature=0.0)
            row = guard.snapshot()["calls"][0]
            result = {"text": completed.text, "usage": row["usage"], "error_code": completed.error_code,
                      "bound_violation": guard.stopped}
        except Exception:
            calls = guard.snapshot()["calls"]
            result = {"text": "", "usage": calls[0]["usage"] if calls else None,
                      "error_code": "MODEL_UNAVAILABLE", "bound_violation": True}
    finally:
        client.close()
    # 凭据即使被供应商回显也不能离开 worker；不输出 SDK 异常/headers。
    return json.loads(json.dumps(result, ensure_ascii=True, allow_nan=False).replace(credentials.api_key, "[REDACTED]"))


class IsolatedWebLLM:
    def __init__(self, project_root, budget, *, runner=None):
        self.root, self.budget = Path(project_root).resolve(), budget
        self._runner = runner or self._subprocess

    def _subprocess(self, body):
        process = subprocess.run([sys.executable, "-I", str(self.root / "scripts/supportops_web_worker.py")],
            input=json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.root, env=isolated_environment(),
            timeout=45, check=False, **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}))
        if process.returncode or not 0 < len(process.stdout) <= 100000:
            raise SupportOpsError("MODEL_UNAVAILABLE")
        return json.loads(process.stdout.decode("ascii"))

    def invoke_for_run(self, messages, *, max_tokens, context):
        body = _wire_body(messages, max_tokens, 0.0)
        try:
            reservation = _reservation(body)
        except SupportOpsError:
            raise SupportOpsError("WEB_REQUEST_TOO_LARGE") from None
        run_id = self.budget.begin_call(context, reservation)
        finished = False
        try:
            result = self._runner(body)
            if not isinstance(result, dict) or set(result) != {"text", "usage", "error_code", "bound_violation"}:
                raise ValueError("Invalid worker result")
            usage = ProviderUsage.model_validate(result["usage"]).model_dump() if result["usage"] is not None else None
            failed = (result["error_code"] is not None or result["bound_violation"] is not False or usage is None
                or (usage is not None and (usage["input_tokens"] > reservation["assumed_input_tokens"]
                    or usage["output_tokens"] > reservation["reserved_output_tokens"]))
                or not isinstance(result["text"], str) or not 0 < len(result["text"]) <= 16000)
            self.budget.finish_call(run_id, usage, failed=failed)
            finished = True
            if failed:
                raise SupportOpsError("MODEL_RESPONSE_INVALID" if result["error_code"] == "MODEL_RESPONSE_INVALID" else "MODEL_UNAVAILABLE")
            return result["text"]
        except Exception as error:
            if not finished:
                self.budget.finish_call(run_id, None, failed=True)
            raise SupportOpsError(error.code if isinstance(error, SupportOpsError) else "MODEL_UNAVAILABLE") from None


class WebAnswerEngine(AnswerEngine):
    def __init__(self, llm, corpus):
        self.public_contents = tuple(doc.content for doc in corpus.documents)
        super().__init__(llm, mode="local_deepseek", budget=RequestBudgetManager(
            config=RequestBudgetConfig(output_tokens=1024)))

    def generate(self, question, history, result, diagnostics, *, on_invoke=None, invocation_context=None):
        if not invocation_context or invocation_context.get("workspace") != "helloagent":
            raise SupportOpsError("WEB_PUBLIC_SCOPE_REQUIRED")
        if any(not any(hit.chunk.content in text for text in self.public_contents) for hit in result.hits):
            raise SupportOpsError("WEB_PUBLIC_SCOPE_REQUIRED")
        try:
            return super().generate(question, history, result, diagnostics, on_invoke=on_invoke,
                                    invocation_context=invocation_context)
        except Exception:
            # 原文协议失败发生在 SDK 成功之后，仍停止该会话；已付费不能当作未发生。
            if self.llm.budget.run_view(invocation_context["run_id"])["external_model_attempts"]:
                self.llm.budget.stop_session()
            raise


def configure_live_parser(subparsers):
    parser = subparsers.add_parser("serve-live", help="显式启用 DeepSeek 本机网页；会发送问题与公开资料并产生费用")
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--ack-public-data", action="store_true", help="确认仅输入可发送给 DeepSeek 的公开问题/资料")
    parser.add_argument("--session-id", required=True, help="web- 开头；重启复用同一 ID，不重置额度")
    parser.add_argument("--session-budget-cny", required=True)
    parser.add_argument("--request-budget-cny", default="0.20")
    parser.add_argument("--max-chat-requests", type=int, default=10)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open-browser", action="store_true")


def run_live_server(args, *, project_root):
    from .database import ApplicationDatabase
    from .local_server import LocalSessionIdentityProvider, LocalWorkbench, QuietHandler
    from wsgiref.simple_server import make_server
    import webbrowser

    if args.enable_live is not True or args.ack_public_data is not True:
        raise SupportOpsError("EXTERNAL_CALLS_DISABLED")
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        raise SupportOpsError("INVALID_REQUEST")
    root = Path(project_root).resolve()
    target = root / "outputs/supportops/live-workbench.sqlite3"
    ledger = root / "outputs/supportops/cumulative_chat_budget.sqlite3"
    if target.resolve() != target or ledger.resolve() != ledger:
        raise SupportOpsError("INVALID_REQUEST")
    binding = {"corpus_sha256": load_seed_corpus().sha256, "model": "deepseek-flash",
        "prompt_profile": DEFAULT_PROMPT_PROFILE,
        "system_sha256": hashlib.sha256(system_rules_for(DEFAULT_PROMPT_PROFILE).encode()).hexdigest(),
        "database_sha256": canonical_sha256(str(target)), "pricing_checked_on": "2026-10-19",
        "input_cny_per_million": "2", "output_cny_per_million": "8", "output_tokens": 1024}
    budget = WebSessionBudget(ledger, session_id=args.session_id, budget_cny=args.session_budget_cny,
        request_budget_cny=args.request_budget_cny, max_requests=args.max_chat_requests, binding=binding)
    identities = LocalSessionIdentityProvider(enabled=True, profile="reader")
    with ApplicationDatabase(target) as database:
        corpus = prepare_public_database(database)
        engine = WebAnswerEngine(IsolatedWebLLM(root, budget), corpus)
        app = LocalWorkbench(database, identities, port=args.port, engine=engine, live_budget=budget)
        with make_server("127.0.0.1", args.port, app, handler_class=QuietHandler) as server:
            print("真实 DeepSeek / 本机私有入口；问题、相关对话和公开证据会发往供应商。禁止敏感资料与公网代理。", flush=True)
            print("按发送前保守预留计入原累计5元账本；不退款，非供应商账单。失败/未知后停止，无自动重试。", flush=True)
            print(json.dumps(budget.session_view(), ensure_ascii=False), flush=True)
            url = identities.launch_url(app.origin)
            print("打开完整链接（临时访问凭据，勿分享）：\n" + url, flush=True)
            if args.open_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0
