"""Grape Service Bus — Provider 工厂（按方法路由：remote/lib 并存，Issue #2479）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.6；
Issue #2479 按方法路由方案见 docs/issue-2479/plan.md。

选择逻辑（两级 key 并存：``service.method`` 契约方法 + ``domain.primitive`` 原语）：

1. **禁止矩阵守卫**（恒优先、不依赖配置表）：
   ``runtime|insight`` 进程对 ``mgmt`` 的任何调用一律 Local——
   ``get_service("mgmt")`` 直接返回 Local 实例（含 mgmt public 全部原语）。
   ``mgmt → runtime|insight`` 方向允许 remote。
2. **全局开关**（部署级）：``GRAPE_PROVIDER_MODE`` 非 ``remote`` → 一律 Local
   （存量单机/共库行为零回归）。未配置时行为与改造前一致。
3. **按方法路由**（``GRAPE_PROVIDER_MODE=remote``）：返回 ``_RoutedService`` 代理，
    每次方法调用按 决策链 取值：
    矩阵守卫 → DB 配置表（``gsb_provider_route``，version+TTL 缓存，改表 ≤30s 生效）
    → 内置基线（热路径四项 + 压测超阈值 14 项 / public 原语 lib；
    压测采样错误的 5 项默认偏好 remote，remote 由配置表显式放开）。

Remote 超时/不可达 **fail-fast**：代理把传输层异常包装为
``GsbRemoteTimeoutError`` / ``GsbRemoteUnreachableError``（稳定错误码），
**不回落 lib**（避免同一请求在不同节点走出不同一致性路径）；
业务错误（HTTP 状态码/ValueError）原样抛出。

新增服务只需 3 步：
1. 定义契约接口（Protocol/ABC）
2. 实现 Local + Remote Provider
3. 调用 register_provider() 注册

使用方式：
    from src.kernel.gsb.provider import get_mgmt_service
    svc = get_mgmt_service()  # 自动按方法路由（Local / Remote 并存）
    cfg = svc.get_repo_webhook_config("owner/repo")
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Any, Callable

from src.kernel.config import log
from src.kernel.gsb import route_config
from src.kernel.gsb.client import (
    GsbRemoteChannelError,
    GsbRemoteTimeoutError,
    GsbRemoteUnreachableError,
)
# 能力端口：kernel 不 import src.domains；契约域 Provider 的注册实现由装配层
# （src/api/contracts_wiring.py）绑定到 pipeline 端口，这里只在调用点解析。
from src.kernel.ports import pipeline as _pipeline

# Provider 注册表
_PROVIDERS: dict[str, dict[str, Callable[[], Any] | None]] = {}

_MODE_LIB = "lib"
_MODE_REMOTE = "remote"

# remote 通道传输层异常（fail-fast 包装口径；HTTP 业务错误不在内）
_REMOTE_CHANNEL_ERRORS: tuple = ()
try:
    import httpx as _httpx

    _REMOTE_CHANNEL_ERRORS = (_httpx.TransportError,)
except ImportError:  # pragma: no cover - httpx 缺失时 remote 不可用
    _httpx = None  # type: ignore[assignment]


def register_provider(
    name: str,
    local_cls: Callable[[], Any],
    remote_cls: Callable[[], Any] | None = None,
) -> None:
    """注册服务的 Local/Remote Provider。

    在模块加载时调用（如 src/domains/contracts/__init__.py）。
    新增服务时在自己的模块中调用此函数注册。

    Args:
        name: 服务名（mgmt / runtime / insight / future-service）
        local_cls: LocalProvider 类（无参构造返回实例）
        remote_cls: RemoteProvider 类（无参构造返回实例），可选
    """
    _PROVIDERS[name] = {"local": local_cls, "remote": remote_cls}
    # 注册面变化 → 代理缓存失效（避免测试/重注册后拿到旧工厂）
    _ROUTED_SERVICE_CACHE.drop(name)


def _ensure_registered() -> None:
    """首次调用时惰性注册 contracts Provider（避免模块级 import cycle）。

    注册动作（`ensure_providers_registered`）属契约域，经 pipeline 端口在调用点解析；
    端口未绑定时 fail-fast 抛 PortNotBound，不再用域懒加载把故障推迟到别处。
    """
    if not _PROVIDERS:
        _pipeline.ensure_providers_registered()


# ---------------- 调用方进程域识别 + 禁止矩阵 ----------------

#: 四进程角色 → 域（docs/lib-3db-3host-design.md §3.1：
#: grape-server=mgmt；grape-event-server/grape-worker-server=runtime 域）
_ROLE_DOMAIN = {
    "server": "mgmt",
    "mgmt": "mgmt",
    "event": "runtime",
    "worker": "runtime",
    "runtime": "runtime",
    "insight": "insight",
}


def caller_domain() -> str | None:
    """当前进程所属域（禁止矩阵的调用方维度）。

    优先显式角色（GRAPE_PROCESS_ROLE / GRAPE_SERVICE_NAME），否则回落
    ``db_registry.current_domain()``（DEVOPS_DB_NAME 派生）。识别不了返回 None
    （不触发矩阵守卫——如测试进程/工具进程）。
    """
    for key in ("GRAPE_PROCESS_ROLE", "GRAPE_SERVICE_NAME"):
        raw = (os.environ.get(key) or "").strip().lower()
        if raw in _ROLE_DOMAIN:
            return _ROLE_DOMAIN[raw]
    try:
        from src.kernel import db_registry

        return db_registry.current_domain()
    except Exception as exc:  # noqa: BLE001 - db_registry 不可用不影响路由
        log.warning(f"caller_domain fallback failed: {type(exc).__name__}: {exc}")
        return None


def matrix_forces_lib(target_service: str) -> bool:
    """禁止矩阵：**以 mgmt 为被调方的一律 lib**（任何调用方、不依赖配置表）。

    两个理由叠在一起：

    1. 回环风险。mgmt 的 ``/internal/*`` 自己也会经 provider 往外调，
       一旦 mgmt 被当作 remote 目标，就会出现 ``mgmt /internal/x → 远端 mgmt /internal/x``
       这类同域回环（provider.py 的历史注释记录过直接 500/超时）。
    2. 运维口径。mgmt 是管理态服务，跨进程调它意味着管理面无谓地依赖网络可用性；
       保持 Local 让 mgmt 的读路径继续是"连自己的库"，不受兄弟机抖动影响。

    ``mgmt → runtime|insight`` 方向仍允许 remote（按配置表 / 内置基线）。
    """
    return target_service == "mgmt"


# ---------------- 按方法路由决策 ----------------


def decide_mode(service: str, method: str) -> str:
    """路由决策：矩阵守卫 → 全局开关 → 配置表 → 内置基线。

    Args:
        service: 目标服务名（mgmt / runtime / insight）
        method: 契约方法名或 public 原语名

    Returns:
        ``lib`` / ``remote``（矩阵守卫与全局开关恒优先，配置表缺行按内置基线：
        热路径四项 + 压测超阈值 14 项与 public 原语 → lib，
        采样错误的契约方法 → remote）。
    """
    # 1) 禁止矩阵守卫：不查任何配置，runtime|insight → mgmt 恒 lib
    if matrix_forces_lib(service):
        return _MODE_LIB
    # 2) 全局开关：未开 remote 制式 → 存量行为（一律 Local）
    if not is_remote_mode():
        return _MODE_LIB
    key = f"{service}.{method}"
    # 3) DB 配置表（version+TTL 缓存；缺行 → 基线；读失败退回上次已知配置）
    row = route_config.get_cached_route(key)
    if row:
        value = (row.get("mode") or "").strip().lower()
        if value in (_MODE_LIB, _MODE_REMOTE):
            return value
    # 4) 内置基线：热路径四项 + 压测超阈值 14 项 → lib（「不影响性能」裁决，
    #    数据支撑见 docs/issue-2479/bench-report.md；remote 由配置表显式放开）；
    #    public 原语（本期无 remote 传输通道）→ lib；
    #    其余契约方法（压测采样错误 5 项，无数据）→ 默认偏好 remote
    if key in _BUILTIN_LIB_KEYS:
        return _MODE_LIB
    if method in route_config.PUBLIC_PRIMITIVES.get(service, ()):
        return _MODE_LIB
    return _MODE_REMOTE


#: 内置基线锁 lib 的契约 key（QA 审查修复，对齐 bench-report 落地结论）：
#: - 热路径四项：逐请求/逐事件调用；
#: - 超阈值 14 项：loopback+共库压测 P95 相对增幅 >20%（绝对增量虽 ≤10ms，
#:   但 lib 基线亚毫秒，「不影响性能」口径判超阈值，QA 报告 Required#1 裁决：
#:   基线锁 lib，remote 由配置表 gsb_provider_route 显式放开（改表 ≤30s 生效）。
#: 压测数据支撑见 docs/issue-2479/bench-report.md。
_BUILTIN_LIB_KEYS = frozenset({
    # 热路径四项
    "mgmt.verify_session_token",
    "insight.log_obs",
    "insight.log_api_latency",
    "insight.log_llm_usage",
    # 压测超阈值 14 项（bench-report §1 判定列）
    "mgmt.get_repo_webhook_config",
    "mgmt.get_agent_session",
    "mgmt.get_scene_config",
    "mgmt.get_scene_pool_engine_ids",
    "mgmt.get_bot_config",
    "mgmt.get_skill",
    "runtime.get_events",
    "runtime.get_event_detail",
    "runtime.get_tasks",
    "runtime.get_task_messages",
    "runtime.get_runtimes",
    "runtime.get_chat_messages",
    "insight.query_alerts",
    "insight.aggregate_runtime_stats",
})


def is_remote_mode() -> bool:
    """当前是否启用 remote 制式（按方法路由仅在 ``GRAPE_PROVIDER_MODE=remote`` 生效）。"""
    return os.environ.get("GRAPE_PROVIDER_MODE", "").strip().lower() == "remote"


def _require_local(name: str, entry: dict) -> Any:
    local_factory = entry["local"]
    if local_factory is None:
        raise ValueError(f"Local provider not registered for service: {name}")
    return local_factory()


# ---------------- 按方法路由代理 ----------------


class _RoutedService:
    """按方法路由的服务代理（``GRAPE_PROVIDER_MODE=remote`` 制式下由 get_service 返回）。

    惰性各持一个 Local / Remote 实例；每次方法调用经 decide_mode() 决策：
    - lib → Local 实例直调；
    - remote → Remote 实例调用；传输层异常 fail-fast 包装（错误码稳定，
      **不回落 lib**），业务错误原样抛出。
    """

    def __init__(self, name: str, entry: dict):
        self._svc_name = name
        self._local_factory = entry["local"]
        self._remote_factory = entry["remote"]
        self._lock = threading.Lock()
        self._local: Any = None
        self._remote: Any = None

    @property
    def service_name(self) -> str:
        """目标服务名（诊断用）。"""
        return self._svc_name

    def _instance(self, mode: str) -> Any:
        if mode == _MODE_REMOTE:
            if self._remote is None:
                self._remote = self._remote_factory()
            return self._remote
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def _remote_timeout_ctx(self, method: str):
        """配置表 per-method timeout_ms → 逐请求超时覆盖（best-effort）。

        QA 审查修复 Required#3：经 ``ServiceHttpClient.with_timeout()`` 公开
        透传 GsbClient 线程本地 per-request 超时，不改共享客户端状态——
        该方法无配置行时不覆盖（维持默认 10s），也不会把上一个方法的超时
        「粘滞」到后续调用。返回可 with 的上下文管理器。
        """
        try:
            ms = route_config.get_cached_timeout_ms(f"{self._svc_name}.{method}")
            if not ms or ms <= 0:
                return contextlib.nullcontext()
            remote_http = getattr(self._remote, "client", None)
            with_timeout = getattr(remote_http, "with_timeout", None)
            if callable(with_timeout):
                return with_timeout(ms / 1000.0)
            return contextlib.nullcontext()
        except Exception as exc:  # noqa: BLE001 - 超时热应用失败不阻断调用
            log.debug(f"per-method remote timeout not applied "
                      f"({self._svc_name}.{method}): {exc}")
            return contextlib.nullcontext()

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        # 只派发 Local 契约面上存在的公共可调用属性（契约方法）
        target = getattr(self._instance(_MODE_LIB), method, None)
        if not callable(target):
            raise AttributeError(f"{self._svc_name}.{method}")

        def _dispatch(*args: Any, **kwargs: Any) -> Any:
            mode = decide_mode(self._svc_name, method)
            impl = getattr(self._instance(mode), method)
            if mode != _MODE_REMOTE:
                return impl(*args, **kwargs)
            with self._remote_timeout_ctx(method):
                try:
                    return impl(*args, **kwargs)
                except _REMOTE_CHANNEL_ERRORS as exc:
                    # fail-fast：明确错误码、不回落 lib（本地实例未被触碰）
                    if _httpx is not None and isinstance(exc, _httpx.TimeoutException):
                        raise GsbRemoteTimeoutError(
                            f"GSB remote timeout: {self._svc_name}.{method} "
                            f"({type(exc).__name__})"
                        ) from exc
                    raise GsbRemoteUnreachableError(
                        f"GSB remote unreachable: {self._svc_name}.{method} "
                        f"({type(exc).__name__})"
                    ) from exc

        return _dispatch


# ---------------- 对外入口 ----------------


def get_local_service(name: str) -> Any:
    """强制返回 Local 实现（忽略 GRAPE_PROVIDER_MODE 与路由配置表）。

    供 ``/internal/*`` 服务端使用：这些端点的职责就是「被远端调用后在本进程内查数据」，
    如果也跟着 remote 模式走 RemoteProvider，就会 HTTP 回环调用自己
    （mgmt /internal/x → RemoteProvider → mgmt /internal/x …），直接 500/超时。
    """
    _ensure_registered()
    entry = _PROVIDERS.get(name)
    if not entry or entry["local"] is None:
        raise ValueError(f"Local provider not registered for service: {name}")
    return entry["local"]()


# ---------------- 进程级代理缓存（QA 审查修复 Required#2） ----------------


class _RoutedServiceCache:
    """按服务名缓存 ``_RoutedService`` 代理（进程级单例）。

    QA 审查（PR #1398 Required#2）：``get_service()`` 每次新建代理 + Local 实例
    的 per-call 构造成本实测 ~17ms/次（remote 制式下采样，见 bench-report），
    「句柄复用」必须落在 get_service 层——调用方每次取句柄即拿到同一代理，
    Local/Remote 实例在各代理内惰性构造一次后复用。注册面变化由
    ``register_provider()`` 主动失效；注册表被直接改写（测试替身）时按
    entry 身份不一致重建。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[dict, Any]] = {}

    def get(self, name: str, entry: dict) -> Any:
        with self._lock:
            cached = self._items.get(name)
            if cached is not None and cached[0] is entry:
                return cached[1]
            proxy = _RoutedService(name, entry)
            self._items[name] = (entry, proxy)
            return proxy

    def drop(self, name: str) -> None:
        with self._lock:
            self._items.pop(name, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_ROUTED_SERVICE_CACHE = _RoutedServiceCache()


def get_service(name: str) -> Any:
    """获取服务实现——矩阵守卫 / 全局开关 / 按方法路由。

    - ``runtime|insight`` 进程请求 ``mgmt`` → 恒 Local 实例（禁止矩阵，不依赖配置表）；
    - ``GRAPE_PROVIDER_MODE`` 非 ``remote`` → Local 实例（存量零回归）；
    - ``GRAPE_PROVIDER_MODE=remote`` 且有 Remote 实现 → ``_RoutedService`` 代理，
      每次方法调用按 决策链 路由（配置表缺行按内置基线：热路径四项 + 超阈值
      14 项/原语 → lib，采样错误 5 项 → remote）。

    首次调用时惰性注册 contracts Provider（避免模块级 import cycle）。

    Raises:
        ValueError: 服务未注册 / 无可用 provider
    """
    _ensure_registered()
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown service: {name}. "
            f"Did you register_provider('{name}', ...)?"
        )
    entry = _PROVIDERS[name]
    if matrix_forces_lib(name) or not is_remote_mode() or entry["remote"] is None:
        return _require_local(name, entry)
    # 进程级代理缓存：调用方每次 get_service() 拿同一代理（QA Required#2），
    # Local/Remote 实例与 HTTP 客户端在代理内惰性构造一次后复用。
    return _ROUTED_SERVICE_CACHE.get(name, entry)


# GsbRemoteChannelError 重导出：业务侧从 provider 即可捕获 fail-fast 错误码
__all__ = [
    "register_provider",
    "get_service",
    "get_local_service",
    "get_mgmt_service",
    "get_runtime_service",
    "get_insight_service",
    "is_remote_mode",
    "decide_mode",
    "caller_domain",
    "matrix_forces_lib",
    "GsbRemoteChannelError",
    "GsbRemoteTimeoutError",
    "GsbRemoteUnreachableError",
]


# ===== 便捷函数——预注册的三个域 =====


def get_mgmt_service() -> Any:
    """获取 MgmtService 实现。"""
    return get_service("mgmt")


def get_runtime_service() -> Any:
    """获取 RuntimeService 实现。"""
    return get_service("runtime")


def get_insight_service() -> Any:
    """获取 InsightService 实现。"""
    return get_service("insight")
