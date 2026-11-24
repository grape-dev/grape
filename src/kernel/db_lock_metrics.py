"""db_lock 排障：可安装指标 + 持有者/等待者跟踪（kernel 层，零域依赖）。

背景（2026-11-23 现网卡顿排查）：mgmt 进程用一把进程级 ``threading.RLock``
（``src/kernel/session_manager.py: db_lock``）串起所有数据库访问。某条重查询在
``with db_lock:`` 里跑 100s 时，同进程其它请求全部排队，日志里只能看到「所有请求
同一秒结束」，看不出是谁拿住了锁——因为锁本身的等待/持有完全没埋点。

本模块提供：
1. ``TrackedRLock``：行为与 ``threading.RLock`` 一致（可重入、支持 with / acquire(timeout)），
   额外记录等待时长、持有时长、当前等待者数、当前持有者。
2. Prometheus 收集器（由 insight 域的 registry 调 ``install(registry)`` 注入，口径与
   ``src/kernel/gsb/metrics.py`` / ``src/kernel/org_metrics.py`` 一致）：
      grape_db_lock_wait_seconds{owner}   Histogram  等锁时长
      grape_db_lock_hold_seconds{owner}   Histogram  持锁时长
      grape_db_lock_waiters               Gauge      当前等锁线程数
      grape_db_lock_holder{owner}         Gauge      当前持锁者（1=持有中，0=已释放）
      grape_db_lock_holds_total{owner}    Counter    持锁次数
   ``owner`` = 请求路径归一化（数字/uuid 段 → {id}）或后台线程名，基数有界。
3. 慢等待 / 慢持有日志（默认阈值 2s，可用 DB_LOCK_SLOW_WAIT_S / DB_LOCK_SLOW_HOLD_S 调）：
   ``db_lock slow wait`` 由等待者打印，带 holder 的 owner/request_id；
   ``db_lock slow hold`` 由持有者在释放时打印。两条日志都进 Loki，可按
   ``db_lock``/``holder`` 直接检索到「谁把后续请求挡住了」。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

_dbg = logging.getLogger(__name__)

# ===== 收集器（install 前为 None，调用点全部 no-op）=====

_WAIT: Any = None
_HOLD: Any = None
_WAITERS: Any = None
_HOLDER: Any = None
_HOLDS: Any = None

# 从毫秒到 5 分钟：卡顿现场一次就是 100s 量级，桶要覆盖到
_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

_SLOW_WAIT_S = float(os.environ.get("DB_LOCK_SLOW_WAIT_S", "2.0") or 2.0)
_SLOW_HOLD_S = float(os.environ.get("DB_LOCK_SLOW_HOLD_S", "2.0") or 2.0)

# thread_ident -> {owner, rid, since, depth}
_STATE: dict[int, dict] = {}
_STATE_GUARD = threading.Lock()
# owner -> (holder_child, holds_child)：避免每次 acquire 都走 prometheus 的 label 查找
_CHILD_CACHE: dict[str, tuple] = {}


def _children(owner: str):
    ch = _CHILD_CACHE.get(owner)
    if ch is None:
        try:
            ch = (_HOLDER.labels(owner=owner), _HOLDS.labels(owner=owner))
        except Exception:
            return (None, None)
        if len(_CHILD_CACHE) > 2000:  # 兜底：标签爆炸时清空重建
            _CHILD_CACHE.clear()
        _CHILD_CACHE[owner] = ch
    return ch

_NUM_SEG_RE = re.compile(r"[0-9]{2,}")
_HEX_SEG_RE = re.compile(r"[0-9a-fA-F]{8,}")
# 带连字符的 36 位 uuid（runtimes/tasks 等按 id 取详情的热路径）
_UUID_SEG_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _normalize_path(path: str) -> str:
    """把路径里的 id 段（纯数字 / 长 hex / uuid）换成 {id}，保证标签基数有界。"""
    out = []
    for seg in path.split("/"):
        if seg and (_UUID_SEG_RE.fullmatch(seg) or _HEX_SEG_RE.fullmatch(seg)
                    or _NUM_SEG_RE.fullmatch(seg)):
            out.append("{id}")
        else:
            out.append(seg)
    return "/".join(out)


def install(registry: Any) -> None:
    """把 db_lock 指标注册到 prometheus_client CollectorRegistry（幂等）。

    同一 registry 重复调用保持幂等；换一个 registry（测试里常见）则重新绑定各
    collector 并清空 `_CHILD_CACHE` —— 缓存的子 metric 属于上一个 registry，不清空
    会让新 registry 永远读到 0。生产只有一份 registry，行为不受影响。
    """
    global _WAIT, _HOLD, _WAITERS, _HOLDER, _HOLDS
    from prometheus_client import Counter, Gauge, Histogram

    try:
        _WAIT = Histogram(
            "grape_db_lock_wait_seconds",
            "db_lock acquisition wait time (all owners).",
            buckets=_BUCKETS,
            registry=registry,
        )
        _HOLD = Histogram(
            "grape_db_lock_hold_seconds",
            "db_lock hold time per acquisition (all owners).",
            buckets=_BUCKETS,
            registry=registry,
        )
        _WAITERS = Gauge(
            "grape_db_lock_waiters",
            "Threads currently blocked waiting for db_lock.",
            registry=registry,
        )
        _HOLDER = Gauge(
            "grape_db_lock_holder",
            "1 while db_lock is held by this owner, 0 after release.",
            ["owner"],
            registry=registry,
        )
        _HOLDS = Counter(
            "grape_db_lock_holds_total",
            "db_lock acquisitions by owner.",
            ["owner"],
            registry=registry,
        )
    except ValueError:  # pragma: no cover - 同一 registry 重复注册（幂等）
        return None
    # 子 metric 已绑定到新 registry：旧 registry 的子 metric 缓存必须失效。
    _CHILD_CACHE.clear()


def _owner_key() -> str:
    """当前线程的 owner 标签：请求路径归一化 > 后台线程名。基数有界。"""
    try:
        from src.kernel.config import path_var
        p = path_var.get() or ""
    except Exception:
        p = ""
    if p:
        return _normalize_path(p)[:80]
    try:
        name = threading.current_thread().name or "thread"
    except Exception:
        name = "thread"
    return ("bg:" + name)[:80]


def _request_id() -> str:
    try:
        from src.kernel.config import request_id_var
        return request_id_var.get() or "-"
    except Exception:
        return "-"


def _log():
    from src.kernel.config import log
    return log


def _current_holder_desc() -> str:
    """当前持锁者描述（owner#rid），无则 '-'。"""
    with _STATE_GUARD:
        for st in _STATE.values():
            if st.get("depth", 0) > 0:
                return f"{st.get('owner')}#{st.get('rid')}"
    return "-"


