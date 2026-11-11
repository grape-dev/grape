"""Mgmt 进程启动入口（:38080）。

启动：python apps/mgmt/main.py [--port 38080] [--workers 2]
等价：uvicorn src.api.grape_server:app --host 0.0.0.0 --port 38080 --workers 2
"""
import os, sys

# 确保项目根目录在 sys.path（支持从任意 CWD 启动 python apps/mgmt/main.py）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.api.version_check import ensure_python_version
ensure_python_version()


def main():
    import argparse
    import uvicorn
    from src.api.grape_server import app
    parser = argparse.ArgumentParser(description="Grape Mgmt Server (:38080)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_PORT", "38080")))
    parser.add_argument("--host", default=os.environ.get("GRAPE_HOST", "0.0.0.0"))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("GRAPE_WORKERS", "2")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, workers=args.workers, log_level="info")


if __name__ == "__main__":
    main()