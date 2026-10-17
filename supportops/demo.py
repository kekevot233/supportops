"""仅供本机 CLI 演示的应用入口，无 HTTP 监听和真实登录。"""

from __future__ import annotations

from pathlib import Path

from .database import ApplicationDatabase
from .documents import DocumentService
from .answering import AnswerEngine, DemoQuoteLLM
from .chat import ChatService
from .errors import SupportOpsError
from .identity import DemoIdentityProvider
from .workspaces import WorkspaceService


def configure_demo_parser(subparsers):
    demo = subparsers.add_parser("demo", help="本机空间和权限演示，不是生产认证")
    demo.add_argument("--enable-demo", action="store_true", help="显式启用公开的演示身份")
    demo.add_argument("--profile", choices=("admin", "reader"), default="reader")
    demo.add_argument("--database", type=Path, default=Path("outputs/supportops/application.sqlite3"))
    actions = demo.add_subparsers(dest="action", required=True)
    actions.add_parser("init", help="一次性创建两个人工演示空间和两名成员")
    actions.add_parser("workspaces", help="仅列出当前身份有权访问的空间")
    create = actions.add_parser("create-workspace")
    create.add_argument("name")
    for name in ("workspace", "members", "audit", "set-member", "remove-member"):
        action = actions.add_parser(name)
        action.add_argument("workspace_id")
        if name in {"set-member", "remove-member"}:
            action.add_argument("member_id")
        if name == "set-member":
            action.add_argument("role", choices=("reader", "editor", "admin"))
    for name in ("upload", "update", "documents", "document", "jobs", "job", "retry", "work", "search", "citation", "delete", "import-seed"):
        action = actions.add_parser(name)
        action.add_argument("workspace_id")
        if name in {"update", "document", "citation", "delete"}:
            action.add_argument("document_id")
        if name in {"upload", "update"}:
            action.add_argument("--filename", required=True)
            action.add_argument("--title", required=True)
            action.add_argument("--content", required=True, help="演示文本，不是文件路径；不要在命令历史中放敏感资料")
            action.add_argument("--key", required=True)
        if name in {"job", "retry"}:
            action.add_argument("job_id")
        if name == "search":
            action.add_argument("question")
        if name == "citation":
            action.add_argument("version_id")
            action.add_argument("chunk_id")
        if name == "delete":
            action.add_argument("--confirm", action="store_true", help="确认删除原件与全部索引")
    for name in ("new-chat", "chats", "chat", "ask", "answer", "answer-citation", "feedback", "run"):
        action = actions.add_parser(name)
        action.add_argument("workspace_id")
        if name == "new-chat":
            action.add_argument("--title", default="新会话")
        if name in {"chat", "ask"}:
            action.add_argument("conversation_id")
        if name == "ask":
            action.add_argument("question")
            action.add_argument("--key", required=True)
        if name in {"answer", "answer-citation", "feedback"}:
            action.add_argument("answer_id")
        if name == "answer-citation":
            action.add_argument("label")
        if name == "feedback":
            action.add_argument("rating", choices=("useful", "not_useful"))
            action.add_argument("--reason", choices=("irrelevant", "incorrect", "outdated", "other"))
        if name == "run":
            action.add_argument("run_id")


