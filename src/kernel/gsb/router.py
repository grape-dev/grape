"""Grape Service Bus — 统一服务路由基类（GsbService）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.5.

每个域进程调用 ``init_gsb(app, role)``，自动获得：
- 服务注册（启动时 register_service，注册失败只告警不影响进程启动）
- 心跳更新（30s asyncio 循环，启动时立即打一次）
- 优雅注销（shutdown 时 deregister_service）
- /internal/gsb/* 运维端点（服务列表 / 单服务发现，集群模式下走服务令牌认证）

使用方式（各进程入口）：
    from src.kernel.gsb.router import init_gsb
    gsb = init_gsb(app, "mgmt")      # import 期：挂路由 + 打 /internal 认证中间件
    # lifespan:
    await gsb.startup()              # 注册 + 起心跳
    yield
    await gsb.shutdown()             # 注销

设计稿里的 ``mount()`` / ``startup()`` / ``shutdown()`` 语义保留，唯一区别是
路由与中间件必须在 app 启动前挂载（FastAPI 启动后不允许再加 middleware），
所以这里拆成 import 期的 ``mount()`` 和运行期的 ``startup()``。

2026-11 补齐：
- base_url 不再写死 localhost，支持 GRAPE_GSB_BASE_URL / GRAPE_{NAME}_URL 覆盖，
  跨机部署（Node A/Node B）注册的地址可直接被对端访问；
- health_url 默认 /api/ping（全进程可达、免鉴权、不查库），避开 /health 的 loopback 限制；
- 新增 /internal/gsb/services 与 /internal/gsb/services/{name} 只读端点；
- 注册失败不再静默：打日志并在下一次心跳时重试注册（自愈）。
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException

from src.kernel.auth_sdk.internal_middleware import InternalAuthMiddleware
from src.kernel.config import log
from src.kernel.gsb.registry import (
    HEARTBEAT_TIMEOUT,
    deregister_service,
    discover_service,
    heartbeat_service,
    list_all_services,
    list_services,
    purge_stale,
    register_service,
)

# 心跳间隔（秒）——设计稿 30s；超时 3 个周期（registry.HEARTBEAT_TIMEOUT）
HEARTBEAT_INTERVAL = 30

# 每 N 拍心跳顺带收敛一次注册表（N × 30s ≈ 10min）
PURGE_EVERY_N_BEATS = 20

# 多机部署命名空间：注册键 = "<service>@<namespace>"，避免两台机器写同一行互相覆盖
# （默认空 = 旧行为，单机部署不受影响）
NAMESPACE_ENV = "GRAPE_GSB_NAMESPACE"


def registry_key(service_name: str, namespace: str | None = None) -> str:
    """注册键：有 namespace 时拼成 ``service@namespace``，否则沿用服务名。"""
    ns = (namespace if namespace is not None else os.environ.get(NAMESPACE_ENV, "")).strip()
    return f"{service_name}@{ns}" if ns else service_name

# 进程角色 → GSB 服务名。worker 进程对外就是 runtime 域（与 GRAPE_RUNTIME_URL 对齐）
_ROLE_TO_SERVICE = {
    "mgmt": "mgmt",
    "event": "event",
    "worker": "runtime",
    "runtime": "runtime",
    "insight": "insight",
}

# 角色默认端口（四进程模式实际端口；单进程 legacy 由 GRAPE_PORT 覆盖）
_ROLE_DEFAULT_PORT = {
    "mgmt": 38081,
    "event": 38082,
    "worker": 38083,
    "runtime": 38083,
    "insight": 38084,
}

# 服务名 → 静态地址环境变量（与 client._ENV_URL_MAP 保持一致）
_SERVICE_URL_ENV = {
    "mgmt": "GRAPE_MGMT_URL",
    "event": "GRAPE_EVENT_URL",
    "runtime": "GRAPE_RUNTIME_URL",
    "insight": "GRAPE_INSIGHT_URL",
}

_REGISTER_RETRY_BACKOFF = 15  # 秒：注册失败后，下一次心跳重试


def service_name_for_role(role: str) -> str:
    """角色 → GSB 服务名（未知角色原样返回，便于扩展新服务）。"""
    return _ROLE_TO_SERVICE.get((role or "").strip().lower(), (role or "").strip().lower())


def resolve_port(role: str, port: int | None = None) -> int:
    """解析本进程监听端口：显式参数 > GRAPE_GSB_PORT > 角色默认端口。"""
    if port:
        return int(port)
    env_port = os.environ.get("GRAPE_GSB_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    return _ROLE_DEFAULT_PORT.get((role or "").strip().lower(), 38080)


def resolve_base_url(service_name: str, port: int, base_url: str | None = None) -> str:
    """解析注册用的 base_url（对端要能访问到）。

    优先级：显式参数 > GRAPE_GSB_BASE_URL > GRAPE_{SERVICE}_URL > http://127.0.0.1:{port}
    """
    if base_url:
        return base_url.rstrip("/")
    env_any = os.environ.get("GRAPE_GSB_BASE_URL")
    if env_any:
        return env_any.rstrip("/")
    env_key = _SERVICE_URL_ENV.get(service_name)
    if env_key:
        env_url = os.environ.get(env_key)
        if env_url:
            return env_url.rstrip("/")
    return f"http://127.0.0.1:{port}"


def _has_internal_auth_middleware(app: FastAPI) -> bool:
    """app 是否已经挂过 InternalAuthMiddleware（attach_internal_routes 会挂）。"""
    for m in getattr(app, "user_middleware", []) or []:
        if getattr(m, "cls", None) is InternalAuthMiddleware:
            return True
    return False


class GsbService:
    """服务基类——每个域进程一个实例，自动获得注册/心跳/注销/运维端点。"""

    def __init__(
        self,
        name: str,
        port: int,
        app: FastAPI,
        base_url: str | None = None,
        health_url: str = "/api/ping",
        version: str | None = None,
    ):
        self.name = name              # 基础服务名（mgmt/event/runtime/insight）
        self.namespace = os.environ.get(NAMESPACE_ENV, "").strip()
        self.key = registry_key(name, self.namespace)  # 注册表主键（多机隔离）
        self.port = port
        self.app = app
        self.base_url = resolve_base_url(name, port, base_url)
        self.health_url = health_url
        self.version = version or str(getattr(app, "version", "1") or "1")
        self.started_at = time.time()

        self.internal_router = APIRouter(prefix="/internal")
        self._mounted = False
        self._registered = False
        self._heartbeat_task: asyncio.Task | None = None
        self._last_register_error = 0.0
        self._stopping = False
        self._build_routes()

    # ---------- 运维端点 ----------

    def _build_routes(self) -> None:
        router = self.internal_router

        @router.get("/gsb/services")
        async def gsb_list_services(include_offline: bool = True) -> dict:
            """注册表全量视图（含 age_seconds / alive），运维排查用。"""
            rows = list_all_services(include_offline=include_offline)
            return {
                "count": len(rows),
                "online": sum(1 for r in rows if r.get("alive")),
                "heartbeat_timeout": HEARTBEAT_TIMEOUT,
                "now": time.time(),
                "services": rows,
            }

        @router.get("/gsb/services/{name}")
        async def gsb_discover(name: str) -> dict:
            """单服务发现（心跳超时视为未找到），语义等同 discover_service()。"""
            svc = discover_service(name)
            if not svc:
                raise HTTPException(status_code=404, detail=f"service not registered or stale: {name}")
            return svc

    # ---------- 生命周期 ----------

    def mount(self) -> None:
        """import 期调用：挂 /internal/gsb/* 路由 + 必要的认证中间件（幂等）。"""
        if self._mounted:
            return
        self._mounted = True

        # 集群模式下 /internal/* 必须带服务令牌；attach_internal_routes 可能已经挂过
        if os.environ.get("GRAPE_MODE", "").strip().lower() == "cluster" and not _has_internal_auth_middleware(self.app):
            self.app.add_middleware(InternalAuthMiddleware)

        self.app.include_router(self.internal_router)
        log.info(f"GSB routes mounted: /internal/gsb/services (service={self.key})")

    def _metadata(self) -> dict[str, Any]:
        return {
            "role": self.name,
            "namespace": self.namespace or socket.gethostname(),
            "port": self.port,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "version": self.version,
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
        }

    def register(self) -> bool:
        """写注册表（幂等）。失败只告警，不阻断进程启动。"""
        try:
            register_service(
                name=self.key,
                base_url=self.base_url,
                health_url=self.health_url,
                metadata=self._metadata(),
                version=self.version,
            )
            self._registered = True
            log.info(
                f"GSB registered: service={self.key} base_url={self.base_url} "
                f"health={self.health_url} version={self.version}"
            )
            return True
        except Exception as e:  # noqa: BLE001 - 注册失败不能拖垮业务进程
            self._registered = False
            self._last_register_error = time.time()
            log.warning(f"GSB register failed: service={self.key} err={type(e).__name__}: {e}")
            return False

    async def startup(self) -> None:
        """lifespan startup：注册 + 启动心跳循环。"""
        self._stopping = False
        self.register()
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def shutdown(self) -> None:
        """lifespan shutdown：停心跳 + 标记 offline（best-effort）。"""
        self._stopping = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 心跳任务异常不影响注销
                pass
            self._heartbeat_task = None
        try:
            deregister_service(self.key)
            log.info(f"GSB deregistered: service={self.key}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"GSB deregister failed: service={self.key} err={type(e).__name__}: {e}")

    async def _heartbeat_loop(self) -> None:
        """心跳循环：立即打一次，之后每 30s；未注册成功则顺带重试注册（自愈）。

        每 ``PURGE_EVERY_N_BEATS`` 拍顺带收敛一次注册表状态（把心跳超时的
        online 行标 offline），否则进程 kill -9 后注册表会长期显示 online。
        收敛是 best-effort，失败只记日志，不影响心跳。
        """
        beats = 0
        while not self._stopping:
            try:
                if not self._registered:
                    self.register()
                else:
                    heartbeat_service(self.key)
                    self._registered = True
            except Exception as e:  # noqa: BLE001
                self._registered = False
                now = time.time()
                if now - self._last_register_error > _REGISTER_RETRY_BACKOFF:
                    self._last_register_error = now
                    log.warning(f"GSB heartbeat failed: service={self.key} err={type(e).__name__}: {e}")

            beats += 1
            if beats % PURGE_EVERY_N_BEATS == 0:
                try:
                    purged = purge_stale()
                    if purged:
                        log.info(f"GSB purge_stale: {purged} stale service(s) marked offline")
                except Exception as e:  # noqa: BLE001
                    log.warning(f"GSB purge_stale failed: {type(e).__name__}: {e}")

            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                break


def init_gsb(app: FastAPI, role: str, port: int | None = None, base_url: str | None = None) -> GsbService:
    """进程入口 import 期调用：创建 GsbService 并挂载路由/中间件。

    lifespan 里配套调用 ``await svc.startup()`` / ``await svc.shutdown()``，
    实例同时写入 ``app.state.gsb`` 供 lifespan 取用。
    """
    name = service_name_for_role(role)
    resolved_port = resolve_port(role, port)
    svc = GsbService(name=name, port=resolved_port, app=app, base_url=base_url)
    svc.mount()
    app.state.gsb = svc
    return svc


def get_gsb(app: FastAPI) -> GsbService | None:
    """从 app.state 取本进程的 GsbService（未初始化返回 None）。"""
    return getattr(app.state, "gsb", None)


__all__ = [
    "HEARTBEAT_INTERVAL",
    "NAMESPACE_ENV",
    "registry_key",
    "GsbService",
    "get_gsb",
    "init_gsb",
    "resolve_base_url",
    "resolve_port",
    "service_name_for_role",
    "list_services",
]