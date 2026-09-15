"""Agentic DevOps API 启动入口 - 生产模式：主 API 内网 3000 + webhook 8080，由 nginx 反代对外 80/443；--dev 单端口 reload"""
import uvicorn, multiprocessing, signal, sys, os

# 加载 .env 文件（OBS 等环境变量）
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def _start_main():
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=3000, log_level="info")


def _start_webhook():
    uvicorn.run("src.api.run_webhook:app", host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dev":
        uvicorn.run("src.api.main:app", host="0.0.0.0", port=3000, reload=True)
        sys.exit(0)

    p1 = multiprocessing.Process(target=_start_main, name="main-api")
    p2 = multiprocessing.Process(target=_start_webhook, name="webhook")
    p1.start()
    p2.start()

    def _shutdown(*_):
        p1.terminate()
        p2.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    p1.join()
    p2.join()
