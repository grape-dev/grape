"""Event 进程启动入口（:38081）。

启动：python apps/runtime/webhook/main.py [--port 38081] [--workers 1]
等价：uvicorn src.api.grape_event_server:app --host 0.0.0.0 --port 38081 --workers 1
"""
import os, sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.api.version_check import ensure_python_version
ensure_python_version()


def main():
    import argparse
    import uvicorn
    from src.api.grape_event_server import app
    parser = argparse.ArgumentParser(description="Grape Event Server (:38081)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_PORT", "38081")))
    parser.add_argument("--host", default=os.environ.get("GRAPE_HOST", "0.0.0.0"))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("GRAPE_WORKERS", "1")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, workers=args.workers, log_level="info")


if __name__ == "__main__":
    main()