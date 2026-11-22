"""Shared FastAPI server scaffolding for Issue #699 Phase 4 (four-process split).

Single source of truth for the pieces every Grape process needs:
- ``build_app``: FastAPI app + CORS + metrics middleware + request-id/log middleware
  + /api/ping + global exception handler
- ``attach_health``: liveness (/health) + readiness (/healthz,/readyz,/health/realtime)
- ``attach_frontend``: static mounts + SPA/static frontend (Mgmt process only)
- ``init_process_db``: idempotent DB bootstrap shared by every process
- ``include_selected``: mount routes from an existing APIRouter filtered by HTTP
  method / path substrings (used for cross-process route allocation, §5.5)

The legacy single-process entry (``src.api.main``) consumes this module too, so
single-process and four-process mode share one implementation.
"""
import os
import time as _time
import uuid as _uuid
import re as _re
from datetime import datetime as _dt, timezone as _tz

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute

from src.api.version_check import ensure_python_version
# Issue #3395：前端功能开关单一下发源（shell HTML 注入 window.__FEATURES__）
from src.api.frontend_flags import apply_feature_flags
# Issue #2784：契约版本号单一来源（健康探针与 /openapi.json 必须是同一个号）
from src.api.contract_versioning import CONTRACT_VERSION
from src.kernel.config import log
from src.kernel.config import (issue_var, request_id_var, repo_var, seq_var, span_id_var,
                               trace_id_var, user_var)

__all__ = [
    "ensure_python_version",
    "build_app",
    "attach_health",
    "attach_frontend",
    "init_process_db",
    "is_loopback",
    "include_selected",
    "check_and_sync_templates",
    "attach_internal_routes",
]


def is_loopback(request: Request) -> bool:
    """判断请求是否来自内网（127.0.0.1 / ::1）。"""
    client = request.client
    if client and client.host in ("127.0.0.1", "::1", "localhost"):
        return True
    # 信任反代 X-Forwarded-For（nginx 加 X-Real-IP）
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first in ("127.0.0.1", "::1"):
            return True
    xri = request.headers.get("x-real-ip", "")
    if xri in ("127.0.0.1", "::1"):
        return True
    return False


