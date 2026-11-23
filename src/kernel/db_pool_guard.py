"""连接池/线程池对齐 + 池饱和巡检（2026-11-23 mgmt 进程全站僵死事故治本）。

事故形状（现场证据：py-spy + api_latency_log + MySQL processlist）：

1. ``DB_POOL_MAX_CONNECTIONS=16``（连接池上限）比 anyio 工作线程上限（40）小。
2. 每个进程的池是**独立的**，但慢语句（需求列表动辄数十秒）会长时间占住连接。
3. 池满后 dbutils ``blocking=True`` 让所有后到线程无限期挂起 —— 包括跑事件
   循环的主线程（后台 outbox 也要借连接）。主线程一卡，uvicorn 连静态文件都
   不再响应：进程活着、日志除了巡检线程外全静默，表现成「整台机器挂了」。
4. 出事后没有任何水位/等待时间日志，只能靠 py-spy 现场抓栈才能看出是池饥饿。

本模块提供三件事（都可用环境变量关掉）：

- ``align_threads_and_pool()``：把 anyio 线程上限收敛到 ≤ 连接池上限，
  从算术上消灭「线程比连接多」的饥饿；
- 请求收尾中间件：每个请求结束后归还本线程借的池连接（兜底防粘）；
- 巡检线程：池连续饱和时打 ERROR + 用 faulthandler dump 全线程栈，
  下次同类事故能在日志里直接看到「谁占着连接」。

环境变量：
- ``DB_POOL_MAX_CONNECTIONS`` 池上限（默认 32，需与 DB_POOL_MAX_CONNECTIONS
  在实际部署的 EnvironmentFile 里对齐）
- ``DB_THREAD_LIMIT`` 期望的 anyio 线程上限（默认 24，最终取 min(它, 池上限)）
- ``DB_POOL_WATCHDOG`` 1/0 是否启用巡检（默认 1）
- ``DB_POOL_WATCH_INTERVAL`` 巡检间隔秒（默认 30）
"""
import faulthandler
import os
import sys
import threading
import time
import traceback

from src.kernel.config import log

_POOL_MAX_ENV = "DB_POOL_MAX_CONNECTIONS"
_THREAD_ENV = "DB_THREAD_LIMIT"
_DEFAULT_POOL_MAX = 32
_DEFAULT_THREAD_LIMIT = 24
_WATCHDOG_STARTED = False


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _apply_anyio_limiter(limit: int) -> bool:
    """在当前事件循环里设置 anyio 默认线程上限（必须在 async 上下文调用）。"""
    try:
        import anyio.to_thread as _anyio_thread
        _anyio_thread.current_default_thread_limiter().total_tokens = limit
        return True
    except Exception:  # noqa: BLE001 - 老 anyio / 非 anyio 环境：交给 lifespan 再试
        return False


def align_threads_and_pool() -> dict:
    """把 anyio 工作线程上限收敛到不超过连接池上限，返回生效值。

    注意：anyio 4 只允许在运行中的事件循环里取 default limiter，模块导入期
    （build_app 时）通常拿不到 —— 返回 ``anyio_applied=False`` 属正常，
    ``install_guard`` 会在 lifespan 启动时再应用一次。
    """
    pool_max = max(4, _env_int(_POOL_MAX_ENV, _DEFAULT_POOL_MAX))
    want_threads = max(4, _env_int(_THREAD_ENV, _DEFAULT_THREAD_LIMIT))
    effective = min(want_threads, pool_max)
    ok = _apply_anyio_limiter(effective)
    msg = (f"db-pool-guard: anyio 线程上限={effective}"
           f"（期望 {want_threads}{'，已收敛到池上限' if effective != want_threads else ''}）"
           f" 连接池上限={pool_max}"
           f"{'' if ok else '（事件循环未就绪，lifespan 启动时再生效）'}")
    if effective < want_threads:
        log.warning(msg + " —— 线程数超过池上限会饿死事件循环，已按池上限收敛")
    else:
        log.info(msg)
    return {"thread_limit": effective, "pool_max": pool_max, "anyio_applied": ok}


