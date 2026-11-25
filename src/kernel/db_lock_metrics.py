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
4. **持有中看门狗**（Issue #4295）：后台守护线程按 1s 周期读 ``snapshot()``，
   某 owner 持有超过 ``DB_LOCK_WATCHDOG_HOLD_S``（默认 5s）时**当场**打 ERROR 并
   dump 持锁线程栈（同 owner 60s 内只打一次）。慢持有日志只在 release 后才出现，
   100s 级的持有过去只能事后回看；看门狗让最长持有者当场可定位。
5. **有界等锁**（Issue #4295）：``TrackedRLock.acquire()`` 在``前台请求``上下文
   （``path_var`` 非空，即正在服务一个 HTTP 请求）里改用 ``DB_LOCK_ACQUIRE_TIMEOUT_S``
   （默认 5s，0=关闭→回退无界等锁）作为等待预算；预算耗尽抛
   ``LockAcquireTimeout``，由 API 层映射成 **503 + Retry-After**（区别于「上游 VCS
   故障」的 502）。后台线程/批处理无请求上下文 → 保持无界，语义不变。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import sys
import threading
import time
import traceback
from typing import Any

_dbg = logging.getLogger(__name__)

# ===== 收集器（install 前为 None，调用点全部 no-op）=====

_WAIT: Any = None
_HOLD: Any = None
_WAITERS: Any = None
_HOLDER: Any = None
_HOLDS: Any = None
_TIMEOUTS: Any = None
_WATCHDOG_HITS_METRIC: Any = None

# 从毫秒到 5 分钟：卡顿现场一次就是 100s 量级，桶要覆盖到
_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

_SLOW_WAIT_S = float(os.environ.get("DB_LOCK_SLOW_WAIT_S", "2.0") or 2.0)
_SLOW_HOLD_S = float(os.environ.get("DB_LOCK_SLOW_HOLD_S", "2.0") or 2.0)

# ===== 有界等锁预算（Issue #4295）：配置在调用时按 env 现读，便于单测 monkeypatch =====

ACQUIRE_TIMEOUT_ENV = "DB_LOCK_ACQUIRE_TIMEOUT_S"
_ACQUIRE_TIMEOUT_DEFAULT_S = 5.0


def _env_float(name: str, default: float) -> float:
    """读浮点 env（缺失/非法 → default）。调用时读，不接受 import 期快照。"""
    try:
        raw = os.environ.get(name, "")
        return float(raw) if str(raw).strip() != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


class LockAcquireTimeout(RuntimeError):
    """前台请求等 db_lock 超出预算（Issue #4295）。

    与"上游 VCS 故障"（502）语义分离：这是**本进程内**锁等待超时，请求可重试，
    API 层统一映射 503 + ``Retry-After``。由 ``TrackedRLock.__enter__``/``acquire``
    在预算耗尽时抛出——抛出点在进入临界区**之前**，所以不存在"没拿到锁却执行了 DB 写"。
    """

    def __init__(self, timeout: float, waited: float, holder: str = "-", owner: str = "-") -> None:
        self.timeout = float(timeout)
        self.waited = float(waited)
        self.holder = holder or "-"
        self.owner = owner or "-"
        super().__init__(
            f"db_lock acquire timed out after {self.timeout:.2f}s "
            f"(waited {self.waited:.2f}s, holder={self.holder}, waiter={self.owner})")


def _request_path() -> str:
    try:
        from src.kernel.config import path_var
        return path_var.get() or ""
    except Exception:
        return ""


# 显式预算覆盖（仅 foreground_lock_budget 使用）：0 = 未覆盖
_BUDGET_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("db_lock_budget_override", default=0.0)


def acquire_budget_s() -> float:
    """当前线程的等锁预算（秒）；0 = 无界等锁（旧语义）。

    只有**前台 HTTP 请求**线程（``path_var`` 非空）受预算约束：后台 sweeper /
    refresh / 批处理没有请求上下文，保持原来的无界行为，避免把长任务打成 503。
    ``foreground_lock_budget()`` 可显式覆盖（测试/无请求上下文的前台入口）。
    """
    override = _BUDGET_OVERRIDE.get()
    if override and override > 0:
        return float(override)
    budget = _env_float(ACQUIRE_TIMEOUT_ENV, _ACQUIRE_TIMEOUT_DEFAULT_S)
    if budget <= 0:
        return 0.0
    return budget if _request_path() else 0.0