def build_app(
    title: str,
    version: str,
    lifespan=None,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    default_response_class=None,
):
    """Create the shared FastAPI app: CORS + metrics + request-id/log + ping + exception handler.

    Middleware registration order is preserved from the legacy main.py (CORS first,
    then metrics, then request-id/log as the outermost), so behavior is identical.
    """
    # systemd 的 PATH 只有 venv/bin 与系统目录，用户级安装的 agent CLI
    # （~/.local/bin/hermes、~/.local/npm-global/bin/opencode）会被漏掉，
    # 导致 web chat 报 "hermes ACP CLI not found in PATH" 而无回复。
    from src.kernel.cli_paths import ensure_user_bin_dirs

    ensure_user_bin_dirs()

    _kw = dict(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    # 增量（Issue #684）：调用方可注入统一 JSON 序列化出口（控制字符/孤立代理项转义）。
    # 缺省 None = 保持基线 FastAPI 默认行为，不影响其它进程入口。
    if default_response_class is not None:
        _kw["default_response_class"] = default_response_class
    app = FastAPI(**_kw)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 可观察性：HTTP 请求指标中间件（仅 METRICS_ADDR 启用时挂载）
    from src.domains.insight.observability import metrics_middleware, metrics_enabled, start_metrics_server
    metrics_middleware(app)
    if metrics_enabled():
        start_metrics_server()

    _attach_request_log(app)

    # 可选 tracing，两条路径互斥、新路径优先：
    #  - 新路径 OTEL_EXPORTER_OTLP_ENDPOINT → src/kernel/otel.py：FastAPI + requests
    #    + db.query + task.* 全套 span（service.name 认 GRAPE_TRACE_SERVICE）；
    #  - 旧路径 GRAPE_OTLP_ENDPOINT → insight/observability/tracing.py：只有 FastAPI
    #    + requests，没有 DB/任务 span（保留兼容，别的环境仍在用）。
    try:
        from src.kernel.otel import otel_enabled as _kernel_otel_enabled
        from src.kernel.otel import instrument_fastapi as _kernel_instrument
        _role = title.lower().split(" ")[0]
        if _kernel_otel_enabled():
            _kernel_instrument(app, f"grape-{_role}")
        else:
            from src.domains.insight.observability.tracing import setup_tracing, instrument_fastapi_app
            setup_tracing(service_default=f"grape-{_role}")
            instrument_fastapi_app(app)
    except Exception as e:
        log.warning(f"tracing setup skipped: {e}")

    @app.get("/api/ping", include_in_schema=False)
    async def api_ping():
        """前端会话保活 / Failed to fetch 恢复预检的轻量探针（无鉴权、不查 DB、浏览器可达）。"""
        return {"ok": True, "ts": _dt.now(_tz.utc).isoformat()}

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # Issue #380/#395: 异常原文（含 DB/SQL 错误细节）不回显给客户端——原文仅落服务端日志，
        # 响应体返回通用文案 + request_id（便于关联排障）
        log.warning(f"API error ({request.url.path}): {exc}")
        rid = request.scope.get("request_id", "-")
        return JSONResponse(status_code=500, content={"code": 500, "message": "服务器内部错误，请稍后重试", "request_id": rid})

    return app


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
# Worker 轮询路径跳过请求日志（13 台 worker × 3s × 3 请求 = ~24 行/秒, 膨胀日志）
_LOG_SKIP_PREFIXES = ("/api/runtime/runtimes/", "/api/runtime/heartbeat", "/api/runtime/tasks/", "/api/ping")


_ISSUE_PATH_RE = _re.compile(r"^/api/issues/([^/]+)")
_EVENT_PATH_RE = _re.compile(r"^/api/events/(\d+)")


def _normalize_issue(value: str) -> str:
    """把 25 / #25 统一成 #25；非数字（如内部 id）原样保留。"""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("#"):
        return v
    if v.isdigit():
        return f"#{v}"
    return v


def _issue_seq_from_request(path: str, params) -> tuple:
    """从请求路径/查询串推断 Issue 号与事件 seq，用于日志字段 issue_id / seq。"""
    issue = ""
    seq = ""
    m = _ISSUE_PATH_RE.match(path or "")
    if m:
        issue = _normalize_issue(m.group(1))
    if not issue and params is not None:
        for key in ("issue", "issue_number", "issue_id"):
            value = params.get(key) if hasattr(params, "get") else ""
            if value:
                issue = _normalize_issue(value)
                break
    m = _EVENT_PATH_RE.match(path or "")
    if m:
        seq = m.group(1)
    if not seq and params is not None:
        seq = (params.get("seq") if hasattr(params, "get") else "") or ""
    return issue, str(seq)


def _trace_ids_from_traceparent(value: str) -> tuple[str, str]:
    """解析 W3C traceparent（00-<32hex trace_id>-<16hex span_id>-<flags>）。

    非法/缺失返回 ("", "")。用于请求日志兜底：Starlette 的 BaseHTTPMiddleware 在
    call_next 之后已离开 OTel span 上下文，get_current_span() 取不到，改用入口 header。
    """
    parts = (value or "").strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return "", ""
    tid, sid = parts[1].strip(), parts[2].strip()
    if len(tid) != 32 or len(sid) != 16:
        return "", ""
    try:
        int(tid, 16)
        int(sid, 16)
    except ValueError:
        return "", ""
    return tid.lower(), sid.lower()


def _attach_request_log(app: FastAPI) -> None:
    """request_id 注入 + HTTP 请求日志（合并为一个中间件，确保 ContextVar 贯穿日志）。

    从 legacy main.py 原样迁移（含 webhook_url / server_base_url 的 Host 头自愈逻辑）。
    """
    from src.kernel.config import log as _log

    @app.middleware("http")
    async def request_id_and_log_middleware(request: Request, call_next):
        """按状态码分级：5xx → ERROR, 4xx → WARN, 2xx/3xx → INFO。
        健康探针路径跳过请求日志（但 request_id 仍注入）。"""
        rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:12]
        request.scope["request_id"] = rid
        token_rid = request_id_var.set(rid)

        # W3C traceparent 兜底：BaseHTTPMiddleware 的 call_next 之后已不在 span 上下文，
        # 从入口 header 取 trace/span 写入 ContextVar，保证请求日志带 trace_id（Grafana↔Tempo）
        _tid, _sid = _trace_ids_from_traceparent(request.headers.get("traceparent", ""))
        token_tid = trace_id_var.set(_tid)
        token_sid = span_id_var.set(_sid)

        # 从请求路径提取 repo_key（如 /api/dashboard/requiredsa/hello-flask → requiredsa/hello-flask）
        path = request.url.path
        _repo = ""
        _path_parts = path.strip("/").split("/")
        if len(_path_parts) >= 4 and _path_parts[0] == "api":
            _module = _path_parts[1]
            if _module == "agents" and len(_path_parts) >= 5:
                _owner = _path_parts[3]
                _repo_name = _path_parts[4]
                if "/" not in _owner and "." not in _owner:
                    _repo = f"gitcode:{_owner}/{_repo_name}"
            elif _module in {"dashboard", "pipeline"} and len(_path_parts) >= 4:
                _owner = _path_parts[2]
                _repo_name = _path_parts[3]
                if "/" not in _owner and "." not in _owner:
                    _repo = f"gitcode:{_owner}/{_repo_name}"
        if not _repo:
            _qp = request.query_params.get("repo", "")
            if _qp:
                # 已有 provider 前缀（github:o/r 等任意在册平台）原样保留；裸键才补默认前缀
                from src.kernel.repo_key import to_repo_key
                _repo = _qp if ":" in _qp else to_repo_key(_qp)
        token_repo = repo_var.set(_repo)

        _uid = ""
        _auth = request.headers.get("Authorization", "")
        if _auth.startswith("Bearer "):
            try:
                from src.kernel.auth_sdk.jwt_utils import verify_access_token
                _payload = verify_access_token(_auth[7:])
                if _payload and _payload.get("sub"):
                    _uid = str(_payload["sub"])
                else:
                    from src.domains.mgmt.user.db import validate_token
                    _uid_val = validate_token(_auth[7:])
                    if _uid_val:
                        _uid = str(_uid_val)
            except Exception:
                pass
        token_uid = user_var.set(_uid)

        # Issue/seq 维度：/api/issues/<n>、/api/events/<seq> 及 ?issue=/?seq= 直接带进日志
        _issue, _seq = _issue_seq_from_request(path, request.query_params)
        token_issue = issue_var.set(_issue)
        token_seq = seq_var.set(_seq)

        # Issue #372 §14.2.2: 已认证请求到达时从 Host 头推导 webhook_url / server_base_url
        if _uid:
            try:
                from src.domains.mgmt.user.db import get_meta_value, set_meta_value, detect_webhook_from_host
                _host = request.headers.get("host", "")
                _fwd_host = request.headers.get("x-forwarded-host", "")
                _fwd_proto = request.headers.get("x-forwarded-proto", "")

                def _is_local_base(base: str) -> bool:
                    b = (base or "").lower().rstrip("/")
                    return (not b) or b.startswith(("http://localhost", "https://localhost",
                                                    "http://127.", "https://127.",
                                                    "http://0.0.0.0", "https://0.0.0.0",
                                                    "http://[::1]", "https://[::1]"))

                if not get_meta_value("webhook_url"):
                    _candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _candidate:
                        set_meta_value("webhook_url", _candidate)
                        if get_meta_value("webhook_url_detect_failed"):
                            set_meta_value("webhook_url_detect_failed", "")
                        _log.info(f"webhook_url auto-detected from Host header: {_candidate}")

                if _is_local_base(get_meta_value("server_base_url")):
                    _base_candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _base_candidate:
                        _base = _base_candidate[: -len("/webhook/gitcode")]
                        set_meta_value("server_base_url", _base)
                        _log.info(f"server_base_url auto-detected from Host header: {_base}")
            except Exception:
                pass

        skip_log = path in _LOG_SKIP_PATHS or path.startswith(_LOG_SKIP_PREFIXES)
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
                extra = {
                    "method": request.method,
                    "path": redacted_path,
                    "status": status,
                    "duration_ms": round(duration),
                }
                msg = f"{request.method} {redacted_path} status={status} duration={duration:.0f}ms"
                # 同 middleware.py：RUM 反代未配置时 /faro/collect 固定 503（feature-off，非事故）
                if redacted_path == "/faro/collect" and status == 503:
                    _log.debug(msg, extra=extra)
                elif status >= 500:
                    _log.error(msg, extra=extra)
                elif status == 401:
                    if path.startswith("/api/runtime/"):
                        _log.warning(msg, extra=extra)
                    else:
                        _log.debug(msg, extra=extra)
                elif status in (403, 404):
                    _log.debug(msg, extra=extra)
                elif status >= 400:
                    _log.warning(msg, extra=extra)
                else:
                    _log.info(msg, extra=extra)
                from src.kernel.config import SLOW_REQUEST_THRESHOLD_MS
                if duration >= SLOW_REQUEST_THRESHOLD_MS:
                    try:
                        # api_latency_log 归 insight：走 owner 域薄写口（任意拓扑）
                        from src.domains.insight import public as _insight_metrics
                        _insight_metrics.record_api_latency_any(
                            rid, request.method, redacted_path, status,
                            round(duration), _repo, _uid)
                    except Exception as _exc:
                        log.debug(f"api_latency_log insert skipped: {_exc}")
            request_id_var.reset(token_rid)
            repo_var.reset(token_repo)
            user_var.reset(token_uid)
            issue_var.reset(token_issue)
            seq_var.reset(token_seq)
            trace_id_var.reset(token_tid)
            span_id_var.reset(token_sid)


