"""Grape Worker 执行态服务 — 任务认领与 Agent 执行（C 进程，Issue #596 P1）。

三进程拆分（docs/weekly-09/grape-three-process-split-design.md §4.1.3）：
- A（grape_server.py，:38080）：用户面工作台；
- B（grape_event_server.py，:38081）：Webhook 接收与事件入队；
- C（本文件，:38082）：任务认领、Agent 执行（direct + remote）、心跳、
  结果回传、引擎健康。

C 进程职责：
1. 加载 `runtime.py` 的 Worker 协议端点（POST register/heartbeat/claim/
   start/progress/messages/usage/complete/fail/requeue/cancel/recover-orphans/
   skills-report/refresh-skills + worker 数据面 GET tags/env/state/install*）；
2. 加载 `tasks.py`（/api/tasks notify/complete/current——Agent 自报完成与自省）；
3. C 归属定时任务（§6 分配表）：local_claimer（3s）/ scheduler autopilot（60s）/
   health_refresh（5min）/ local_probe（10min）/ reap_stale（60s）/
   sweep_tasks_loop（15min，P2 拆分后）/ table_maintenance agent_tasks 部分（6h，
   锁 key `table_maintenance_tasks`，与 A 的 messages 锁独立并行）；
4. 单 worker + 内部 `_DIRECT_POOL` 线程池（RUNTIME_MAX_CONCURRENT_TASKS=20）
   ——P0 迁移后 direct 执行在本进程完成。

启动：
    GRAPE_LOCAL_CLAIMER=1 uvicorn src.api.grape_worker_server:app \
        --host 0.0.0.0 --port 38082 --workers 1
"""
import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# runtime.py：Worker 协议端点（POST + worker 数据面 GET）——C 加载
from src.api.routes.runtime import router as runtime_router
# tasks.py：Agent 自报完成 / 通知 / 当前任务自省——C 加载
from src.api.routes.tasks import router as tasks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.config import log

    # 数据库初始化 + 账号加载（C 独立进程需自建，不依赖 A 的启动顺序）
    try:
        from src.api.db_setup import init_database
        init_database()
    except Exception as e:
        log.warning(f"Worker server init_database failed: {e}")
    try:
        from src.config import _load_accounts
        _load_accounts()
    except Exception as e:
        log.warning(f"Worker server _load_accounts failed: {e}")

    # ===== C 归属定时任务（设计文档 §6 分配表）=====

    # Autopilot 定时调度器（60s，分布式锁 autopilot_scheduler）
    try:
        from src.runtime.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        log.warning(f"Autopilot scheduler start failed: {e}")

    # 本机 direct 引擎一等认领器（3s；GRAPE_LOCAL_CLAIMER=0 可关闭）
    try:
        from src.runtime.local_claimer import start_local_claimer
        start_local_claimer()
    except Exception as e:
        log.warning(f"Local claimer start failed: {e}")

    # Issue #85：引擎健康度定时刷新（5min）
    try:
        from src.runtime.health_scorer import start_health_refresh_scheduler
        start_health_refresh_scheduler()
    except Exception as e:
        log.warning(f"Health refresh scheduler start failed: {e}")

    # 本机直连引擎周期重探（10min，刷新 agent_runtimes.healthy）
    _probe_task = None
    try:
        async def _local_probe_loop():
            from src.runtime.registry import discover_runtimes
            while True:
                def _run():
                    from src.runtime.local_probe import probe_local_engines
                    from src.runtime.agent import _scan_local_skills_with_detail
                    from src.user import db as user_db
                    import socket as _sock
                    _hostname = _sock.gethostname()
                    probed = [r for r in probe_local_engines(discover_runtimes()) if r.get("installed")]
                    if probed:
                        for r in probed:
                            _name = (r.get("name") or "").strip().lower()
                            r["local_skills_detail"] = _scan_local_skills_with_detail(_name)
                            r["local_skills"] = [s["name"] for s in r["local_skills_detail"]]
                        user_db.save_agent_runtimes(probed, host_name=_hostname)
                    return len(probed)
                try:
                    n = await asyncio.to_thread(_run)
                    log.info(f"Local probe loop: refreshed {n} local engine(s)")
                except Exception as e:
                    log.warning(f"Local probe failed: {e}")
                await asyncio.sleep(600)

        _probe_task = asyncio.create_task(_local_probe_loop())
        log.info("Local probe loop started (10min)")
    except Exception as e:
        log.warning(f"Local probe loop start failed: {e}")

    # 远端 runtime 僵尸清理（60s）
    _reap_task = None
    try:
        async def _reap_stale_loop():
            while True:
                await asyncio.sleep(60)
                try:
                    result = await asyncio.to_thread(_reap_stale_runtimes_sync)
                    if result.get("reaped"):
                        log.info(f"Reap loop: {result}")
                except Exception as e:
                    log.warning(f"Reap stale runtimes failed: {e}")

        def _reap_stale_runtimes_sync():
            from src.runtime.service import reap_stale_runtimes
            return reap_stale_runtimes()

        _reap_task = asyncio.create_task(_reap_stale_loop())
        log.info("Reap stale runtimes loop started (60s)")
    except Exception as e:
        log.warning(f"Reap stale runtimes loop start failed: {e}")

    # P2 拆分后的任务回收循环（15min）：悬空 agent_tasks 回收
    _sweep_tasks_task = None
    try:
        from src.runtime.cleanup import sweep_tasks_loop
        _sweep_tasks_task = asyncio.create_task(sweep_tasks_loop())
        log.info("Task sweeper loop started (15min, C process)")
    except Exception as e:
        log.warning(f"Task sweeper loop start failed: {e}")

    # table_maintenance（C 部分，§6.2 拆分）：只清 agent_tasks，
    # 锁 key 独立（与 A 侧 task_messages 清理的锁 key 互不影响，
    # A/C 可并行执行各自的清理）。
    try:
        from src.runtime.task_lock import run_with_distributed_lock

        async def _table_maintenance_tasks_worker():
            await asyncio.sleep(600)  # 10 min after startup (avoid migration race)
            while True:
                try:
                    async with run_with_distributed_lock("table_maintenance_tasks", ttl=3600):
                        from src.runtime.table_maintenance import (
                            cleanup_old_agent_tasks_fields,
                            delete_old_agent_tasks,
                        )
                        cleanup_old_agent_tasks_fields()
                        delete_old_agent_tasks()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("table maintenance (tasks) run failed")
                await asyncio.sleep(6 * 3600)  # every 6 hours

        asyncio.create_task(_table_maintenance_tasks_worker(), name="table-maintenance-tasks")
    except Exception as e:
        log.warning(f"Table maintenance (tasks) scheduler start failed: {e}")

    # psutil CPU 基准预热（local_probe / 指标采集用）
    try:
        import psutil as _ps_init
        _ps_init.cpu_percent(interval=None)
    except Exception:
        pass

    yield

    # ===== 优雅关闭 =====
    log.info("C process (worker server) stopping...")
    for _t in (_probe_task, _reap_task, _sweep_tasks_task):
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    # Issue #85 / #150：健康度刷新与 autopilot 调度器优雅关闭
    try:
        from src.runtime.health_scorer import stop_health_refresh_scheduler
        stop_health_refresh_scheduler()
    except Exception:
        pass
    try:
        from src.runtime.scheduler import stop_scheduler
        await stop_scheduler()
    except Exception:
        pass
    # 终止运行中的 agent 子进程，使 executor 线程尽快结束
    try:
        from src.agent.executor import terminate_active_agents
        terminate_active_agents(grace_seconds=5)
    except Exception as e:
        log.warning(f"terminate_active_agents failed: {e}")
    log.info("C process (worker server) stopped")


app = FastAPI(
    title="Grape Worker Server",
    version="2.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== C 路由注册：runtime Worker 协议 + tasks 自报 =====
app.include_router(runtime_router)
app.include_router(tasks_router)

# ===== 共享中间件 / 异常处理 / 健康检查（Issue #596 P1 抽出复用）=====
from src.api.middleware import install_request_middleware
install_request_middleware(app)

from src.api.exception_handlers import install_exception_handlers
install_exception_handlers(app)

from src.api.health_endpoints import install_health_endpoints
install_health_endpoints(app, service_name="worker-server")


if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("GRAPE_PORT", "38082"))
    uvicorn.run("src.api.grape_worker_server:app", host="0.0.0.0", port=_port, workers=1)
