"""Run every connector. One failing connector is logged and the rest keep going."""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectors import CONNECTORS  # noqa: E402
from db import KB_HOME, connect, init, log_ingest  # noqa: E402


def env_flag(name):
    path = os.path.join(KB_HOME, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip("\"'").lower() in ("1", "true", "yes")
    return False


def main(only=None):
    con = connect()
    init(con)
    print(f"=== ingest {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    for name, fn in CONNECTORS.items():
        if only and name not in only:
            continue
        t = time.time()
        try:
            msg = fn(con)
            log_ingest(con, name, True, msg)
            print(f"  {name}: {msg} ({time.time() - t:.1f}s)", flush=True)
        except Exception as e:
            log_ingest(con, name, False, f"{type(e).__name__}: {e}")
            print(f"  {name}: FAILED {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
    if env_flag("KB_AUTO_SUMMARIZE"):
        try:
            import summarize
            msg = summarize.run(con)
            log_ingest(con, "summaries", True, msg)
            print(f"  summaries: {msg}", flush=True)
        except Exception as e:
            log_ingest(con, "summaries", False, f"{type(e).__name__}: {e}")
            print(f"  summaries: FAILED {e}", flush=True)
    else:
        print("  summaries: off (set KB_AUTO_SUMMARIZE=1 in .env after approving the backfill cost)")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