def init_process_db() -> None:
    """Idempotent DB bootstrap shared by every Grape process（从 legacy main.py 迁移）。

    三库独立模式（GRAPE_DOMAIN_LIB=1）下跳过：该模式的 schema 由 provisioning 负责
    （rds_p4_provision 建好三库全部基表），进程启动时不该再跑全量建表/迁移——
    每个进程主库只是本域库，跑全量迁移必然撞「别的域的表不在这里」。
    """
    import os as _os
    if _os.environ.get("GRAPE_DOMAIN_LIB") == "1":
        from src.kernel.config import log as _log
        _log.info("three-database mode: skip full bootstrap (schema provisioned by P4)")
        # 启动自检（#3580 复盘）：多库拓扑下"跨域依赖缺失"必须当场拒绝启动，不能
        # 靠调用方各自降级——缺 GRAPE_DSN_<DOMAIN> 时跨域读会回落进程库，读到的是
        # 同名 VIEW（列快照），基表加列即 1054 的 500（/api/dashboard/* 实锤）。
        from src.kernel.domain_topology import assert_cross_domain_wiring as _assert_wiring
        _assert_wiring()
        # 视图新鲜度：只修本库视图，修不掉（列仍缺/权限不足）→ 严格模式 exit 7。
        try:
            from src.kernel.domain_views import reconcile_on_startup as _reconcile_views
            _reconcile_views()
        except SystemExit:
            raise
        except Exception as _view_err:  # noqa: BLE001
            _log.error(f"three-database mode: view freshness check failed: {_view_err}")
        # kernel 端口绑定与建表/迁移无关，三库模式同样必须执行：insight 角色
        # （dashboard 等按 repo_key 取权限的入口）若拿不到 vcs_cards 端口，
        # repo_key 的平台前缀候选集会退化成空，非默认前缀仓库一律 403。
        try:
            from src.api import contracts_wiring as _contracts_wiring
            _contracts_wiring.bind_all(replace=True)
        except Exception as _cw:
            _log.warning(f"three-database mode: kernel port binding failed ({_cw})")
        # 但本域登记的增量迁移仍要跑：台账（grape_t_schema_migrations）与 DDL 都落在
        # 本域库，归属守卫保证不会碰别域的表；只有 GRAPE_MIGRATION_OWNER 指定的那个
        # 进程执行（runtime=event / insight=insight / mgmt 由 init_database 走常规路径）。
        try:
            from src.api.db_setup import run_domain_migrations as _run_domain_migrations
            _run_domain_migrations()
        except Exception as _mig_err:  # noqa: BLE001
            _log.warning(f"three-database mode: domain migrations skipped ({_mig_err})")
        try:
            from src.kernel.config import _load_accounts
            _load_accounts()
        except Exception as _e:  # 非 mgmt 进程没有账号表，属正常
            _log.debug(f"three-database mode: account cache skipped: {_e}")
        return
    from src.api.db_setup import init_database
    init_database()
    from src.kernel.config import _load_accounts
    _load_accounts()


