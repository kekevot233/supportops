"""仅供浏览器测试：临时库、两个随机本机会话、stdin 故障注入；不修改默认业务库。"""

import json
import sys
import tempfile
from pathlib import Path
from threading import Thread
from wsgiref.simple_server import make_server

from supportops.answering import AnswerEngine, DemoQuoteLLM
from supportops.database import ApplicationDatabase
from supportops.local_server import LocalSessionIdentityProvider, LocalWorkbench, QuietHandler


class FaultLLM:
    def __init__(self):
        self.fail_next = False

    def invoke(self, messages, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("TEST_ONLY_PRIVATE_BACKEND_DETAIL")
        return DemoQuoteLLM().invoke(messages, **kwargs)


def main():
    root = Path(__file__).resolve().parent.parent / "outputs" / "supportops"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="browser-", dir=root) as directory, ApplicationDatabase(Path(directory) / "app.sqlite3") as db:
        db.bootstrap_demo(enabled=True)
        servers, apps, urls, llms, threads = [], {}, {}, {}, []
        try:
            for profile in ("admin", "reader"):
                provider = LocalSessionIdentityProvider(enabled=True, profile=profile)
                server = make_server("127.0.0.1", 0, None, handler_class=QuietHandler)
                llm = FaultLLM()
                app = LocalWorkbench(db, provider, port=server.server_port, engine=AnswerEngine(llm, mode="fake_extractive"))
                server.set_app(app)
                thread = Thread(target=server.serve_forever, daemon=True);thread.start()
                servers.append(server);threads.append(thread);apps[profile]=app;llms[profile]=llm
                urls[profile] = provider.launch_url(app.origin)
            print(json.dumps({"ready": urls}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                if command["action"] == "stop": break
                if command["action"] == "fail_model": llms["reader"].fail_next = True
                elif command["action"] == "fail_index":
                    index=apps["admin"].api.documents._index
                    original=index.build
                    def fail_once(*args, **kwargs):
                        index.build=original
                        raise RuntimeError("TEST_ONLY_PRIVATE_INDEX_DETAIL")
                    index.build=fail_once
                else: raise ValueError("Unknown test command")
                print(json.dumps({"ack": command["id"]}), flush=True)
        finally:
            for server in servers: server.shutdown();server.server_close()
            for thread in threads:thread.join(timeout=3)


if __name__ == "__main__":
    main()
