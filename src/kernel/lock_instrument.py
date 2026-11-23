"""进程级锁观测与任务级锁（Issue #3927）。

背景：`db_lock` / `owner_lock("runtime")` 是进程级单锁，complete 终态写与后台
health refresh 会长时间持锁（17–50s），把 claim/heartbeat/tags 全部堵在锁外。
本模块提供四件事，供各调用点按需组合：

1. `InstrumentedLock`：兼容 `threading.RLock` 用法（`with` / `acquire` / `release`
   / `locked` / 可重入）的观测包装。等待/持锁超过阈值时打印对应栈并记入按
   `(lock, owner)` 维度的指标；不改变锁语义。慢等待日志同时给出**等待方栈**
   （waiter_stack）与**上一持锁方栈**（holder_stack(previous)），避免排障
   "谁持锁 17–50s"被指错方向（Issue #3927 审查 Optional 修正）。
2. `lock_owner(label)`：ContextVar 标记当前临界区的 owner（如 `complete`、
   `refresh:runtime`），日志/指标按 owner 归因。
3. `task_lock(task_id)`：任务级锁 `t_<task_id>`（弱引用式引用计数回收），
   同一任务串行、跨任务互不阻塞。
4. `event_lock(seq)`：事件终态保护锁 `t_ev:<seq>`（引用计数回收）。Issue #3927
   审查 R1：**所有终态写者**（含不带 task_id 的判死/回收/接管面）统一按事件
   维度取锁，`SELECT status → UPDATE` 恢复原子；task_id 仅作归因与同任务附加串行。

指标基数有界（Issue #3927 审查 R3）：锁名用粗粒度常量（`task_lock` / `event_lock`），
owner 尾段实体 id（task uuid / seq / runtime id）在**指标层**折叠为 `prefix:*`
（日志仍打印完整 owner）；`(lock, owner)` 条目数受 `LOCK_METRICS_MAX_ENTRIES`
（默认 512）约束，超限折叠进 `(lock, "__overflow__")` 桶——长稳进程不再无界增长，
Prometheus `grape_lock_slow_total{lock,owner,kind}` 标签基数不再随任务数爆炸。

指标通过 `lock_metrics_snapshot()` 查询（进程内），并在安装 prometheus_client 时
额外导出 `grape_lock_slow_total{lock,owner,kind}` 计数器（owner 同样粗化）。
"""
from __future__ import annotations

import contextvars
import re
import threading
import time
import traceback
from contextlib import contextmanager

from src.kernel.config import log

_SLOW_HOLD_SECONDS_DEFAULT = 5.0
_SLOW_WAIT_SECONDS_DEFAULT = 2.0
_METRICS_MAX_ENTRIES_DEFAULT = 512
_METRICS_OVERFLOW_OWNER = "__overflow__"

_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("grape_lock_owner", default="")

_metrics_guard = threading.RLock()
_metrics: dict[tuple[str, str], dict] = {}

_prom_ready = False
_prom_counter = None
_prom_guard = threading.Lock()

# 尾段含数字（task uuid / seq / runtime id 等实体 id）→ 指标层折叠为 prefix:*。
# 仅作用于指标/Prometheus 标签；日志仍输出完整 owner 便于归因。
_DIGIT_RE = re.compile(r"\d")


def _float_env(name: str, default: float) -> float:
    try:
        return float(__import__("os").environ.get(name, default))
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(__import__("os").environ.get(name, default))
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


def _coarse_owner(owner: str) -> str:
    """指标用 owner 粗化：尾段含实体 id 数字 → `prefix:*`（基数有界）。"""
    if not owner:
        return "unlabeled"
    head, sep, tail = owner.rpartition(":")
    if sep and tail and _DIGIT_RE.search(tail):
        return f"{head}:*"
    return owner


def _metrics_max_entries() -> int:
    return max(16, _int_env("LOCK_METRICS_MAX_ENTRIES", _METRICS_MAX_ENTRIES_DEFAULT))


def _metrics_entry(lock_name: str, owner: str) -> dict:
    key = (lock_name or "lock", _coarse_owner(owner))
    with _metrics_guard:
        entry = _metrics.get(key)
        if entry is None:
            if len(_metrics) >= _metrics_max_entries():
                # R3：容量上限。新键折叠进该 lock 的 __overflow__ 桶，防长稳进程
                # 指标字典无界增长（每把锁名至多多一个桶，总数仍有界）。
                key = (key[0], _METRICS_OVERFLOW_OWNER)
                entry = _metrics.get(key)
                if entry is None:
                    entry = _new_metrics_entry(key)
                    _metrics[key] = entry
                return entry
            entry = _new_metrics_entry(key)
            _metrics[key] = entry
        return entry