def attach_health(app: FastAPI, service_name: str = "server", readiness: bool = False) -> None:
    """Liveness / readiness health endpoints.

    - /health: liveness（loopback 内网专用，外网 403）；返回 service 名便于四进程区分
    - /healthz + /readyz + /health/realtime + /metrics: Mgmt 进程独占（readiness=True）
    """
    _PROCESS_STARTED_AT = _dt.now(_tz.utc).isoformat()

    @app.get("/health")
    async def health(request: Request):
        """Liveness：进程存活，不检查 DB。仅允许内网访问。

        同时暴露「这个进程连的是哪个库」：连错库时进程照常 200，只有业务查不到数据，
        远端表现成「凭据全 401、引擎全失联」。以前只能翻 /proc/<pid>/environ 才看得出来。
        """
        if not is_loopback(request):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        try:
            from src.kernel.db_registry import describe_connection
            _db = describe_connection()
        except Exception as _e:  # noqa: BLE001
            _db = {"error": str(_e)}
        return {
            "status": "ok",
            "service": service_name,
            "version": CONTRACT_VERSION,
            "pid": os.getpid(),
            "started_at": _PROCESS_STARTED_AT,
            "database": _db,
        }

    if not readiness:
        return

    # /healthz 3s TTL 缓存（参考 multica readinessCacheTTL）
    _readiness_cache = {"result": None, "expires_at": 0}
    _READINESS_CACHE_TTL = 3  # 秒

    @app.get("/healthz")
    @app.get("/readyz")
    async def readiness(request: Request):
        """Readiness：检查 DB 连通 + 表结构完整性，3s 缓存。仅允许内网访问。"""
        if not is_loopback(request):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        import time as _t
        now = _t.time()
        cached = _readiness_cache["result"]
        if cached and now < _readiness_cache["expires_at"]:
            return cached

        from src.kernel.session_manager import db, db_lock
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
                # agent_tasks 归 runtime：readiness 表探活走 owner 域（任意拓扑）
                from src.domains.runtime import public as _runtime_health
                _runtime_health.health_task_rows_total()
            except Exception:
                checks["tables"] = "error: agent_tasks missing"
                status = "not_ready"
                http_status = 503
        import os as _os_mod2
        _json_path = _os_mod2.path.join(
            _os_mod2.path.dirname(_os_mod2.path.dirname(_os_mod2.path.dirname(_os_mod2.path.abspath(__file__)))),
            "configs", "project-templates.json")
        if _os_mod2.path.isfile(_json_path) and _os_mod2.path.getsize(_json_path) > 100:
            checks["templates"] = "ok"
        else:
            checks["templates"] = "missing: project-templates.json"
            if status == "ok":
                status = "degraded"
        _d32_failed = getattr(app, "state", None)
        if _d32_failed and getattr(_d32_failed, "d32_migration_failed", False):
            checks["migration"] = "error: D32 migration failed"
            status = "not_ready"
            http_status = 503
        else:
            checks["migration"] = "ok"
        resp = JSONResponse(
            status_code=http_status,
            content={"status": status, "checks": checks},
        )
        _readiness_cache["result"] = resp
        _readiness_cache["expires_at"] = now + _READINESS_CACHE_TTL
        return resp

    @app.get("/health/realtime")
    async def health_realtime(request: Request):
        """运维诊断：暴露 IM bot 状态、任务队列深度、runtime 在线数。仅允许内网访问。"""
        if not is_loopback(request):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        from src.domains.runtime.engine.health import derive_runtime_health
        import time as _time2, os as _os2

        now_ts = _time2.time()
        result = {
            "status": "ok",
            "version": CONTRACT_VERSION,
            "pid": _os2.getpid(),
        }
        try:
            # bot_manager 挂在持有 IM WS 的进程模块上（单进程 main / 四进程 grape_server）
            from src.api import main as _main_mod
            _bm = getattr(_main_mod, "bot_manager", None)
            if not _bm:
                try:
                    from src.api import grape_server as _gs_mod
                    _bm = getattr(_gs_mod, "bot_manager", None)
                except Exception:
                    _bm = None
            if _bm:
                result["im"] = _bm.health_check()
        except Exception:
            pass
        try:
            # agent_tasks / agent_runtimes 归 runtime：全量统计走 owner 域原语（任意拓扑）
            from src.domains.runtime import public as _runtime_health
            result["tasks"] = {r[0]: r[1] for r in _runtime_health.health_task_status_counts()}
            result["in_progress"] = _runtime_health.health_task_inflight_count()
            online = 0
            total = 0
            for r in _runtime_health.health_runtime_rows():
                total += 1
                h = derive_runtime_health(r[0] or "offline", r[1], now_ts)
                if h == "online":
                    online += 1
            result["runtimes"] = {"total": total, "online": online}
        except Exception as e:
            result["db_error"] = str(e)

        # Issue #2132 B2/B3: knowledge read cache and truncation counters (hits /
        # misses / evictions / truncated_files / truncated_batches / size / caps).
        # Truncation is no longer visible only through warning logs.
        try:
            from src.api.knowledge import knowledge_cache_stats
            result["knowledge_cache"] = knowledge_cache_stats()
        except Exception as e:
            result["knowledge_cache"] = {"error": str(e)}
        return result

    @app.get("/metrics")
    async def metrics():
        """Prometheus 指标端点已禁用——无外部 Prometheus 消费，且无认证暴露 API 路由结构有安全风险。"""
        return JSONResponse({"error": "metrics endpoint disabled"}, status_code=404)


