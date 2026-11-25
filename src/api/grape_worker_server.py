"""Worker 进程（grape_worker_server :38083）——Worker 执行态。

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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api import drain_state, server_common, server_tasks
# Issue #2784：契约版本号单一来源（勿再写死字面量）
from src.api.contract_versioning import CONTRACT_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.kernel.config import log

    # Issue #3969：清理上次停机残留的排空标记，避免重启后 /readyz 永久 not_ready。
    drain_state.clear_drain()

    # Issue #4203：启动自检——打印自身 cgroup 生效内存限额（无限额打 WARNING）。
    # 004 上 OOM 后只能靠 dmesg 反查受害者归属；生效值进日志，值班一眼可查。
    try:
        from src.domains.insight.observability import log_effective_memory_limit
        log_effective_memory_limit()
    except Exception as _mem_selfcheck_err:
        log.warning(f"[memory-limit] selfcheck skipped: {_mem_selfcheck_err}")

    # 启动自检：连错库就拒绝启动 —— 带着错库对外返回 200 时，症状是
    # 「凭据全 401、引擎全失联」，现场只能翻 /proc 才看得出来。
    from src.kernel.db_registry import assert_role_database, warn_domain_table_residency
    assert_role_database("worker")
    # Issue #3732：域表 ↔ DSN 归属抽样自检（错配只告警不阻断，单库 no-op）
    warn_domain_table_residency("worker")

    # Issue #3749：worker trace 中继启动自检（本进程挂载 runtime 全量路由，含 POST /api/runtime/traces）。OTLP_RELAY_TARGET 为空时
    # 启动期 WARN 一次并说明 503 语义，配置存在则打印生效端点；自检失败不阻断启动。
    try:
        from src.domains.runtime.routes.runtime import trace_relay_selfcheck
        trace_relay_selfcheck()
    except Exception as _relay_selfcheck_err:
        log.warning(f"[trace-relay] startup selfcheck skipped: {_relay_selfcheck_err}")


    # kernel 端口绑定：GSB 注册/心跳经 pipeline 端口调契约域原语，未绑定会抛
    # PortNotBound（现象：GSB register failed: service=xxx err=PortNotBound）。
    # mgmt 在 db_setup.init_database() 里绑；本 server 不走那条路径，必须自己绑，
    # 且要在 GSB startup 之前。
    try:
        from src.api import contracts_wiring as _cw
        _cw.bind_all()
    except Exception as _bw_e:
        log.warning(f"kernel ports bind failed（GSB 注册可能失败）: {_bw_e}")


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
    version=CONTRACT_VERSION,
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

# Issue #3969：启用 readiness（/healthz+/readyz），并挂停机摘流端点。
# systemd ExecStop 先 POST /internal/drain（仅回环；nginx 已 deny /internal/），
# 标记落盘后 /readyz 转 503，再等待排空窗口，最后才 SIGTERM。
@app.post("/internal/drain")
async def internal_drain(request: Request):
    # Issue #3969 复审 R7：只认 TCP 对端（request.client.host），不读 XFF/X-Real-IP。
    # 否则直连 0.0.0.0:38082 伪造 `X-Forwarded-For: 127.0.0.1` 即可摘流并持久 503。
    if not server_common.is_direct_loopback(request):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    drain_state.begin_drain()
    return {"status": "draining", "drain_seconds": drain_state.drain_seconds()}


server_common.attach_health(
    app, service_name="worker-server", readiness=True,
    drain_file=drain_state.drain_file_path() if drain_state.is_configured() else None,
)