def _install_lifespan_align(app) -> None:
    """在 FastAPI lifespan 启动处再应用一次线程上限（此时事件循环已存在）。"""
    from contextlib import asynccontextmanager

    orig = app.router.lifespan_context
    limit = min(max(4, _env_int(_THREAD_ENV, _DEFAULT_THREAD_LIMIT)),
                max(4, _env_int(_POOL_MAX_ENV, _DEFAULT_POOL_MAX)))

    @asynccontextmanager
    async def _lifespan(app_):
        if _apply_anyio_limiter(limit):
            log.info(f"db-pool-guard: lifespan 应用 anyio 线程上限={limit}")
        async with orig(app_) as state:
            yield state

    app.router.lifespan_context = _lifespan


class _ReleasePooledConnMiddleware:
    """纯 ASGI 中间件：请求收尾把本线程借的池连接还回去（幂等、无额外开销）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            if scope.get("type") == "http":
                try:
                    from src.kernel.mysql_compat import release_all_thread_connections
                    released = release_all_thread_connections()
                    if released:
                        # 正常路径上只读语句已自动 commit 归还，这里有值说明
                        # 有路径把连接粘住了 —— 记一笔便于事后定位。
                        log.debug(f"db-pool-guard: 请求收尾归还 {released} 条被粘住的池连接")
                except Exception:  # noqa: BLE001
                    pass


def _watch_loop() -> None:
    from src.kernel.mysql_compat import pool_stats_all

    interval = max(5, _env_int("DB_POOL_WATCH_INTERVAL", 30))
    streak = 0
    last_dump = 0.0
    while True:
        time.sleep(interval)
        try:
            stats = pool_stats_all()
        except Exception:  # noqa: BLE001
            continue
        worst = 0.0
        worst_db = ""
        for st in stats:
            mx = st.get("max") or 0
            if mx <= 0:
                continue
            ratio = (st.get("in_use") or 0) / mx
            if ratio > worst:
                worst, worst_db = ratio, st.get("database") or "?"
        if worst >= 0.9:
            streak += 1
            log.warning(
                f"db-pool-guard: 连接池饱和 {worst:.0%}（{worst_db}，"
                f"连续 {streak} 次采样）stats={stats}")
            if streak >= 2 and time.time() - last_dump > 300:
                last_dump = time.time()
                log.error(
                    f"db-pool-guard: 连接池持续饱和 {worst:.0%}（{worst_db}），"
                    f"dump 全线程栈用于定位谁占着连接")
                try:
                    threads = {t.ident: t.name for t in threading.enumerate()}
                    log.error(f"db-pool-guard: 线程清单 {threads}")
                    faulthandler.dump_traceback(file=sys.stderr)
                except Exception:  # noqa: BLE001
                    log.error("db-pool-guard: " + traceback.format_exc())
        else:
            streak = 0


def start_watchdog() -> bool:
    """启动池饱和巡检线程（幂等）。"""
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED:
        return False
    if (os.environ.get("DB_POOL_WATCHDOG", "1").lower() not in ("1", "true", "yes")):
        return False
    t = threading.Thread(target=_watch_loop, name="db-pool-watchdog", daemon=True)
    t.start()
    _WATCHDOG_STARTED = True
    return True


def install_guard(app) -> dict:
    """在 FastAPI app 上装载：线程/池对齐 + 收尾归还 + 饱和巡检。"""
    align = align_threads_and_pool()
    try:
        app.add_middleware(_ReleasePooledConnMiddleware)
    except Exception as e:  # noqa: BLE001
        log.warning(f"db-pool-guard: 中间件装载失败（{e}）")
    try:
        _install_lifespan_align(app)
    except Exception as e:  # noqa: BLE001
        log.warning(f"db-pool-guard: lifespan 对齐装载失败（{e}）")
    align["watchdog"] = start_watchdog()
    return align
