"""共享健康检查端点（Issue #596 P1：从 main.py 抽出，多入口复用）。

三层健康检查（liveness / readiness / realtime）+ /api/ping。
各进程标识通过 `service_name` 区分（§4.7 健康检查端点响应 service 标识）：
- A: {"status": "ok", "service": "server"}
- B: {"status": "ok", "service": "event-server"}
- C: {"status": "ok", "service": "worker-server"}
"""
import os
import time
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Issue #2784：契约版本号单一来源（健康探针与 /openapi.json 必须是同一个号）
from src.api.contract_versioning import CONTRACT_VERSION

_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _is_loopback(request: Request) -> bool:
    """判断请求是否来自内网（127.0.0.1 / ::1）。"""
    client = request.client
    if client and client.host in ("127.0.0.1", "::1", "localhost"):
        return True
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first in ("127.0.0.1", "::1"):
            return True
    xri = request.headers.get("x-real-ip", "")
    if xri in ("127.0.0.1", "::1"):
        return True
    return False


def install_health_endpoints(app: FastAPI, service_name: str = "server") -> None:
    """在 app 上注册 /health /healthz /readyz /health/realtime /api/ping。

    - service_name：进程标识（server / event-server / worker-server），
      健康响应携带（§4.7）；
    - IM 状态读取走 `app.state.im_engine`（A 进程装配新引擎；
      B/C 无 IM，realtime 不报 im 字段）。
    """

    @app.get("/health")
    async def health(request: Request):
        """Liveness：进程存活，不检查 DB（K8s livenessProbe 用）。

        同时暴露「这个进程连的是哪个库」：连错库时进程照常 200，只有业务查不到数据，
        表现成远端引擎失联/凭据 401。以前只能翻 /proc/<pid>/environ 才看得出来。
        """
        if not _is_loopback(request):
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

    # /healthz 3s TTL 缓存
    _readiness_cache = {"result": None, "expires_at": 0}
    _READINESS_CACHE_TTL = 3  # 秒

    @app.get("/healthz")
    @app.get("/readyz")
    async def readiness(request: Request):
        """Readiness：DB 连通 + 表结构完整性，3s 缓存。"""
        if not _is_loopback(request):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        now = time.time()
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
                # agent_tasks 归 runtime：表探活走 owner 域原语（任意拓扑）。
                # 原实现在 api 进程主库直发 COUNT(*)，三库拓扑下 api 库无 agent_tasks
                # （视图 0）→ 恒 1146，readiness 永远 503，被 K8s 反复重启。
                from src.domains.runtime import public as _runtime_health
                _runtime_health.health_task_rows_total()
            except Exception:
                checks["tables"] = "error: agent_tasks missing"
                status = "not_ready"
                http_status = 503
        # Issue #887：skill 一致性门禁聚合项（近 N 天 [skill_mismatch] 拒绝数，AC4）。
        # 非阻断（warn 不改 readiness 终态，仅标记 degraded 告警）。
        try:
            from src.domains.runtime.engine.health import aggregate_skill_consistency as _agg_sk
            _sk_agg = _agg_sk()
            checks["skill_consistency"] = _sk_agg.get("status", "unknown")
            if _sk_agg.get("status") == "warn":
                checks["skill_consistency"] = f"warn: {_sk_agg.get('detail', '')}"
                if status == "ok":
                    status = "degraded"
        except Exception as _ske:
            checks["skill_consistency"] = f"error: {_ske}"
        resp = JSONResponse(
            status_code=http_status,
            content={"status": status, "checks": checks, "service": service_name},
        )
        _readiness_cache["result"] = resp
        _readiness_cache["expires_at"] = now + _READINESS_CACHE_TTL
        return resp

    @app.get("/health/realtime")
    async def health_realtime(request: Request):
        """运维诊断：IM bot 状态、任务队列深度、runtime 在线数。"""
        if not _is_loopback(request):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        from src.domains.runtime.engine.health import derive_runtime_health

        now_ts = time.time()
        result = {
            "status": "ok",
            "service": service_name,
            "pid": os.getpid(),
        }

        # IM 状态：新引擎（A2 后唯一链路；legacy BotManager 已下线）
        im_engine = getattr(app.state, "im_engine", None)
        if im_engine is not None:
            try:
                result["im"] = im_engine.health()
            except Exception as e:
                result["im"] = {"error": str(e)}

        try:
            # agent_tasks / agent_runtimes 归 runtime：三条语句走 owner 域具名原语。
            # 原实现在 api 进程主库直发（一把 db_lock 内连查三表），三库拓扑下 api 库
            # 无这两张表（视图 0）→ 整块 1146 落到 db_error，/health/realtime 恒报错。
            # 口径同 api/server_common.py：全量统计不过滤 repo/org，在线判定归调用方。
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
        # misses / evictions / truncated_files / truncated_batches / size / ttl).
        # Truncation is no longer visible only through warning logs.
        try:
            from src.api.knowledge import knowledge_cache_stats
            result["knowledge_cache"] = knowledge_cache_stats()
        except Exception as e:
            result["knowledge_cache"] = {"error": str(e)}

        return result

    @app.get("/api/ping", include_in_schema=False)
    async def api_ping():
        """前端会话保活轻量探针（无鉴权、不查 DB、零 IO）。"""
        return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
