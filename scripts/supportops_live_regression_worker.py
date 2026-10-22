"""Isolated one-shot full32 worker. Credentials never enter argv/environment."""
import json
import logging
import os
from pathlib import Path
import sys


def main():
    logging.disable(logging.CRITICAL)
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHON_DOTENV_DISABLED", "PYTHONIOENCODING"}
    if not sys.flags.isolated or {name.upper() for name in os.environ} - allowed:
        return 2
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from supportops.live_regression import RUN_ID, execute_regression, paths
    from supportops.errors import SupportOpsError
    if sys.argv[1:] != [RUN_ID]:
        return 2
    try:
        payload = execute_regression(root, authorized=True)
        output, _, _ = paths(root)
        with (output / (RUN_ID + ".worker-report.json")).open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps(payload, ensure_ascii=True, allow_nan=False))
        return 0
    except SupportOpsError as error:
        print(json.dumps({"code": error.code}))
        return 2
    except Exception:
        print('{"code":"PILOT_PROCESS_FAILED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
