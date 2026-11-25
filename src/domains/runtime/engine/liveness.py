"""pending_self_report 活跃窗：任务侧与事件侧共用的唯一一份定义。

背景（Issue #1071）：主控重启时 `terminate_active_agents` 把仍在执行的 REMOTE 任务置
`pending_self_report`，任务不再是 queued/dispatched/running。两层 sweeper 都要判「这条
任务是不是还在写执行流」：

- 任务侧 `scheduler._sweep_pending_self_report_timeouts`：活跃流豁免，命中则不判超时；
- 事件侧 `cleanup.sweep_stuck_events_once`：pending_self_report 任务活跃时事件不判 failed。

两处曾经各写一份 600（一处字面量、一处 env 可覆盖）——运维调 env 后两侧判据分叉，
正是「任务被判死、事件还 running」的复现路径。这里收敛为单一来源。

数值来源：600s 与 sweeper 扫描周期（60s）量级匹配，避免无限豁免；真沉默任务最多延迟
一轮被判死。见 docs/issue-1071/spec.md。
"""
import os

from src.kernel.config import log

_DEFAULT_PENDING_STREAM_ACTIVE_WINDOW_SECONDS = 600


def _env_int(name: str, default: int) -> int:
    """读整型 env；空值/非数字/非正数一律回落默认并告警，避免 import 期抛错打挂进程。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(float(str(raw).strip()))
    except Exception:
        log.warning(f"{name}={raw!r} 不是合法整数，回落默认 {default}")
        return default
    if value <= 0:
        log.warning(f"{name}={value} 非正数，回落默认 {default}")
        return default
    return value


PENDING_STREAM_ACTIVE_WINDOW_SECONDS = _env_int(
    "PENDING_STREAM_ACTIVE_WINDOW_SECONDS", _DEFAULT_PENDING_STREAM_ACTIVE_WINDOW_SECONDS)

# 兼容既有引用（模块内旧名 / 用例断言）——值同源，不再各自定义。
_PENDING_STREAM_ACTIVE_WINDOW_SECONDS = PENDING_STREAM_ACTIVE_WINDOW_SECONDS