def check_and_sync_templates() -> bool:
    """检查 project-templates.json + examples/ 是否存在，缺失则自动从 grape-template 仓库拉取。

    防止 git merge/pull 后模板文件被删除导致模板页变空（commit 838f2c98 事故）。
    调用 scripts/sync_templates.sh 执行实际同步。（从 legacy main.py 迁移）
    """
    from src.kernel.config import log as _log

    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _json_path = os.path.join(_repo_root, "configs", "project-templates.json")
    _examples_dir = os.path.join(_repo_root, "examples")

    json_ok = os.path.isfile(_json_path) and os.path.getsize(_json_path) > 100
    examples_ok = os.path.isdir(_examples_dir) and \
        any(os.path.isdir(os.path.join(_examples_dir, d)) for d in os.listdir(_examples_dir) if not d.startswith('.'))

    if json_ok and examples_ok:
        return True

    missing = []
    if not json_ok:
        missing.append("configs/project-templates.json")
    if not examples_ok:
        missing.append("examples/")
    _log.warning(f"Templates: missing {missing}, auto-syncing from grape-template repo...")

    _sync_script = os.path.join(_repo_root, "scripts", "sync_templates.sh")
    if not os.path.isfile(_sync_script):
        _log.error(f"Templates: sync script not found at {_sync_script}")
        return False

    import subprocess as _sp
    try:
        _result = _sp.run(
            ["bash", _sync_script],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "TEMPLATES_REPO": "https://gitcode.com/grape-dev/grape-template.git"}
        )
        if _result.returncode == 0:
            _log.info(f"Templates: auto-sync OK\n{_result.stdout}")
        else:
            _log.error(f"Templates: auto-sync failed (exit={_result.returncode})\n{_result.stderr}")
    except Exception as e:
        _log.error(f"Templates: auto-sync exception: {e}")
        return False

    json_ok = os.path.isfile(_json_path) and os.path.getsize(_json_path) > 100
    examples_ok = os.path.isdir(_examples_dir) and \
        any(os.path.isdir(os.path.join(_examples_dir, d)) for d in os.listdir(_examples_dir) if not d.startswith('.'))
    return json_ok and examples_ok