class TrackedRLock:
    """threading.RLock 的埋点替身：接口兼容，额外记录等待/持有并打慢日志。"""

    __slots__ = ("_lock", "__weakref__")

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---- 兼容 RLock 接口 ----

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.perf_counter()
        # 先拍持有者快照：等到了之后再拍就是自己了（初版日志里 waiter==holder）
        holder_before = _current_holder_desc()
        if _WAITERS is not None:
            _WAITERS.inc()
        try:
            if timeout is None or timeout < 0:
                ok = self._lock.acquire(blocking)
            else:
                ok = self._lock.acquire(blocking, timeout)
        finally:
            if _WAITERS is not None:
                _WAITERS.dec()
        waited = time.perf_counter() - t0
        if ok:
            self._on_acquired(waited, holder_before)
        return ok

    def release(self) -> None:
        held = self._on_release_pre()
        self._lock.release()
        if held is not None:
            self._on_release_post(held)

    def __enter__(self) -> "TrackedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _is_owned(self) -> bool:  # pymysql/threading 兼容
        return self._lock._is_owned()  # type: ignore[attr-defined]

    # ---- 埋点 ----

    def _on_acquired(self, waited: float, holder_before: str = "-") -> None:
        tid = threading.get_ident()
        first = False
        with _STATE_GUARD:
            st = _STATE.get(tid)
            if st is None:
                st = {"owner": _owner_key(), "rid": _request_id(),
                      "since": time.perf_counter(), "depth": 0}
                _STATE[tid] = st
            st["depth"] += 1
            first = st["depth"] == 1
        if waited >= _SLOW_WAIT_S:
            try:
                _log().warning(
                    "db_lock slow wait: waited=%.2fs waiter=%s holder=%s",
                    waited, f"{st['owner']}#{st['rid']}", holder_before,
                    extra={"db_lock_waited_s": round(waited, 3),
                           "db_lock_holder": holder_before},
                )
            except Exception as _le:
                _dbg.debug("db_lock slow-wait log failed: %s", _le)
        if first:
            if _WAIT is not None:
                _WAIT.observe(waited)
            if _HOLDER is not None:
                _hc, _kc = _children(st["owner"])
                try:
                    if _hc is not None:
                        _hc.set(1)
                    if _kc is not None:
                        _kc.inc()
                except Exception as _me:
                    _dbg.debug("db_lock holder metric failed: %s", _me)

    def _on_release_pre(self):
        """释放前置：只做计数递减；真正 release 成功后再记时长。"""
        tid = threading.get_ident()
        with _STATE_GUARD:
            st = _STATE.get(tid)
            if st is None:
                return None
            st["depth"] -= 1
            if st["depth"] > 0:
                return None
            _STATE.pop(tid, None)
            return (time.perf_counter() - st["since"], st)

    def _on_release_post(self, held_payload) -> None:
        held, st = held_payload
        if _HOLD is not None:
            _HOLD.observe(held)
        if _HOLDER is not None:
            try:
                _hc, _kc = _children(st["owner"])
                if _hc is not None:
                    _hc.set(0)
            except Exception as _me:
                _dbg.debug("db_lock holder metric failed: %s", _me)
        if held >= _SLOW_HOLD_S:
            try:
                _log().warning(
                    "db_lock slow hold: held=%.2fs owner=%s",
                    held, f"{st['owner']}#{st['rid']}",
                    extra={"db_lock_held_s": round(held, 3),
                           "db_lock_owner": st["owner"]},
                )
            except Exception as _le:
                _dbg.debug("db_lock slow-hold log failed: %s", _le)


def snapshot() -> list[dict]:
    """当前持锁线程快照（调试/测试用）。"""
    with _STATE_GUARD:
        return [dict(v) for v in _STATE.values() if v.get("depth", 0) > 0]