"""Event 进程（grape_event_server :38081）——Git 仓库事件处理。

Issue #699 Phase 4 四进程拆分：Webhook 接收（/webhook/gitcode、/webhook/github）、
A2A 交接（/api/a2a、/api/a2a/status）、事件操作端点（terminate/redispatch）、
Issue 事件入队（POST /api/issues/{owner}/{repo}）；启动 Event 进程定时任务
（sweep_events_loop / bot_token_health，设计 §5.2 / §5.5 / §10）。
"""
import sys, os

# Issue #455：主服务要求 Python >= 3.10。
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import server_common, server_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.kernel.config import log

    # GSB：注册本进程 + 起 30s 心跳（失败只告警，不影响 event 启动）
    _gsb = getattr(app.state, "gsb", None)
    if _gsb is not None:
        await _gsb.startup()

    # Event 进程定时任务（设计 §5.2）
    _event_h = await server_tasks.start_event_tasks(app)
    log.info("Grape Event Server lifespan started")

    yield

    if _gsb is not None:
        await _gsb.shutdown()
    await server_tasks.stop_event_tasks(app, _event_h)
    log.info("Grape Event Server lifespan stopped")


app = server_common.build_app(
    title="Grape Event Server",
    version="2.2.0",
    lifespan=lifespan,
)

# Webhook + A2A 交接路由（无 prefix：/webhook/gitcode、/webhook/github、/api/a2a、/api/a2a/status）
from src.domains.runtime.routes import webhook, events, issues
app.include_router(webhook.router)

# 事件操作端点（POST terminate/redispatch，设计 §5.5：Event 独占操作）
server_common.include_selected(app, events.router, methods={"POST"}, paths=("/terminate", "/redispatch"))

# Issue 事件入队（POST /api/issues/{owner}/{repo}，设计 §5.5）
server_common.include_selected(app, issues.router, methods={"POST"}, paths=("/api/issues/{owner}/{repo}",))

server_common.init_process_db()

# GSB 服务注册表端点（/internal/gsb/*；event 进程同样参与服务发现）
from src.kernel.gsb.router import init_gsb
init_gsb(app, "event")

server_common.attach_health(app, service_name="event-server", readiness=False)
