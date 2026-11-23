"""进程级锁观测与任务级锁（Issue #3927）。

背景：`db_lock` / `owner_lock("runtime")` 是进程级单锁，complete 终态写与后台
health refresh 会长时间持锁（17–50s），把 claim/heartbeat/tags 全部堵在锁外。
本模块提供三件事，供各调用点按需组合：

1. `InstrumentedLock`：兼容 `threading.RLock` 用法（`with` / `acquire` / `release`
   / `locked` / 可重入）的观测包装。等待/持锁超过阈值时打印**持锁栈**并记入
   按 `(lock, owner)` 维度的指标；不改变锁语义。
2. `lock_owner(label)`：ContextVar 标记当前临界区的 owner（如 `complete`、
   `refresh:runtime`），日志/指标按 owner 归因。
3. `task_lock(task_id)`：任务级锁 `t_<id>`（弱引用式引用计数回收），同一任务串行、
   跨任务互不阻塞；用于 complete 终态写替换全局锁。

指标通过 `lock_metrics_snapshot()` 查询（进程内），并在安装 prometheus_client 时
额外导出 `grape_lock_slow_total{lock,owner,kind}` 计数器。
"""
from __future__ import annotations

import contextvars
import threading
import time
import traceback
from contextlib import contextmanager

from src.kernel.config import log

_SLOW_HOLD_SECONDS_DEFAULT = 5.0
_SLOW_WAIT_SECONDS_DEFAULT = 2.0

_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("grape_lock_owner", default="")

_metrics_guard = threading.RLock()
_metrics: dict[tuple[str, str], dict] = {}

_prom_ready = False
_prom_counter = None
_prom_guard = threading.Lock()


def _float_env(name: str, default: float) -> float:
    try:
        return float(__import__("os").environ.get(name, default))
    except Exception:
        return default


def current_owner() -> str:
    return _OWNER.get() or "unlabeled"


@contextmanager
def lock_owner(label: str):
    """在临界区内标记 owner；异常/嵌套均安全恢复。"""
    token = _OWNER.set(str(label or "unlabeled"))
    try:
        yield
    finally:
        _OWNER.reset(token)


def _metrics_entry(lock_name: str, owner: str) -> dict:
    key = (lock_name or "lock", owner or "unlabeled")
    with _metrics_guard:
        entry = _metrics.get(key)
        if entry is None:
            entry = {
                "lock": key[0],
                "owner": key[1],
                "slow_hold": 0,
                "slow_wait": 0,
                "hold_count": 0,
                "hold_seconds_total": 0.0,
                "wait_count": 0,
                "wait_seconds_total": 0.0,
                "max_hold": 0.0,
                "max_wait": 0.0,
            }
            _metrics[key] = entry
        return entry


def _prom_observe(lock_name: str, owner: str, kind: str) -> None:
    """慢锁计数器（可选，缺 prometheus_client 时静默）。"""
    global _prom_ready, _prom_counter
    if not _prom_ready:
        with _prom_guard:
            if not _prom_ready:
                try:
                    from prometheus_client import Counter

                    _prom_counter = Counter(
                        "grape_lock_slow_total",
                        "Slow lock wait/hold events by lock and owner (Issue #3927)",
                        ["lock", "owner", "kind"],
                    )
                except Exception:  # pragma: no cover - worker 包无 prometheus_client
                    _prom_counter = None
                _prom_ready = True
    if _prom_counter is not None:
        try:
            _prom_counter.labels(lock=lock_name, owner=owner, kind=kind).inc()
        except Exception:  # pragma: no cover
            pass


def _record_slow(lock_name: str, owner: str, kind: str, seconds: float,
                 stack: str) -> None:
    entry = _metrics_entry(lock_name, owner)
    with _metrics_guard:
        entry[f"slow_{kind}"] += 1
        if kind == "hold":
            entry["max_hold"] = max(entry["max_hold"], round(seconds, 4))
        else:
            entry["max_wait"] = max(entry["max_wait"], round(seconds, 4))
    _prom_observe(lock_name, owner, kind)
    log.warning(
        "db_lock slow %s: lock=%s owner=%s %s=%.3fs\nholder_stack:\n%s",
        kind, lock_name, owner, kind, seconds, stack,
    )


