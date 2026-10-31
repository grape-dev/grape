"""Grape 事件处理服务 — Webhook 接收与事件入队（B 进程，Issue #596 P1）。

三进程拆分（docs/weekly-09/grape-three-process-split-design.md §4.1.2）：
- A（grape_server.py，:38080）：用户面工作台；
- B（本文件，:38081）：Webhook 接收、事件归一化、路由判定、任务入队（不直接执行）；
- C（grape_worker_server.py，:38082）：任务认领、Agent 执行。

B 进程职责：
1. 加载 `webhook.py`（/webhook/gitcode、/webhook/github、/api/a2a、/api/a2a/status）；
2. 加载 `events.py` 的操作端点（POST /{seq}/terminate、POST /{seq}/redispatch、
   POST /{seq}/a2a-redo、POST /{seq}/chat* 等触发事件处理层的操作端点）——
   通过 events.py 提供的 `ops_router`（只含操作端点；GET 详情端点归 A 的
   events_router 视图）。events.py 同时导出完整 `router`（单进程模式用），
   保证 `main.py` 兼容不变；
3. B 归属定时任务（§6 分配表）：sweep_events_loop（15min，P2 拆分后）、
   start_bot_token_health（5min）；
4. 单 worker（--workers 1）：event_pipeline 自身用线程池并行；
   P0 迁移后 `_DIRECT_FUTURES` 已不在本进程，无多 worker 内存共享问题。

event_pipeline.py 作为共享库被 B 进程 import，但 B 只调用 run_pipeline
（入队逻辑），不调用 _execute_agent（P0 已迁移至 direct_executor，归 C）。

启动：
    uvicorn src.api.grape_event_server:app --host 0.0.0.0 --port 38081 --workers 1
"""
import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from src.api.routes.webhook import router as webhook_router
# events.py 的操作端点（terminate/redispatch/a2a-redo/chat 触发类）——B 加载
from src.api.routes.events import ops_router as events_ops_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.config import log

    # 数据库初始化 + 账号加载（B 独立进程需自建，不依赖 A 的启动顺序）
    try:
        from src.api.db_setup import init_database
        init_database()
    except Exception as e:
        log.warning(f"Event server init_database failed: {e}")
    try:
        from src.config import _load_accounts
        _load_accounts()
    except Exception as e:
        log.warning(f"Event server _load_accounts failed: {e}")

    # ===== B 归属定时任务（设计文档 §6 分配表）=====

    # P2 拆分后的事件回收循环（15min）：僵尸事件回收 + IM 实例清理 + workdir/目录清理
    _sweep_events_task = None
    try:
        from src.runtime.cleanup import sweep_events_loop
        _sweep_events_task = asyncio.create_task(sweep_events_loop())
        log.info("Event sweeper loop started (15min, B process)")
    except Exception as e:
        log.warning(f"Event sweeper loop start failed: {e}")

    # Issue #345: 全局 bot token 健康探活——每 5 分钟，分布式锁保证单实例执行
    try:
        from src.runtime.bot_token_health import start_bot_token_health
        start_bot_token_health()
    except Exception as e:
        log.warning(f"Bot token health check start failed: {e}")

    yield

    # ===== 优雅关闭 =====
    log.info("B process (event server) stopping...")
    if _sweep_events_task:
        _sweep_events_task.cancel()
        try:
            await asyncio.wait_for(_sweep_events_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    log.info("B process (event server) stopped")


app = FastAPI(
    title="Grape Event Server",
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

# ===== B 路由注册：webhook + A2A + events 操作端点 =====
app.include_router(webhook_router)
app.include_router(events_ops_router)

# ===== 共享中间件 / 异常处理 / 健康检查（Issue #596 P1 抽出复用）=====
from src.api.middleware import install_request_middleware
install_request_middleware(app)

from src.api.exception_handlers import install_exception_handlers
install_exception_handlers(app)

from src.api.health_endpoints import install_health_endpoints
install_health_endpoints(app, service_name="event-server")


if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("GRAPE_PORT", "38081"))
    uvicorn.run("src.api.grape_event_server:app", host="0.0.0.0", port=_port, workers=1)
