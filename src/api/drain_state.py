"""停机摘流标记（Issue #3969）。

worker 收到 SIGTERM 后 uvicorn 先关闭监听再排空，systemd `Restart=always` 冷启动期间
新连接被拒 → 网关单上游直接 502。这里提供**文件型**排空标记：systemd ExecStop 在发送
SIGTERM 前写入标记并等待排空窗口，readiness（/readyz）立即转 not_ready，多 uvicorn
worker 进程共享同一文件、判定一致。

标记默认落 `/tmp/grape-worker-draining`；通过环境变量 `GRAPE_DRAIN_FILE` 指定，
未配置时所有操作均为无操作（不改变未启用摘流的部署行为）。
"""
import os
import time

DEFAULT_DRAIN_FILE = "/tmp/grape-worker-draining"
_DEFAULT_DRAIN_SECONDS = 5.0


def drain_file_path() -> str:
    return os.environ.get("GRAPE_DRAIN_FILE") or DEFAULT_DRAIN_FILE


def is_configured() -> bool:
    """是否显式启用摘流（未配置 `GRAPE_DRAIN_FILE` 时不做任何事）。"""
    return bool(os.environ.get("GRAPE_DRAIN_FILE"))


def begin_drain(path: "str | None" = None) -> None:
    """写入排空标记（幂等；未配置时无操作）。"""
    if not is_configured():
        return
    _path = path or drain_file_path()
    try:
        with open(_path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def clear_drain(path: "str | None" = None) -> None:
    """清除排空标记（进程启动时调用，避免上次停机残留导致永不 ready）。"""
    _path = path or drain_file_path()
    try:
        os.remove(_path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def is_draining(path: "str | None" = None) -> bool:
    """当前是否处于排空状态（未配置时恒 False）。"""
    if not is_configured():
        return False
    return os.path.exists(path or drain_file_path())


def drain_seconds() -> float:
    """排空等待窗口（`GRAPE_DRAIN_SECONDS`，默认 5s，负数归零）。"""
    raw = os.environ.get("GRAPE_DRAIN_SECONDS", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DRAIN_SECONDS
    return max(0.0, val)