def run_demo(args, *, project_root: Path) -> dict:
    provider = DemoIdentityProvider(enabled=args.enable_demo)
    identity = provider.authenticate(args.profile)
    target = args.database.resolve()
    if not target.is_relative_to((project_root / "outputs").resolve()) or target.suffix != ".sqlite3":
        raise SupportOpsError("INVALID_REQUEST")
    if args.action == "init" and args.profile != "admin":
        raise SupportOpsError("FORBIDDEN")
    with ApplicationDatabase(target) as database:
        service = WorkspaceService(database, provider)
        if args.action == "init":
            result = {"initialized": database.bootstrap_demo(enabled=True)}
        elif args.action == "workspaces":
            result = [row.model_dump() for row in service.list_workspaces(args.profile)]
        elif args.action == "create-workspace":
            result = service.create_workspace(args.profile, {"name": args.name}).model_dump()
        elif args.action == "workspace":
            result = service.get_workspace(args.profile, args.workspace_id).model_dump()
        elif args.action == "members":
            result = [row.model_dump() for row in service.list_members(args.profile, args.workspace_id)]
        elif args.action == "audit":
            result = [row.model_dump() for row in service.list_audit(args.profile, args.workspace_id)]
        elif args.action == "set-member":
            result = service.set_member(args.profile, args.workspace_id,
                                        {"member_id": args.member_id, "role": args.role}).model_dump()
        elif args.action == "remove-member":
            service.remove_member(args.profile, args.workspace_id, args.member_id)
            result = {"removed": True}
        elif args.action in {"new-chat", "chats", "chat", "ask", "answer", "answer-citation", "feedback", "run"}:
            chat = ChatService(database, provider, AnswerEngine(DemoQuoteLLM(), mode="fake_extractive"))
            result = _chat_action(chat, args)
        else:
            documents = DocumentService(database, provider)
            result = _document_action(documents, service, args)
    return {"mode": "local_demo", "warning": "本地演示身份，非企业认证；禁止对外服务。",
            "identity": identity.model_dump(), "model_calls": 0,
            "answer_mode": "fake_extractive" if args.action == "ask" else None, "result": result}


def _chat_action(chat, args):
    credential, workspace = args.profile, args.workspace_id
    if args.action == "new-chat":
        return chat.create_conversation(credential, workspace, {"title": args.title})
    if args.action == "chats":
        return chat.list_conversations(credential, workspace)
    if args.action == "chat":
        return chat.get_conversation(credential, workspace, args.conversation_id)
    if args.action == "ask":
        return chat.send_message(credential, workspace, args.conversation_id,
                                 {"question": args.question, "idempotency_key": args.key})
    if args.action == "answer":
        return chat.get_answer(credential, workspace, args.answer_id)
    if args.action == "answer-citation":
        return chat.read_answer_citation(credential, workspace, args.answer_id, args.label)
    if args.action == "feedback":
        return chat.put_feedback(credential, workspace, args.answer_id, {"rating": args.rating, "reason": args.reason})
    return chat.get_run(credential, workspace, args.run_id)


def _document_action(documents, workspaces, args):
    credential, workspace = args.profile, args.workspace_id
    if args.action in {"upload", "update"}:
        return documents.upload(credential, workspace, {"filename": args.filename, "title": args.title,
            "idempotency_key": args.key}, args.content.encode("utf-8"),
            document_id=getattr(args, "document_id", None)).model_dump()
    if args.action == "documents":
        return [row.model_dump() for row in documents.list_documents(credential, workspace)]
    if args.action == "document":
        return documents.get_document(credential, workspace, args.document_id).model_dump()
    if args.action == "jobs":
        return [row.model_dump() for row in documents.list_jobs(credential, workspace)]
    if args.action == "job":
        return documents.get_job(credential, workspace, args.job_id).model_dump()
    if args.action == "retry":
        return documents.retry_job(credential, workspace, args.job_id).model_dump()
    if args.action == "work":
        job = documents.process_next(credential, workspace)
        return {"job": job.model_dump() if job else None}
    if args.action == "search":
        return {"mode": "retrieval_only", **documents.search(credential, workspace, args.question).model_dump(mode="json")}
    if args.action == "citation":
        return documents.read_citation(credential, workspace, args.document_id, args.version_id, args.chunk_id)
    if args.action == "delete":
        if not args.confirm:
            raise SupportOpsError("INVALID_REQUEST")
        return documents.delete_document(credential, workspace, args.document_id)
    # 只导入 P0 显式清单，不扫描工作区或验收目录。
    from .seed import load_seed_corpus
    workspaces.get_workspace(credential, workspace)
    corpus = load_seed_corpus()
    return [documents.upload(credential, workspace, {"filename": f"{doc.id}.md", "title": doc.title,
        "idempotency_key": f"seed-{corpus.sha256[:16]}-{doc.id}"}, doc.content.encode("utf-8")).model_dump()
        for doc in corpus.documents]


__all__ = ["configure_demo_parser", "run_demo"]