def lock_metrics_snapshot() -> list[dict]:
    """返回当前进程内按 (lock, owner) 聚合的慢锁指标快照。"""
    with _metrics_guard:
        return [dict(v) for v in _metrics.values()]


def reset_lock_metrics() -> None:
    with _metrics_guard:
        _metrics.clear()


class InstrumentedLock:
    """`threading.RLock` 的观测包装（可重入，语义等价）。"""

    def __init__(self, name: str,
                 slow_hold: float | None = None,
                 slow_wait: float | None = None):
        self.name = name
        self._lock = threading.RLock()
        self._local = threading.local()
        self._slow_hold = (_float_env("DB_LOCK_SLOW_HOLD_SECONDS", _SLOW_HOLD_SECONDS_DEFAULT)
                           if slow_hold is None else float(slow_hold))
        self._slow_wait = (_float_env("DB_LOCK_SLOW_WAIT_SECONDS", _SLOW_WAIT_SECONDS_DEFAULT)
                           if slow_wait is None else float(slow_wait))

    # ---- 统计 ----
    def _record(self, kind: str, seconds: float) -> None:
        owner = current_owner()
        entry = _metrics_entry(self.name, owner)
        with _metrics_guard:
            entry[f"{kind}_count"] += 1
            entry[f"{kind}_seconds_total"] = round(
                entry[f"{kind}_seconds_total"] + seconds, 4)
        if seconds >= (self._slow_hold if kind == "hold" else self._slow_wait):
            if kind == "hold":
                stack = getattr(self._local, "acquire_stack", "") or "".join(
                    traceback.format_stack(limit=12))
            else:
                stack = "".join(traceback.format_stack(limit=12))
            _record_slow(self.name, owner, kind, seconds, stack)

    # ---- RLock 协议 ----
    def acquire(self, blocking: bool = True, timeout: float = -1):
        start = time.perf_counter()
        if timeout is None or timeout < 0:
            ok = self._lock.acquire(blocking)
        else:
            ok = self._lock.acquire(blocking, timeout)
        if not ok:
            return ok
        waited = time.perf_counter() - start
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.depth = 1
            self._local.held_since = time.perf_counter()
            self._local.acquire_stack = "".join(traceback.format_stack(limit=12))
            self._local.wait = waited
        else:
            self._local.depth = depth + 1
        return True

    def release(self):
        depth = getattr(self._local, "depth", 0)
        try:
            if depth <= 1:
                held = time.perf_counter() - getattr(self._local, "held_since", time.perf_counter())
                waited = getattr(self._local, "wait", 0.0)
                self._local.depth = 0
                self._local.held_since = 0.0
                self._record("wait", waited)
                self._record("hold", held)
            else:
                self._local.depth = depth - 1
        finally:
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<InstrumentedLock {self.name!r}>"


# ===== 任务级锁 =====

_task_guard = threading.RLock()
_task_locks: dict[str, dict] = {}


@contextmanager
def task_lock(task_id):
    """任务级可重入锁 `t_<task_id>`；跨任务并行、同任务串行。

    引用计数在 **获取锁之前** 递增，释放后再递减并回收——避免「A 已从表删除、
    B 又拿到新锁」的双锁竞态。
    """
    key = str(task_id or "").strip() or "__unkeyed__"
    with _task_guard:
        entry = _task_locks.get(key)
        if entry is None:
            entry = {"lock": InstrumentedLock(f"task:{key}"), "refs": 0}
            _task_locks[key] = entry
        entry["refs"] += 1
    token = _OWNER.set(f"task:{key}")
    entry["lock"].acquire()
    try:
        yield entry["lock"]
    finally:
        entry["lock"].release()
        _OWNER.reset(token)
        with _task_guard:
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                _task_locks.pop(key, None)


def task_lock_count() -> int:
    """当前登记在册的任务锁数量（观测/测试用）。"""
    with _task_guard:
        return len(_task_locks)


__all__ = [
    "InstrumentedLock",
    "lock_owner",
    "current_owner",
    "task_lock",
    "task_lock_count",
    "lock_metrics_snapshot",
    "reset_lock_metrics",
]