@contextlib.contextmanager
def foreground_lock_budget(seconds: float | None = None):
    """显式声明"这段代码属于前台路径"（无请求上下文的线程里也可用）。

    仅用于测试与极少数无 path_var 的前台入口；常规 HTTP 请求靠 ``path_var`` 自动判定，
    不需要本上下文。
    """
    budget = acquire_budget_s() if seconds is None else float(seconds)
    if budget <= 0:
        yield 0.0
        return
    tok = _BUDGET_OVERRIDE.set(budget)
    try:
        yield budget
    finally:
        _BUDGET_OVERRIDE.reset(tok)

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
    global _WAIT, _HOLD, _WAITERS, _HOLDER, _HOLDS, _TIMEOUTS, _WATCHDOG_HITS_METRIC
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
        # Issue #4295：前台请求等锁超预算（→ 503）与看门狗命中，按 owner 维度可巡检。
        _TIMEOUTS = Counter(
            "grape_db_lock_acquire_timeouts_total",
            "db_lock bounded acquisitions that ran out of budget (Issue #4295).",
            ["owner"],
            registry=registry,
        )
        _WATCHDOG_HITS_METRIC = Counter(
            "grape_db_lock_watchdog_events_total",
            "db_lock holders reported by the hold watchdog (Issue #4295).",
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

    __slots__ = ("_lock", "_bound_in_request", "__weakref__")

    def __init__(self, bound_in_request: bool = False) -> None:
        """``bound_in_request=True`` = 前台请求里改用有界等锁（Issue #4295）。

        只给**进程级全局 db_lock** 打开：它是把整个进程 DB 访问串起来的单点，无界等待
        会演成 99.9s 持有 → 502。域级 owner_lock 是跨域写的短临界区，保持旧语义。
        """
        self._lock = threading.RLock()
        self._bound_in_request = bool(bound_in_request)

    # ---- 兼容 RLock 接口 ----

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.perf_counter()
        # 先拍持有者快照：等到了之后再拍就是自己了（初版日志里 waiter==holder）
        holder_before = _current_holder_desc()
        # 显式 timeout（如 acquire(timeout=30)）语义不变：等不到返回 False。
        # 未指定 timeout 时，前台请求上下文（path_var 非空）改用有界等锁预算，
        # 预算耗尽抛 LockAcquireTimeout（进入临界区之前失败 → 无未加锁的 DB 访问）。
        bounded = bool(timeout is None or timeout < 0)
        # blocking=False 不能带 timeout（threading 语义：ValueError），预算只在阻塞等锁时生效
        budget = (acquire_budget_s()
                  if (bounded and blocking and self._bound_in_request) else 0.0)
        if _WAITERS is not None:
            _WAITERS.inc()
        try:
            if budget > 0:
                ok = self._lock.acquire(blocking, budget)
            elif bounded:
                ok = self._lock.acquire(blocking)
            else:
                ok = self._lock.acquire(blocking, timeout)
        finally:
            if _WAITERS is not None:
                _WAITERS.dec()
        waited = time.perf_counter() - t0
        if ok:
            self._on_acquired(waited, holder_before)
        elif budget > 0:
            self._on_budget_exhausted(budget, waited, holder_before)
        return ok

    def release(self) -> None:
        held = self._on_release_pre()
        self._lock.release()
        if held is not None:
            self._on_release_post(held)

    def __enter__(self) -> "TrackedRLock":
        # 有界等锁：预算耗尽时 acquire() 直接抛 LockAcquireTimeout（不返回 False），
        # 因此不会出现「没拿到锁却执行了临界区」。
        if self.acquire() is False:  # pragma: no cover - 仅显式 timeout 路径可能为 False
            raise LockAcquireTimeout(0.0, 0.0, "-", _owner_key())
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
                st = {"owner": _owner_key(), "rid": _request_id(), "tid": tid,
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

    def _on_budget_exhausted(self, budget: float, waited: float, holder_before: str) -> None:
        """前台请求等锁超预算：记指标 + ERROR，然后抛 LockAcquireTimeout（→ 503）。"""
        owner = _owner_key()
        if _TIMEOUTS is not None:
            try:
                _TIMEOUTS.labels(owner=owner).inc()
            except Exception as _me:  # pragma: no cover - 指标绝不阻断主流程
                _dbg.debug("db_lock timeout metric failed: %s", _me)
        try:
            _log().error(
                "db_lock acquire timeout: budget=%.2fs waited=%.2fs waiter=%s holder=%s",
                budget, waited, f"{owner}#{_request_id()}", holder_before,
                extra={"db_lock_waited_s": round(waited, 3),
                       "db_lock_holder": holder_before,
                       "db_lock_budget_s": round(budget, 3)},
            )
        except Exception as _le:  # pragma: no cover
            _dbg.debug("db_lock timeout log failed: %s", _le)
        raise LockAcquireTimeout(budget, waited, holder_before, owner)

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

# ===== 持有中看门狗（Issue #4295）=====
#
# 慢持有日志只在 release 之后才出现（见 ``_on_release_post``）：一次 99.9s 的持有，
# 日志要等 100s 后才落地，且当时已无法还原"卡在哪一行"。看门狗在持有**期间**按周期
# 扫 ``snapshot()``，超阈值立即 ERROR + dump 持锁线程栈（``sys._current_frames()``），
# 并按 owner 60s 去重避免日志风暴。
#
# 硬约束：看门狗**绝不获取 db_lock**，只读 ``_STATE`` 快照（_STATE_GUARD 为独立小锁）。

WATCHDOG_HOLD_ENV = "DB_LOCK_WATCHDOG_HOLD_S"
WATCHDOG_PERIOD_ENV = "DB_LOCK_WATCHDOG_S"
WATCHDOG_DEDUP_ENV = "DB_LOCK_WATCHDOG_DEDUP_S"
_WATCHDOG_STACK_FRAMES = 15

_WATCHDOG_THREAD: threading.Thread | None = None
_WATCHDOG_STOP = threading.Event()
_WATCHDOG_START_LOCK = threading.Lock()
# owner -> 上次上报的 perf_counter；同 owner 在去重窗口内不重复上报
_WATCHDOG_REPORTED: dict[str, float] = {}


def watchdog_hold_s() -> float:
    return _env_float(WATCHDOG_HOLD_ENV, 5.0)


def watchdog_period_s() -> float:
    return _env_float(WATCHDOG_PERIOD_ENV, 1.0)


def watchdog_dedup_s() -> float:
    return _env_float(WATCHDOG_DEDUP_ENV, 60.0)


def _holder_stack(tid: Any, limit: int = _WATCHDOG_STACK_FRAMES) -> list[str]:
    """持锁线程的栈（最后 limit 帧）。线程已退出/无 frame → 单行占位，不抛异常。"""
    try:
        frame = sys._current_frames().get(int(tid))
    except Exception:  # pragma: no cover - 非法 tid
        frame = None
    if frame is None:
        return ["<holder-frame-unavailable>"]
    try:
        return [ln.rstrip("\n") for ln in traceback.format_stack(frame)[-limit:]]
    except Exception as _e:  # pragma: no cover
        return [f"<stack-dump-failed: {_e}>"]


def watchdog_scan(now: float | None = None) -> list[dict]:
    """单次扫描：对持有超阈值的 owner 打 ERROR + 持锁栈，返回本次上报记录（测试用）。"""
    hold_s = watchdog_hold_s()
    dedup_s = watchdog_dedup_s()
    if hold_s <= 0:  # 0/负数 = 看门狗关闭
        return []
    now = time.perf_counter() if now is None else float(now)
    hits: list[dict] = []
    for st in snapshot():
        held = now - float(st.get("since") or now)
        if held < hold_s:
            continue
        owner = str(st.get("owner") or "-")
        last = _WATCHDOG_REPORTED.get(owner)
        if last is not None and (now - last) < dedup_s:
            continue
        _WATCHDOG_REPORTED[owner] = now
        stack = _holder_stack(st.get("tid"))
        hit = {"owner": owner, "rid": st.get("rid") or "-", "held_s": round(held, 3),
               "depth": st.get("depth", 1), "tid": st.get("tid"), "stack": stack}
        hits.append(hit)
        if _WATCHDOG_HITS_METRIC is not None:
            try:
                _WATCHDOG_HITS_METRIC.labels(owner=owner).inc()
            except Exception as _me:  # pragma: no cover
                _dbg.debug("db_lock watchdog metric failed: %s", _me)
        try:
            _log().error(
                "db_lock watchdog: held=%.2fs owner=%s#%s depth=%s holder_stack=\n%s",
                held, owner, st.get("rid") or "-", st.get("depth", 1), "\n".join(stack),
                extra={"db_lock_held_s": round(held, 3), "db_lock_owner": owner,
                       "db_lock_holder_tid": st.get("tid")},
            )
        except Exception as _le:  # pragma: no cover
            _dbg.debug("db_lock watchdog log failed: %s", _le)
    if _WATCHDOG_REPORTED and dedup_s > 0:
        for key in [k for k, ts in _WATCHDOG_REPORTED.items() if (now - ts) >= dedup_s]:
            _WATCHDOG_REPORTED.pop(key, None)
    return hits


def _watchdog_loop() -> None:
    while not _WATCHDOG_STOP.is_set():
        period = watchdog_period_s()
        if period <= 0:
            return
        try:
            watchdog_scan()
        except Exception as _e:  # pragma: no cover - 看门狗自身绝不制造故障
            _dbg.debug("db_lock watchdog scan failed: %s", _e)
        _WATCHDOG_STOP.wait(period)


def watchdog_running() -> bool:
    t = _WATCHDOG_THREAD
    return bool(t is not None and t.is_alive())


def start_watchdog() -> bool:
    """启动持有看门狗（幂等；``DB_LOCK_WATCHDOG_S`` <= 0 时不启动）。"""
    global _WATCHDOG_THREAD
    if watchdog_period_s() <= 0 or watchdog_hold_s() <= 0:
        return False
    with _WATCHDOG_START_LOCK:
        if watchdog_running():
            return False
        _WATCHDOG_STOP.clear()
        t = threading.Thread(target=_watchdog_loop, name="db_lock_watchdog", daemon=True)
        _WATCHDOG_THREAD = t
        t.start()
        return True


def stop_watchdog(timeout: float = 2.0) -> None:
    """停止看门狗（测试/优雅退出用）。"""
    global _WATCHDOG_THREAD
    _WATCHDOG_STOP.set()
    t = _WATCHDOG_THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    with _WATCHDOG_START_LOCK:
        if _WATCHDOG_THREAD is t:
            _WATCHDOG_THREAD = None
