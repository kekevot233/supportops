"""One bounded Chat call. No credentials in argv, inherited environment or output."""
import json
import logging
import os
from pathlib import Path
import sys


def main():
    logging.disable(logging.CRITICAL)
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHON_DOTENV_DISABLED", "PYTHONIOENCODING"}
    if not sys.flags.isolated or {key.upper() for key in os.environ} - allowed or len(sys.argv) != 1:
        return 2
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    try:
        raw = sys.stdin.buffer.read(60001)
        if len(raw) > 60000:
            return 2
        from supportops.web_live import execute_web_worker
        result = execute_web_worker(root, json.loads(raw.decode("ascii")))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception:
        # 不打印 traceback/配置/SDK 例外。
        print('{"code":"MODEL_UNAVAILABLE"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
