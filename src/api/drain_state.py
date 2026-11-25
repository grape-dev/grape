"""停机摘流标记（Issue #3969）。

worker 收到 SIGTERM 后 uvicorn 先关闭监听再排空，systemd `Restart=always` 冷启动期间
新连接被拒 → 网关单上游直接 502。这里提供**文件型**排空标记：systemd ExecStop 在发送
SIGTERM 前写入标记并等待排空窗口，readiness（/readyz）立即转 not_ready，多 uvicorn
worker 进程共享同一文件、判定一致。

标记角色隔离（复审 R6）：worker 用 `GRAPE_DRAIN_FILE`（默认
`/tmp/grape-worker-draining`），main+webhook 用 `GRAPE_MAIN_DRAIN_FILE`（默认
`/tmp/grape-main-webhook-draining`）。两者不得共用一个环境变量——否则共享
`EnvironmentFile`（/etc/grape/grape.env）时，停 main/webhook 会写入 worker 的
readiness 标记，把 worker 打成永续 not_ready（标记只在 worker 启动时清）。

未配置对应环境变量时所有操作均为无操作（不改变未启用摘流的部署行为）。
"""
import os
import time

from src.kernel.config import log

DEFAULT_DRAIN_FILE = "/tmp/grape-worker-draining"
DEFAULT_MAIN_DRAIN_FILE = "/tmp/grape-main-webhook-draining"
_DEFAULT_DRAIN_SECONDS = 5.0


def drain_file_path() -> str:
    return os.environ.get("GRAPE_DRAIN_FILE") or DEFAULT_DRAIN_FILE


def main_drain_file_path() -> str:
    return os.environ.get("GRAPE_MAIN_DRAIN_FILE") or DEFAULT_MAIN_DRAIN_FILE


def is_configured() -> bool:
    """worker 是否显式启用摘流（未配置 `GRAPE_DRAIN_FILE` 时不做任何事）。"""
    return bool(os.environ.get("GRAPE_DRAIN_FILE"))


def is_main_configured() -> bool:
    """main+webhook 是否显式启用摘流（未配置 `GRAPE_MAIN_DRAIN_FILE` 时不做任何事）。"""
    return bool(os.environ.get("GRAPE_MAIN_DRAIN_FILE"))


def _write_marker(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception as e:
        log.warning(f"drain-state: write marker failed {path}: {e}")


def _remove_marker(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        log.debug(f"drain-state: marker absent on clear {path}")
    except Exception as e:
        log.warning(f"drain-state: remove marker failed {path}: {e}")


def begin_drain(path: "str | None" = None) -> None:
    """写入 worker 排空标记（幂等；未配置时无操作）。"""
    if not is_configured():
        return
    _write_marker(path or drain_file_path())


def clear_drain(path: "str | None" = None) -> None:
    """清除 worker 排空标记（进程启动时调用，避免上次停机残留导致永不 ready）。

    与 `begin_drain()` 判定对齐：未配置时同样无操作。
    """
    if not is_configured():
        return
    _remove_marker(path or drain_file_path())


def begin_main_drain(path: "str | None" = None) -> None:
    """写入 main+webhook 排空标记（角色隔离，不触碰 worker 标记）。"""
    if not is_main_configured():
        return
    _write_marker(path or main_drain_file_path())


def clear_main_drain(path: "str | None" = None) -> None:
    """清除 main+webhook 排空标记（未配置时无操作）。"""
    if not is_main_configured():
        return
    _remove_marker(path or main_drain_file_path())


def is_draining(path: "str | None" = None) -> bool:
    """worker 当前是否处于排空状态（未配置时恒 False）。"""
    if not is_configured():
        return False
    return os.path.exists(path or drain_file_path())


def is_main_draining(path: "str | None" = None) -> bool:
    """main+webhook 当前是否处于排空状态（未配置时恒 False）。"""
    if not is_main_configured():
        return False
    return os.path.exists(path or main_drain_file_path())


def drain_seconds() -> float:
    """排空等待窗口（`GRAPE_DRAIN_SECONDS`，默认 5s，负数归零）。"""
    raw = os.environ.get("GRAPE_DRAIN_SECONDS", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DRAIN_SECONDS
    return max(0.0, val)