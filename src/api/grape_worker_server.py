"""Worker 进程（grape_worker_server :38082）——Worker 执行态。

Issue #699 Phase 4 四进程拆分：注册 Worker API 路由（runtime 全量 POST/GET 协议端点、
tasks 通知/完成/当前任务自省、chat 执行端点），启动 Worker 进程定时任务
（local_claimer / autopilot scheduler / health_refresh / local_probe / reap_stale /
sweep_tasks_loop / table_maintenance agent_tasks 部分，设计 §5.3 / §5.5 / §10）。
"""
import sys, os

# Issue #455：主服务要求 Python >= 3.10。
from src.api.version_check import ensure_python_version
ensure_python_version()
# 进程角色默认值（2026-11-16 实弹）：表归属守卫按角色判定 owner，缺角色会把本域 DDL
# 整批当「别家域」跳过（平台四表 / meta 因此不建，PAT 关系写不进）。入口写死默认值，
# 自定义启动方式未设 env 时也不会静默丢表。
os.environ.setdefault("GRAPE_PROCESS_ROLE", "runtime")

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
    assert_role_database("worker")

    # GSB：注册本进程（服务名 runtime）+ 起 30s 心跳
    _gsb = getattr(app.state, "gsb", None)
    if _gsb is not None:
        await _gsb.startup()

    # Worker 进程定时任务（设计 §5.3）
    _worker_h = await server_tasks.start_worker_tasks(app)
    log.info("Grape Worker Server lifespan started")

    yield

    if _gsb is not None:
        await _gsb.shutdown()
    await server_tasks.stop_worker_tasks(app, _worker_h)
    log.info("Grape Worker Server lifespan stopped")


app = server_common.build_app(
    title="Grape Worker Server",
    version="2.2.0",
    lifespan=lifespan,
)

# Worker API 路由（设计 §5.3）：runtime 全量（POST 协议 + GET 轮询/安装/健康）
from src.domains.runtime.routes import runtime, tasks, chat
app.include_router(runtime.router)

# tasks：Agent 自报完成（POST /complete）、通知（POST /notify）、当前任务自省（GET /current）
app.include_router(tasks.router)

# chat 执行端点（POST 系：send / send/stream / submit / send/async 等，设计 §5.5）
server_common.include_selected(app, chat.router, methods={"POST"})

server_common.init_process_db()

# /internal/* 端点——供集群模式 RemoteProvider 调用（Phase 5）
server_common.attach_internal_routes(app, "runtime")

# GSB 服务注册表端点（/internal/gsb/*）
from src.kernel.gsb.router import init_gsb
init_gsb(app, "worker")

server_common.attach_health(app, service_name="worker-server", readiness=False)
