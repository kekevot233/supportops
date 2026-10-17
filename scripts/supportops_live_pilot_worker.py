"""由显式授权的父进程以 python -I 和最小环境启动；不接受命令行密钥。"""

import json
import logging
import os
import sys
from pathlib import Path


def emit_payload(payload):
    # -I 忽略 PYTHONIOENCODING；ASCII JSON 在 GBK/UTF-8 控制台均可安全传递。
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False))


def main():
    logging.disable(logging.CRITICAL)
    # 不继承 HOME/代理/OpenAI/SMTP/其他应用变量，不临时修改全局环境。
    allowed = {"SystemRoot", "WINDIR", "TEMP", "TMP", "PYTHON_DOTENV_DISABLED", "PYTHONIOENCODING"}
    if {key.upper() for key in os.environ} - {key.upper() for key in allowed} or not sys.flags.isolated:
        return 2
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from supportops.live_pilot import AUTHORIZATION_ID, TOTAL_BUDGET_CNY, PRIOR_UNKNOWN_RESERVE_CNY, execute_worker
    from supportops.errors import SupportOpsError
    if sys.argv[1:] != [AUTHORIZATION_ID]:
        return 2
    grant = root / "outputs" / "supportops" / (AUTHORIZATION_ID + ".grant.json")
    if not grant.is_file():
        return 2
    try:
        metadata = json.loads(grant.read_text(encoding="utf-8"))
        if (metadata.get("authorization_id") != AUTHORIZATION_ID
                or metadata.get("state") != "reserved_no_automatic_retry"
                or metadata.get("reserved_chat_attempts") != 3 or metadata.get("budget_cny") != str(TOTAL_BUDGET_CNY)
                or metadata.get("prior_unknown_reserved_cny") != str(PRIOR_UNKNOWN_RESERVE_CNY)):
            return 2
        # worker 同样独占领取；直接重跑脚本或父进程超时也不会重复使用本轮授权。
        with grant.with_suffix(".started").open("x", encoding="utf-8") as handle:
            handle.write(AUTHORIZATION_ID)
        with grant.with_suffix(".events.jsonl").open("x", encoding="utf-8") as journal:
            def event_sink(event):
                journal.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            payload = execute_worker(root, authorized=True, event_sink=event_sink)
        with grant.with_suffix(".worker-report.json").open("x", encoding="utf-8") as report:
            json.dump(payload, report, ensure_ascii=False, allow_nan=False)
            report.flush()
            os.fsync(report.fileno())
        emit_payload(payload)
        return 0
    except SupportOpsError as error:
        emit_payload({"code": error.code})
        return 2
    except Exception:
        print('{"code":"PILOT_PROCESS_FAILED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