def _new_metrics_entry(key: tuple[str, str]) -> dict:
    return {
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


def _prom_observe(lock_name: str, owner: str, kind: str) -> None:
    """慢锁计数器（可选，缺 prometheus_client 时静默）。owner 粗化防标签基数爆炸。"""
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
            _prom_counter.labels(lock=lock_name, owner=_coarse_owner(owner), kind=kind).inc()
        except Exception:  # pragma: no cover
            pass


def _record_slow(lock_name: str, owner: str, kind: str, seconds: float,
                 stack: str, holder_stack: str = "") -> None:
    entry = _metrics_entry(lock_name, owner)
    with _metrics_guard:
        entry[f"slow_{kind}"] += 1
        if kind == "hold":
            entry["max_hold"] = max(entry["max_hold"], round(seconds, 4))
        else:
            entry["max_wait"] = max(entry["max_wait"], round(seconds, 4))
    _prom_observe(lock_name, owner, kind)
    if kind == "wait":
        log.warning(
            "db_lock slow wait: lock=%s owner=%s wait=%.3fs\n"
            "waiter_stack:\n%s\nholder_stack(previous):\n%s",
            lock_name, owner, seconds, stack, holder_stack or "<unavailable>",
        )
    else:
        log.warning(
            "db_lock slow hold: lock=%s owner=%s hold=%.3fs\nholder_stack:\n%s",
            lock_name, owner, seconds, stack,
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
        # 上一持锁方的 acquire 栈快照（慢等待归因用；release 后保留至下一次 acquire）
        self._last_holder_stack = ""

    # ---- 统计 ----
    def _record(self, kind: str, seconds: float, stack: str = "",
                holder_stack: str = "") -> None:
        owner = current_owner()
        entry = _metrics_entry(self.name, owner)
        with _metrics_guard:
            entry[f"{kind}_count"] += 1
            entry[f"{kind}_seconds_total"] = round(
                entry[f"{kind}_seconds_total"] + seconds, 4)
            if kind == "wait":
                entry["max_wait"] = max(entry["max_wait"], round(seconds, 4))
            else:
                entry["max_hold"] = max(entry["max_hold"], round(seconds, 4))
        threshold = self._slow_hold if kind == "hold" else self._slow_wait
        if seconds >= threshold:
            if kind == "hold":
                stack = stack or getattr(self._local, "acquire_stack", "") or "".join(
                    traceback.format_stack(limit=12))
                _record_slow(self.name, owner, kind, seconds, stack)
            else:
                _record_slow(self.name, owner, kind, seconds,
                             stack or "".join(traceback.format_stack(limit=12)),
                             holder_stack=holder_stack)

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
            prev_holder_stack = self._last_holder_stack
            acquire_stack = "".join(traceback.format_stack(limit=12))
            self._local.depth = 1
            self._local.held_since = time.perf_counter()
            self._local.acquire_stack = acquire_stack
            self._local.wait = waited
            self._last_holder_stack = acquire_stack
            # 等待观测在 acquire 处收口：此刻才拿得到「上一持锁方」的栈快照，
            # 且 waiter 栈就是真实阻塞现场（等待方调用点）。
            self._record("wait", waited, stack=acquire_stack,
                         holder_stack=prev_holder_stack)
        else:
            self._local.depth = depth + 1
        return True

    def release(self):
        depth = getattr(self._local, "depth", 0)
        try:
            if depth <= 1:
                held = time.perf_counter() - getattr(self._local, "held_since", time.perf_counter())
                self._local.depth = 0
                self._local.held_since = 0.0
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


# ===== 实体级锁（引用计数回收） =====

_entity_guard = threading.RLock()
_entity_locks: dict[str, dict] = {}


@contextmanager
def _entity_lock(key: str, metric_name: str, owner_label: str):
    """引用计数式实体锁：获取前递增、释放后递减，归零回收——避免「A 已从表删除、
    B 又拿到新锁」的双锁竞态。指标锁名用粗粒度常量（R3 基数控制）。"""
    with _entity_guard:
        entry = _entity_locks.get(key)
        if entry is None:
            entry = {"lock": InstrumentedLock(metric_name), "refs": 0}
            _entity_locks[key] = entry
        entry["refs"] += 1
    token = _OWNER.set(owner_label)
    entry["lock"].acquire()
    try:
        yield entry["lock"]
    finally:
        entry["lock"].release()
        _OWNER.reset(token)
        with _entity_guard:
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                _entity_locks.pop(key, None)


@contextmanager
def task_lock(task_id):
    """任务级可重入锁 `t_<task_id>`；跨任务并行、同任务串行。

    引用计数在 **获取锁之前** 递增，释放后再递减并回收——避免「A 已从表删除、
    B 又拿到新锁」的双锁竞态。
    """
    key = str(task_id or "").strip() or "__unkeyed__"
    with _entity_lock(f"task:{key}", "task_lock", f"task:{key}"):
        yield


@contextmanager
def event_lock(seq):
    """事件终态保护锁 `t_ev:<seq>`；同一事件全部终态写者共用（Issue #3927 审查 R1）。

    传/不传 task_id 的 `update_event_complete`、`reset_event_terminal` 等都按事件
    维度串行，`SELECT status → UPDATE` 恢复原子；跨事件互不阻塞。
    """
    try:
        key = f"event:{int(seq)}"
    except (TypeError, ValueError):
        key = f"event:{seq}"
    with _entity_lock(key, "event_lock", f"event:{seq}"):
        yield


def task_lock_count() -> int:
    """当前登记在册的实体锁数量（观测/测试用）。"""
    with _entity_guard:
        return len(_entity_locks)


__all__ = [
    "InstrumentedLock",
    "lock_owner",
    "current_owner",
    "task_lock",
    "event_lock",
    "task_lock_count",
    "lock_metrics_snapshot",
    "reset_lock_metrics",
]