def attach_internal_guard(app: FastAPI) -> None:
    """兜底：未匹配的 /internal/* 一律返回 JSON 404，而不是落到 SPA catch-all。

    mgmt 不挂载业务型 /internal/* 契约后，未知的 /internal/* 会被前端 catch-all
    当成页面路由返回 200 + index.html，调用方无法区分"不存在"和"页面"。
    必须在业务/GSB 路由注册之后、attach_frontend 之前调用（按注册顺序匹配）。
    """
    from fastapi import HTTPException

    @app.api_route("/internal/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   include_in_schema=False)
    async def _internal_not_found(rest: str):  # noqa: ARG001
        raise HTTPException(status_code=404,
                            detail={"code": 404, "message": "internal endpoint not found"})


def attach_frontend(app: FastAPI) -> None:
    """前端静态托管（Mgmt 进程独占）：附件/社区静态目录 + SPA（i18n/兑票注入）+ admin/account/兜底路由。

    从 legacy main.py 原样迁移；必须在 API 路由与健康端点之后调用，
    保证 SPA catch-all（/{path:path}）最后注册、不拦截 API/health。
    """
    from src.kernel.config import WORKSPACE_DIR as _WS_DIR

    # Issue 附件静态服务（Task Center 上传的文件）
    _ISSUE_ATTACH_DIR = os.path.join(_WS_DIR, "..", "data", "issue_attachments")
    os.makedirs(_ISSUE_ATTACH_DIR, exist_ok=True)
    app.mount("/static/issue_attachments", StaticFiles(directory=_ISSUE_ATTACH_DIR), name="issue_attachments")

    # Community uploads (images for markdown posts)
    _COMMUNITY_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "community_uploads")
    os.makedirs(_COMMUNITY_UPLOAD_DIR, exist_ok=True)
    app.mount("/static/community_uploads", StaticFiles(directory=_COMMUNITY_UPLOAD_DIR), name="community_uploads")

    # Vue 迁移后的静态根：优先 frontend/shell（本仓布局），回退 arch 线的 static
    _api_dir = os.path.dirname(os.path.abspath(__file__))
    _legacy_dir = os.path.join(_api_dir, "frontend", "shell")
    STATIC_DIR = _legacy_dir if os.path.isdir(_legacy_dir) else os.path.join(_api_dir, "static")
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Vue 构建产物（phase 1 Gate 0 方案 A 的临时挂载）：迁移前保持与 /static-vue 等价入口
    _VUE_DIST_DIR = os.path.join(_api_dir, "frontend", "static-dist")
    if os.path.isdir(_VUE_DIST_DIR):
        class _VueSPAStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                # SPA 兜底只兜「路径不存在」：其余异常（权限/IO/磁盘错）必须冒泡，
                # 否则真实 5xx 会被伪装成 200 的 index.html，故障定位被掩盖。
                from starlette.exceptions import HTTPException as _HTTPExc

                try:
                    return await super().get_response(path, scope)
                except _HTTPExc as e:
                    if e.status_code == 404:
                        return await super().get_response("index.html", scope)
                    raise
        app.mount("/static-vue", _VueSPAStaticFiles(directory=_VUE_DIST_DIR, html=True), name="static_vue")

    if os.path.isdir(STATIC_DIR):
        # Static assets (JS/CSS): allow browser caching with ETag revalidation.
        from starlette.middleware.base import BaseHTTPMiddleware

        class NoCacheStaticMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                resp = await call_next(request)
                if request.url.path.startswith("/static/"):
                    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                return resp

        app.add_middleware(NoCacheStaticMiddleware)

        from fastapi.responses import FileResponse, HTMLResponse
        from src.domains.mgmt.auth.email_service import is_email_configured as _is_email_configured
        import json as _json_mod

        # ── i18n boost: 服务端注入翻译数据，消除首屏 i18n 闪动 ──
        _I18N_BOOST_CACHE = None

        def _get_i18n_boost():
            """启动时读取 locale JSON，提取 auth 页面 [data-i18n] 需要的 key，缓存为双语 boost pack。"""
            nonlocal _I18N_BOOST_CACHE
            if _I18N_BOOST_CACHE is not None:
                return _I18N_BOOST_CACHE
            try:
                import re as _re
                boost = {'en': {'app': {}}, 'zh': {'app': {}}}
                auth_keys = set()
                for fname in ('register.html', 'forgot.html', 'index.html'):
                    fpath = os.path.join(STATIC_DIR, fname)
                    if os.path.isfile(fpath):
                        with open(fpath, 'r', encoding='utf-8') as f:
                            html = f.read()
                        for m in _re.finditer(r'data-i18n(?:-placeholder)?="([^"]+)"', html):
                            auth_keys.add(m.group(1))
                for lang in ('en', 'zh'):
                    app_path = os.path.join(STATIC_DIR, 'locales', lang, 'app.json')
                    if os.path.isfile(app_path):
                        with open(app_path, 'r', encoding='utf-8') as f:
                            app_data = _json_mod.load(f)
                        org_path = os.path.join(STATIC_DIR, 'locales', lang, 'organizations.json')
                        org_data = {}
                        if os.path.isfile(org_path):
                            with open(org_path, 'r', encoding='utf-8') as of:
                                org_data = _json_mod.load(of)
                        for k in auth_keys:
                            parts = k.split('.')
                            if parts[0] == 'app' and len(parts) >= 2:
                                section, leaf = parts[1], parts[-1]
                                if section not in app_data:
                                    continue
                                if isinstance(app_data[section], dict):
                                    if leaf in app_data[section]:
                                        boost[lang]['app'].setdefault(section, {})[leaf] = app_data[section][leaf]
                                elif parts[1] == leaf:
                                    boost[lang]['app'][leaf] = app_data[section]
                            elif parts[0] == 'organizations' and len(parts) >= 2:
                                leaf = parts[-1]
                                if leaf in org_data:
                                    boost[lang].setdefault('organizations', {})[leaf] = org_data[leaf]
                    common_path = os.path.join(STATIC_DIR, 'locales', lang, 'common.json')
                    if os.path.isfile(common_path):
                        with open(common_path, 'r', encoding='utf-8') as f:
                            common_data = _json_mod.load(f)
                        for ck in ('server_error', 'request_failed', 'session_expired', 'network_upload_error'):
                            if ck in common_data:
                                boost[lang].setdefault('common', {})[ck] = common_data[ck]
                _I18N_BOOST_CACHE = _json_mod.dumps(boost, ensure_ascii=False, separators=(',', ':'))
            except Exception as _e:
                import sys
                print(f"[WARNING] Failed to build i18n boost pack: {_e}", file=sys.stderr)
                _I18N_BOOST_CACHE = '{}'
            return _I18N_BOOST_CACHE

        _EARLY_TRANSLATE_SCRIPT = '''<script>
(function(){if(!window.__I18N_BOOST__)return;var lang=localStorage.getItem('grapeLang')||((location.pathname.indexOf('/en')===0)?'en':'zh');if(lang==='zh'){document.documentElement.classList.remove('i18n-loading');return;}
var P=window.__I18N_BOOST__[lang];if(!P)return;
function L(k){var p=k.split('.'),c=P;for(var i=0;i<p.length;i++){if(c==null||typeof c!=='object')return;c=c[p[i]];}return c;}
var els=document.querySelectorAll('[data-i18n]');for(var i=0;i<els.length;i++){var k=els[i].getAttribute('data-i18n');if(k){var v=L(k);if(v&&v!==k)els[i].textContent=v;}}
var phs=document.querySelectorAll('[data-i18n-placeholder]');for(var i=0;i<phs.length;i++){var k=phs[i].getAttribute('data-i18n-placeholder');if(k){var v=L(k);if(v&&v!==k)phs[i].setAttribute('placeholder',v);}}
document.documentElement.classList.remove('i18n-loading');})();
</script>'''

        # UC15（#535）：早期兑票内联脚本（治本方案）
        _EARLY_EXCHANGE_SCRIPT = '''<script>
(function(){
  try {
    var m = location.search.match(/[?&]exchange_code=([0-9a-f]{16,})/);
    if (!m) return;
    var code = m[1];
    sessionStorage.setItem('__oauth_inline_pending__', '1');
    if (window.history.replaceState) window.history.replaceState(null, '', location.pathname);
    fetch('/api/auth/exchange', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code}),
    }).then(function(r){
      if (!r.ok) {
        return r.json().catch(function(){ return null; }).then(function(b){
          var d = (b && b.detail) || {};
          throw {msg: d.message || d.error_code || ''};
        });
      }
      return r.json();
    }).then(function(d){
      try {
        localStorage.setItem('token', d.token);
        localStorage.setItem('app_cache_ver', '46');
        localStorage.removeItem('repo_member_cache');
        localStorage.removeItem('currentRepo');
        localStorage.removeItem('current_org_id');
        sessionStorage.removeItem('currentRepo');
        sessionStorage.setItem('__oauth_inline_login__', '1');
        if (d.first_login) sessionStorage.setItem('__oauth_inline_first_login__', '1');
      } catch (e) {}
      try { sessionStorage.removeItem('__oauth_inline_pending__'); } catch (e) {}
    }).catch(function(e){
      try {
        sessionStorage.setItem('__oauth_exchange_failed__', '1');
        var _em = (e && e.msg) || '';
        if (_em) sessionStorage.setItem('__oauth_exchange_failed_msg__', String(_em));
      } catch (e2) {}
      try { sessionStorage.removeItem('__oauth_inline_pending__'); } catch (e2) {}
    });
  } catch (e) {}
})();
</script>'''

        def _inject_email_configured(html: str) -> str:
            from src.domains.mgmt.auth.providers import get_provider, list_providers
            from src.domains.mgmt.user.db import get_meta_value
            configured = _is_email_configured()
            val = 'true' if configured else 'false'
            cls = 'email-configured' if configured else 'email-not-configured'
            boost_json = _get_i18n_boost()
            # D108（登录按钮零延迟）：注入完整渠道目录（固定展示序），app.js defer
            # 阶段首帧渲染前同步生成按钮，不再等 /auth/providers AJAX
            try:
                from src.domains.mgmt.auth.providers import providers_catalog
                catalog = providers_catalog()
            except Exception:
                catalog = []
            oauth_map = {p["provider"]: bool(p["configured"]) for p in catalog}
            if not oauth_map:
                for key in list_providers():
                    client_id = get_meta_value(f"oauth_{key}_client_id") or ""
                    oauth_map[key] = bool(client_id)
            oauth_classes = ' '.join(
                f'oauth-{key}-not-configured' for key, ok in oauth_map.items() if not ok
            )
            oauth_json = _json_mod.dumps(oauth_map)
            head_inject = (
                f'<script>window.__EMAIL_CONFIGURED__={val};document.documentElement.className+=" {cls} {oauth_classes}";'
                f'window.__OAUTH_CONFIGURED__={oauth_json};'
                f'window.__OAUTH_PROVIDERS__={_json_mod.dumps(catalog)};'
                f'window.__I18N_BOOST__={boost_json};</script>'
            )
            html = html.replace('</head>', head_inject + '</head>', 1)
            html = html.replace('</body>', _EARLY_TRANSLATE_SCRIPT + '</body>', 1)
            html = html.replace('</head>', _EARLY_EXCHANGE_SCRIPT + '</head>', 1)
            # Issue #3395：注入前端功能开关（flag=0 时移除侧边栏插件入口整块）
            html = apply_feature_flags(html)
            return html

        def _serve_auth_page(filename: str) -> HTMLResponse:
            with open(os.path.join(STATIC_DIR, filename), 'r', encoding='utf-8') as f:
                html = f.read()
            html = _inject_email_configured(html)
            return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

        # 官网落地页优先（114 预览形态，对齐生产 www：/=落地页，/app=平台）。
        # 目录不存在时全部 miss，行为与未改动前完全一致（生产零影响）。
        _LANDING_DIR = os.environ.get("GRAPE_LANDING_DIR", "")

        # docs 站（生产由 nginx 伺服；114 预览：GRAPE_DOCS_DIR 指向 build-docs.py 生成物）
        _DOCS_DIR = os.environ.get("GRAPE_DOCS_DIR", "")

        def _landing_file(rel: str):
            if rel == "docs" or rel.startswith("docs/"):
                if not _DOCS_DIR or not os.path.isdir(_DOCS_DIR):
                    return None
                droot = os.path.realpath(_DOCS_DIR)
                dfull = os.path.realpath(os.path.join(droot, rel[5:] or "index.html"))
                if dfull != droot and not dfull.startswith(droot + os.sep):
                    return None
                if os.path.isdir(dfull):
                    dfull = os.path.join(dfull, "index.html")
                return dfull if os.path.isfile(dfull) else None
            if not _LANDING_DIR or not os.path.isdir(_LANDING_DIR):
                return None
            root = os.path.realpath(_LANDING_DIR)
            full = os.path.realpath(os.path.join(root, rel))
            if full != root and not full.startswith(root + os.sep):
                return None
            if os.path.isdir(full):
                full = os.path.join(full, "index.html")
            return full if os.path.isfile(full) else None

        def _spa_response():
            with open(os.path.join(STATIC_DIR, "index.html"), 'r', encoding='utf-8') as f:
                html = f.read()
            html = _inject_email_configured(html)
            return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

        @app.get("/")
        async def serve_spa():
            _lf = _landing_file("index.html")
            if _lf:
                from fastapi.responses import FileResponse as _LFR
                return _LFR(_lf)
            return _spa_response()

        @app.get("/register")
        async def serve_register():
            return _serve_auth_page("register.html")

        @app.get("/forgot")
        async def serve_forgot():
            return _serve_auth_page("forgot.html")

        @app.get("/en")
        @app.get("/en/{path:path}")
        async def serve_spa_en(path: str = ""):
            from fastapi import HTTPException
            if path.startswith("api/") or path.startswith("static/"):
                raise HTTPException(status_code=404)
            _lf = _landing_file(os.path.join("en", path, "index.html") if not path else os.path.join("en", path))
            if _lf:
                from fastapi.responses import FileResponse as _LFR
                return _LFR(_lf)
            if path == "register":
                return _serve_auth_page("register.html")
            if path == "forgot":
                return _serve_auth_page("forgot.html")
            if path == "admin" or path.startswith("admin/"):
                _admin_html = os.path.join(STATIC_DIR, "admin.html")
                if os.path.isfile(_admin_html):
                    return FileResponse(_admin_html,
                                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
            return _spa_response()

    # Admin / Account / OAuth callback / SPA catch-all（必须在所有 API 路由与健康端点之后注册）
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    from fastapi import HTTPException

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def serve_admin(path: str = ""):
        """Admin 独立页面：返回 <STATIC_DIR>/admin.html（frontend 单页）。"""
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404)
        _admin_html = os.path.join(STATIC_DIR, "admin.html")
        if os.path.isfile(_admin_html):
            resp = FileResponse(_admin_html)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp
        raise HTTPException(status_code=404, detail="admin.html not found")

    @app.get("/account", include_in_schema=False)
    @app.get("/account/{path:path}", include_in_schema=False)
    async def serve_account_redirect():
        """旧用户中心 URL 302 重定向到内嵌 SPA view。"""
        return RedirectResponse(url="/#account", status_code=302)

    @app.get("/oauth-popup-callback.html", include_in_schema=False)
    async def serve_oauth_popup_callback():
        """OAuth 弹窗回调中间页——极简 HTML，通过 postMessage 通知父窗口后关闭。"""
        resp = FileResponse(os.path.join(STATIC_DIR, "oauth-popup-callback.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/favicon.ico", include_in_schema=False)
    async def serve_favicon():
        """页签图标：浏览器/书签直连 /favicon.ico，不能落到 SPA 兜底。

        兜底会把 favicon.ico 当成页面路由返回 200 + index.html，浏览器拿到非图片
        内容就只能显示默认空白图标（下线前浏览器还爱请求它）。
        """
        _fav = os.path.join(STATIC_DIR, "favicon.ico")
        if not os.path.isfile(_fav):
            raise HTTPException(status_code=404)
        resp = FileResponse(_fav, media_type="image/x-icon")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # 官网落地页（grape-website）优先：文件存在则直接伺服（对齐生产 www 形态：/=落地页，/app=平台）。
    _LANDING_DIR = os.environ.get("GRAPE_LANDING_DIR", "/root/grape-website-preview")

    def _landing_candidate(path: str):
        _df = _landing_file(path if not path.endswith("/") else path + "index.html")
        if _df:
            return _df
        if not os.path.isdir(_LANDING_DIR):
            return None
        rel = os.path.join(path, "index.html") if (not path or path.endswith("/")) else path
        root = os.path.realpath(_LANDING_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full if os.path.isfile(full) else None

    @app.get("/{path:path}")
    async def spa_catch_all(path: str):
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404)
        _lf = _landing_candidate(path)
        if _lf:
            from fastapi.responses import FileResponse as _LandingFR
            return _LandingFR(_lf)
        if not os.path.isdir(STATIC_DIR):
            raise HTTPException(status_code=404)
        with open(os.path.join(STATIC_DIR, "index.html"), 'r', encoding='utf-8') as f:
            html = f.read()
        html = _inject_email_configured(html)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


def include_selected(
    app: FastAPI,
    router,
    methods=None,
    paths=None,
) -> None:
    """把已有 APIRouter 中满足条件（HTTP method / 路径子串）的路由挂到 app 上。

    用于跨进程路由分配（设计 §5.5）：不同进程复用同一路由文件的不同端点，
    不复制 handler、不改共享路由文件。mount 后的路由对象保留原有 path/methods/
    dependencies/openapi 元数据。

    - methods: 允许的 HTTP method 集合（如 {"GET"} / {"POST"}）；None=全部
    - paths:   路径子串过滤器（任一命中即挂载）；None=全部
    """
    routes = []
    for r in router.routes:
        if not isinstance(r, APIRoute):
            continue
        if methods is not None and not (r.methods & set(methods)):
            continue
        if paths is not None and not any(p in r.path for p in paths):
            continue
        routes.append(r)
    if routes:
        from fastapi import APIRouter
        sub = APIRouter(routes=routes)
        app.include_router(sub)


def attach_internal_routes(app: FastAPI, domain: str) -> None:
    """Mount /internal/* routes for cross-domain RemoteProvider calls (Phase 5).

    Always installs InternalAuthMiddleware (guards /internal/gsb/* registry too),
    then mounts the domain's business internal router. mgmt intentionally gets no
    business router: it is the management-plane service and must not be called
    over HTTP by event/worker/insight (those read the shared DB or use in-process
    contracts instead).

    Args:
        app: FastAPI application instance
        domain: domain name (mgmt / runtime / insight)
    """
    from src.kernel.auth_sdk.internal_middleware import InternalAuthMiddleware
    app.add_middleware(InternalAuthMiddleware)

    # mgmt 是管理态服务：P2 起只开放**只读白名单**内部端点 + 补偿应用端点（方案 §D5），
    # 不再"完全不开放"（那会导致 runtime 侧 webhook-config/bot-config 42 次 404）。
    # 白名单与写端点分别由 readonly_router / xwrite_router 承载，见 mgmt/routes/internal.py。
    if domain == "mgmt":
        from src.domains.mgmt.routes.internal import readonly_router, xwrite_router

        app.include_router(readonly_router)
        app.include_router(xwrite_router)
        log.info("attach_internal_routes: mgmt 挂载只读白名单 %d 条 + xwrite/apply",
                 len(readonly_router.routes))
        return

    if domain == "runtime":
        from src.domains.runtime.routes.internal import router as internal_router
    elif domain == "insight":
        from src.domains.insight.routes.internal import router as internal_router
    else:
        return
    app.include_router(internal_router)