"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes, templates, tasks, runtime, feishu_onboard, task_logs, knowledge, health


bot_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_manager
    from src.config import log

    try:
        from src.im.feishu_async import BotManager
        from src.im.handler_async import process_chat_async
        from src.im.vod_handler_async import process_vod_async

        # lease 模式：默认开启（方式 A），BOT_LEASE_DISABLED=true 退回旧模式
        from src.config import BOT_LEASE_DISABLED
        lease_store = None
        if not BOT_LEASE_DISABLED:
            from src.im.bot_lease import BotLeaseStore, generate_instance_id
            instance_id = generate_instance_id()
            lease_store = BotLeaseStore(instance_id=instance_id)
            log.info(f"IM WS: lease enabled, instance_id={instance_id}")

        bot_manager = BotManager(max_concurrent_per_bot=10, lease_store=lease_store)
        await bot_manager.start()

        async def _on_message(ctx, parsed):
            if ctx.bot_type == "vod_bot":
                await process_vod_async(ctx, bot_manager.api, parsed)
            else:
                await process_chat_async(ctx, bot_manager.api, parsed)

        bot_manager.set_message_handler(_on_message)
        await bot_manager.load_from_db()

        hc = bot_manager.health_check()
        log.info(f"IM WS: {hc['total_bots']} bot(s), {hc['connected']} connected")
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    # lease sweep 循环（问题 J：变量名 _lease_sweep_task 区分）
    _lease_sweep_task = None
    if bot_manager and bot_manager._lease_store:
        _lease_sweep_task = asyncio.create_task(bot_manager.lease_sweep_loop())
        log.info("Lease sweep loop started")

    # sweeper：Issue done 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）
    _sweep_task = None
    try:
        from src.runtime.cleanup import sweep_loop
        _sweep_task = asyncio.create_task(sweep_loop())
        log.info("Sweeper loop started")
    except Exception as e:
        log.warning(f"Sweeper loop start failed: {e}")

    # 本机直连引擎周期重探（方案 C，local-direct-health-probe-design.md）：每 10 分钟真实探测 --version，
    # 刷新 agent_runtimes.healthy，防二进制变更/被删后 healthy 过期
    _probe_task = None
    try:
        async def _local_probe_loop():
            import time as _t
            from src.runtime.registry import discover_runtimes
            while True:
                def _run():
                    from src.runtime.local_probe import probe_local_engines
                    from src.user import db as user_db
                    probed = [r for r in probe_local_engines(discover_runtimes()) if r.get("installed")]
                    if probed:
                        user_db.save_agent_runtimes(probed)
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

    yield

    log.info("IM WS: stopping...")
    # 问题 H：先 cancel lease_sweep_task（不再抢新锁）→ 再 stop bot_manager（停 bot + 释放 lease）
    if _lease_sweep_task:
        _lease_sweep_task.cancel()
        try:
            await asyncio.wait_for(_lease_sweep_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if bot_manager:
        await bot_manager.stop()
    if _sweep_task:
        _sweep_task.cancel()
    if _probe_task:
        _probe_task.cancel()
    # 终止运行中的 agent 子进程（opencode），使 executor 线程尽快结束，
    # 避免 uvicorn 优雅停机等待超时被 systemd SIGKILL（grape-main timeout 重启根因，2026-08-18）
    try:
        from src.agent.executor import terminate_active_agents
        terminate_active_agents(grace_seconds=5)
    except Exception as e:
        log.warning(f"terminate_active_agents failed: {e}")
    log.info("IM WS stopped")


app = FastAPI(title="Agentic DevOps API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可观察性：HTTP 请求指标中间件（仅 METRICS_ADDR 启用时挂载）
from src.observability import metrics_middleware, start_metrics_server, metrics_endpoint, metrics_enabled
metrics_middleware(app)
if metrics_enabled():
    start_metrics_server()

# 可观察性：request_id + HTTP 请求日志合并中间件（参考 multica middleware/request_logger.go）
# request_id：注入 X-Request-ID + ContextVar，贯穿日志 + 响应头
# 请求日志：结构化记录 method/path/status/duration/request_id，按状态码分级
import uuid as _uuid
import time as _time
from src.config import request_id_var, repo_var, user_var, log as _log


# webhook 路径中的 token 需要 redact（参考 multica redactWebhookPath）
_WEBHOOK_PREFIX = "/webhook/"


def _redact_path(path: str) -> str:
    """webhook 路径中的 token 段替换为 [redacted]。"""
    if not path.startswith(_WEBHOOK_PREFIX):
        return path
    rest = path[len(_WEBHOOK_PREFIX):]
    if "/" in rest:
        return _WEBHOOK_PREFIX + "[redacted]" + rest[rest.index("/"):]
    return _WEBHOOK_PREFIX + "[redacted]"


# 健康探针路径不记请求日志（参考 multica isHealthProbePath）
_LOG_SKIP_PATHS = frozenset({"/health", "/healthz", "/readyz", "/metrics"})


@app.middleware("http")
async def request_id_and_log_middleware(request: Request, call_next):
    """request_id 注入 + HTTP 请求日志（合并为一个中间件，确保 ContextVar 贯穿日志）。

    按状态码分级：5xx → ERROR, 4xx → WARN, 2xx/3xx → INFO。
    健康探针路径跳过请求日志（但 request_id 仍注入）。
    """
    rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:12]
    request.scope["request_id"] = rid
    token_rid = request_id_var.set(rid)

    # 从请求路径提取 repo_key（如 /api/dashboard/requiredsa/hello-flask → requiredsa/hello-flask）
    path = request.url.path
    _repo = ""
    # 路径模式：/api/dashboard/{owner}/{repo} 或 /api/pipeline/{owner}/{repo} 等
    _path_parts = path.strip("/").split("/")
    if len(_path_parts) >= 4 and _path_parts[0] == "api":
        _candidate = f"{_path_parts[2]}/{_path_parts[3]}"
        # 排除已知的非仓库路径段
        _non_repo_segments = {"ops", "settings", "health", "healthz", "readyz",
                              "metrics", "auth", "skills", "repos", "scenes",
                              "search", "webhook", "runtime", "task-logs", "static",
                              "dashboard", "agents", "events", "chat", "pipeline",
                              "tasks", "knowledge"}
        if ("/" in _candidate
                and _path_parts[2] not in _non_repo_segments
                and _path_parts[3] not in _non_repo_segments
                and "." not in _path_parts[2]):
            _repo = f"gitcode:{_candidate}"
    # query param 中的 repo（如 ?repo=requiredsa/hello-flask）
    if not _repo:
        _qp = request.query_params.get("repo", "")
        if _qp:
            _repo = f"gitcode:{_qp}" if not _qp.startswith("gitcode:") else _qp
    token_repo = repo_var.set(_repo)

    # 从 Authorization header 提取 user_id
    _uid = ""
    _auth = request.headers.get("Authorization", "")
    if _auth.startswith("Bearer "):
        try:
            from src.user.db import validate_token
            _uid_val = validate_token(_auth[7:])
            if _uid_val:
                _uid = str(_uid_val)
        except Exception:
            pass
    token_uid = user_var.set(_uid)

    skip_log = path in _LOG_SKIP_PATHS
    start = _time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        raise
    finally:
        if not skip_log:
            duration = (_time.time() - start) * 1000
            redacted_path = _redact_path(path)
            # 结构化字段通过 extra 注入，作为独立 JSON 字段输出（非 message 文本）
            extra = {
                "method": request.method,
                "path": redacted_path,
                "status": status,
                "duration_ms": round(duration),
            }
            msg = f"{request.method} {redacted_path} status={status} duration={duration:.0f}ms"
            if status >= 500:
                _log.error(msg, extra=extra)
            elif status >= 400:
                _log.warning(msg, extra=extra)
            else:
                _log.info(msg, extra=extra)
        request_id_var.reset(token_rid)
        repo_var.reset(token_repo)
        user_var.reset(token_uid)

app.include_router(auth.router)
app.include_router(repos.router)
app.include_router(dashboard.router)
app.include_router(agents.router)
app.include_router(events.router)
app.include_router(chat.router)
app.include_router(settings.router)
app.include_router(skills.router)
app.include_router(search.router)
app.include_router(pipeline.router)
app.include_router(webhook.router)
app.include_router(scenes.router)
app.include_router(templates.router)
app.include_router(tasks.router)
app.include_router(runtime.router)
app.include_router(feishu_onboard.router)
app.include_router(task_logs.router)
app.include_router(knowledge.router)
app.include_router(health.router)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from fastapi.responses import FileResponse
    @app.get("/")
    async def serve_spa():
        from fastapi.responses import FileResponse
        resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

from src.api.db_setup import init_database
init_database()

from src.config import _load_accounts
_load_accounts()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from src.config import log
    log.warning(f"API error: {exc}")
    rid = request.scope.get("request_id", "-")
    return JSONResponse(status_code=500, content={"code": 500, "message": str(exc), "request_id": rid})


# ===== 三层健康检查（参考 multica health.go）=====

import os as _os_mod
from datetime import datetime as _dt, timezone as _tz

_PROCESS_STARTED_AT = _dt.now(_tz.utc).isoformat()


@app.get("/health")
async def health():
    """Liveness：进程存活，不检查 DB。

    K8s livenessProbe 用此端点。DB 挂了不应导致 pod 重启，只标记 NotReady。
    """
    return {
        "status": "ok",
        "version": "2.0.0",
        "pid": _os_mod.getpid(),
        "started_at": _PROCESS_STARTED_AT,
    }


# /healthz 3s TTL 缓存（参考 multica readinessCacheTTL）
_readiness_cache = {"result": None, "expires_at": 0}
_READINESS_CACHE_TTL = 3  # 秒


@app.get("/healthz")
@app.get("/readyz")
async def readiness():
    """Readiness：检查 DB 连通 + 表结构完整性，3s 缓存。

    K8s readinessProbe / startupProbe 用此端点。
    """
    import time as _t
    now = _t.time()
    cached = _readiness_cache["result"]
    if cached and now < _readiness_cache["expires_at"]:
        return cached

    from src.session_manager import db, db_lock
    checks = {"db": "ok", "tables": "ok"}
    status = "ok"
    http_status = 200
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1")
            c.fetchone()
    except Exception as e:
        checks["db"] = f"error: {e}"
        checks["tables"] = "unknown"
        status = "not_ready"
        http_status = 503
    else:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM agent_tasks")
                c.fetchone()
        except Exception:
            checks["tables"] = "error: agent_tasks missing"
            status = "not_ready"
            http_status = 503
    resp = JSONResponse(
        status_code=http_status,
        content={"status": status, "checks": checks},
    )
    _readiness_cache["result"] = resp
    _readiness_cache["expires_at"] = now + _READINESS_CACHE_TTL
    return resp


@app.get("/health/realtime")
async def health_realtime():
    """运维诊断：暴露 IM bot 状态、任务队列深度、runtime 在线数。

    参考 multica /health/realtime（health_realtime.go）。
    生产环境建议通过反向代理限制访问（仅 loopback 或 REALTIME_METRICS_TOKEN）。
    """
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    import time as _time, os as _os

    now_ts = _time.time()
    result = {
        "status": "ok",
        "version": "2.0.0",
        "pid": _os.getpid(),
    }

    # IM bot 状态
    if bot_manager:
        result["im"] = bot_manager.health_check()

    # 任务队列实时快照
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, COUNT(*) FROM agent_tasks GROUP BY status")
            result["tasks"] = {r[0]: r[1] for r in c.fetchall()}

            c.execute("SELECT COUNT(*) FROM agent_tasks WHERE status IN ('dispatched','running')")
            result["in_progress"] = c.fetchone()[0]

            c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes")
            online = 0
            total = 0
            for r in c.fetchall():
                total += 1
                h = derive_runtime_health(r[0] or "offline", r[1], now_ts)
                if h == "online":
                    online += 1
            result["runtimes"] = {"total": total, "online": online}
    except Exception as e:
        result["db_error"] = str(e)

    return result


@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点（参考 multica metrics/server.go NewHandler）。

    scrape 时触发 MySQL 指标采集 + BusinessSampler 采样。
    """
    from src.observability import on_metrics_scrape
    on_metrics_scrape()
    return metrics_endpoint()