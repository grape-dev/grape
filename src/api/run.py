"""Agentic DevOps API 启动入口 - 生产模式：主 API 内网 3000 + webhook 8080，由 nginx 反代对外 80/443；--dev 单端口 reload"""
import sys, os

# Issue #455：主服务要求 Python >= 3.10（新式类型注解/语法依赖）。低于下限时在 import 任何业务模块
# 之前显式提示并退出，避免 3.9 环境下导入阶段即报语法错误、排查成本高。
from src.api.version_check import ensure_python_version
ensure_python_version()

import uvicorn, multiprocessing, signal

# 加载 .env 文件（OBS 等环境变量）
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def _start_main():
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=3000, log_level="info")


def _start_webhook():
    uvicorn.run("src.api.run_webhook:app", host="0.0.0.0", port=8080, log_level="info")


_STOP_TIMEOUT = float(os.environ.get("GRAPE_STOP_TIMEOUT", "20"))


def _drain_before_stop() -> float:
    """Issue #3969：SIGTERM 后先摘流并等待在飞请求排空，再 terminate 子进程。

    仅当部署配置 `GRAPE_DRAIN_FILE` 时生效（未配置 = 保持原行为）。摘流标记让
    /readyz 转 not_ready，等待窗口（`GRAPE_DRAIN_SECONDS`，默认 5s）内不再接新流量；
    随后 uvicorn 子进程按既有 graceful shutdown 排空剩余连接。
    """
    from src.api import drain_state

    if not drain_state.is_configured():
        return 0
    drain_state.begin_drain()
    _secs = drain_state.drain_seconds()
    if _secs > 0:
        import time
        time.sleep(_secs)
    return _secs


def _shutdown(p1, p2):
    """父进程 SIGTERM：先摘流排空，再 terminate 并等待子进程优雅收尾（原 Step 2 语义）。"""
    _drain_before_stop()
    p1.terminate()
    p2.terminate()
    for _p in (p1, p2):
        try:
            _p.join(timeout=_STOP_TIMEOUT)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dev":
        uvicorn.run("src.api.main:app", host="0.0.0.0", port=3000, reload=True)
        sys.exit(0)

    p1 = multiprocessing.Process(target=_start_main, name="main-api")
    p2 = multiprocessing.Process(target=_start_webhook, name="webhook")
    p1.start()
    p2.start()

    # Step 2（graceful-shutdown-requeue-design.md）：父进程 SIGTERM 后等待子进程完成优雅收尾
    # （子进程 uvicorn lifespan shutdown 内会执行 terminate_active_agents 等），再退出，
    # 避免孤儿 / systemd 二次起服窗口。
    signal.signal(signal.SIGINT, lambda *_: _shutdown(p1, p2))
    signal.signal(signal.SIGTERM, lambda *_: _shutdown(p1, p2))
    p1.join()
    p2.join()
