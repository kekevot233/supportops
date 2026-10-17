"""显式启用的单机演示服务器；随机会话不是企业认证，禁止反向代理/端口转发。"""

from __future__ import annotations

import json
import secrets
import time
import webbrowser
from http import HTTPStatus
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .api import SupportOpsAPI
from .answering import AnswerEngine, DemoQuoteLLM
from .database import ApplicationDatabase
from .errors import SupportOpsError
from .models import Identity

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = [
    ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
     "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
]


class LocalSessionIdentityProvider:
    """单进程、固定身份、8 小时随机 capability；不接受公开 profile 作为凭据。"""

    def __init__(self, *, enabled=False, profile="reader", clock=time.monotonic):
        if enabled is not True:
            raise SupportOpsError("DEMO_DISABLED")
        if profile not in {"reader", "admin"}:
            raise SupportOpsError("INVALID_REQUEST")
        self._token = secrets.token_urlsafe(32)
        self._subject = "demo-" + profile
        self._clock, self._expires = clock, clock() + 8 * 3600

    def authenticate(self, credential):
        if (not isinstance(credential, str) or not credential.isascii() or len(credential) > 100 or self._clock() >= self._expires
                or not secrets.compare_digest(credential.encode("utf-8"), self._token.encode("ascii"))):
            raise SupportOpsError("UNAUTHENTICATED")
        return Identity(subject=self._subject, auth_mode="local_session")

    def launch_url(self, origin):
        # URL fragment 不发送到服务器；浏览器读取后立即从地址栏删除。
        return origin + "/#access=" + self._token


class LocalWorkbench:
    def __init__(self, database, identities, *, port, engine=None):
        if not isinstance(identities, LocalSessionIdentityProvider):
            raise SupportOpsError("DEMO_DISABLED")
        if type(port) is not int or not 1 <= port <= 65535:
            raise SupportOpsError("INVALID_REQUEST")
        self.origin = f"http://127.0.0.1:{port}"
        self.authority = f"127.0.0.1:{port}"
        self.identities = identities
        self.api = SupportOpsAPI(database, identities, engine or AnswerEngine(DemoQuoteLLM(), mode="fake_extractive"),
                                 allow_local_sessions=True)

    def __call__(self, environ, start_response):
        def hardened_start(status, headers, exc_info=None):
            names = {name.lower() for name, _ in SECURITY_HEADERS}
            return start_response(status, [(key, value) for key, value in headers if key.lower() not in names]
                                  + SECURITY_HEADERS, exc_info)

        try:
            # Host 精确匹配防 DNS rebinding；Origin/Fetch Metadata 防跨站驱动。无 CORS、无 Cookie。
            if (environ.get("REMOTE_ADDR") != "127.0.0.1" or environ.get("HTTP_HOST") != self.authority
                    or environ.get("HTTP_ORIGIN", self.origin) != self.origin
                    or environ.get("HTTP_SEC_FETCH_SITE", "none") not in {"none", "same-origin"}):
                raise SupportOpsError("FORBIDDEN")
            path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
            if path in ASSETS and method == "GET":
                filename, content_type = ASSETS[path]
                content = (Path(__file__).parent / "web" / "dist" / filename).read_bytes()
                return self._response(hardened_start, 200, content, content_type)
            if path == "/api/session" or (isinstance(path, str) and path.endswith("/jobs/process-next")):
                header = environ.get("HTTP_AUTHORIZATION", "")
                credential = header[7:] if header.startswith("Bearer ") else None
                identity = self.api.workspaces._identity(credential)
                if path == "/api/session" and method == "GET":
                    payload = {"identity": identity.model_dump(), "mode": "local_demo", "model_mode": "fake_extractive",
                               "external_model_calls": 0, "expires_in_hours_max": 8}
                else:
                    parts = path.split("/")
                    if len(parts) != 6 or parts[1:3] != ["api", "workspaces"] or method != "POST":
                        raise SupportOpsError("NOT_FOUND")
                    if self.api._body(environ):
                        raise SupportOpsError("INVALID_REQUEST")
                    job = self.api.documents.process_next(credential, parts[3])
                    payload = {"job": job.model_dump() if job else None}
                return self._response(hardened_start, 200, {"data": payload})
            return self.api(environ, hardened_start)
        except SupportOpsError as error:
            return self._response(hardened_start, error.status, error.to_dict())
        except Exception:
            return self._response(hardened_start, 503, SupportOpsError("API_UNAVAILABLE").to_dict())

    @staticmethod
    def _response(start, status, payload, content_type="application/json; charset=utf-8"):
        content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start(f"{status} {HTTPStatus(status).phrase}", [("Content-Type", content_type), ("Content-Length", str(len(content)))])
        return [content]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # 不记录请求路径、问题、Authorization 或启动 fragment。
        pass

    def handle(self):
        self.connection.settimeout(15)
        super().handle()


def configure_serve_parser(subparsers):
    parser = subparsers.add_parser("serve", help="仅监听 127.0.0.1 的离线演示工作台，禁止代理/公开部署")
    parser.add_argument("--enable-demo", action="store_true")
    parser.add_argument("--profile", choices=("reader", "admin"), default="reader")
    parser.add_argument("--database", type=Path, default=Path("outputs/supportops/application.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")


def run_server(args, *, project_root):
    identities = LocalSessionIdentityProvider(enabled=args.enable_demo, profile=args.profile)
    target = args.database.resolve()
    if not target.is_relative_to((project_root / "outputs").resolve()) or target.suffix != ".sqlite3":
        raise SupportOpsError("INVALID_REQUEST")
    with ApplicationDatabase(target) as database:
        app = LocalWorkbench(database, identities, port=args.port)
        with make_server("127.0.0.1", args.port, app, handler_class=QuietHandler) as server:
            print("本机演示，非企业认证；Fake 原文选择器，不具备语义判断。禁止代理/端口转发。", flush=True)
            print("打开下列完整链接（含临时访问凭据，勿分享；8 小时失效，重启即失效）：", flush=True)
            url = identities.launch_url(app.origin)
            print(url, flush=True)
            if args.open_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0
