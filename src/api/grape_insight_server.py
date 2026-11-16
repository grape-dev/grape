"""Insight 进程（grape_insight_server :38083）——可观测性、看板、SLO、告警。

Issue #699 Phase 4 四进程拆分：原合并在 Mgmt 进程中的可观测性定时任务独立到本进程，
解决可观测性计算与用户请求争抢资源的核心痛点（设计 §5.4 / §10）。
注册数据域路由（dashboard / observability / search / latency / health），
启动 Insight 进程定时任务（obs_rollup / obs_sample / obs_alert+SLO / server_metrics /
install_master_logging / trace stamp / table_maintenance task_messages 部分）。
"""
import sys, os

# Issue #455：主服务要求 Python >= 3.10。
from src.api.version_check import ensure_python_version
ensure_python_version()
# 进程角色默认值（2026-11-16 实弹）：表归属守卫按角色判定 owner，缺角色会把本域 DDL
# 整批当「别家域」跳过（平台四表 / meta 因此不建，PAT 关系写不进）。入口写死默认值，
# 自定义启动方式未设 env 时也不会静默丢表。
os.environ.setdefault("GRAPE_PROCESS_ROLE", "insight")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import server_common, server_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.kernel.config import log

    # 启动自检：连错库就拒绝启动 —— 带着错库对外返回 200 时，症状是
    # 「凭据全 401、引擎全失联」，现场只能翻 /proc 才看得出来。
    from src.kernel.db_registry import assert_role_database
    assert_role_database("insight")

    # GSB：注册本进程 + 起 30s 心跳（失败只告警，不影响 insight 启动）
    _gsb = getattr(app.state, "gsb", None)
    if _gsb is not None:
        await _gsb.startup()

    # Insight 进程定时任务（设计 §5.4）
    _insight_h = await server_tasks.start_insight_tasks(app)
    log.info("Grape Insight Server lifespan started")

    yield

    if _gsb is not None:
        await _gsb.shutdown()
    await server_tasks.stop_insight_tasks(app, _insight_h)
    log.info("Grape Insight Server lifespan stopped")


app = server_common.build_app(
    title="Grape Insight Server",
    version="2.2.0",
    lifespan=lifespan,
)

# 数据域路由（设计 §5.4）
from src.domains.insight.routes import dashboard, observability, search, latency, health
app.include_router(dashboard.router)
app.include_router(observability.router)
app.include_router(search.router)
app.include_router(latency.router)
app.include_router(health.router)

server_common.init_process_db()

# /internal/* 端点——供集群模式 RemoteProvider 调用（Phase 5）
server_common.attach_internal_routes(app, "insight")

# GSB 服务注册表端点（/internal/gsb/*）
from src.kernel.gsb.router import init_gsb
init_gsb(app, "insight")

server_common.attach_health(app, service_name="insight-server", readiness=False)