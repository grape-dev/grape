"""SandboxManager——策略决策 + Provider 缓存 + 持有全局依赖。

职责：
1. get_sandbox_mode：从 DB 查 sandbox_mode + degrade_policy
2. prepare：fail-closed 决策 + 委托 Provider 创建 Sandbox
3. Provider 缓存：首次探测后缓存，reset_provider() 供测试

detect 出能力事实（detect_capabilities），manager 做选择（用 registry dict 映射）。
audit/credential 是全局单例，通过 registry factory 注入给 Provider。

fail-closed 决策矩阵：
- DISABLED → NullSandbox
- GRAPE_AGENT_SANDBOX=off → NullSandbox（紧急关闭）
- REQUIRED + Provider 可用 → BwrapSandbox
- REQUIRED + Provider 不可用 → raise SandboxUnavailableError
- PREFERRED + Provider 可用 → BwrapSandbox
- PREFERRED + 不可用 → NullSandbox（降级 + 告警）
"""
from __future__ import annotations

import os
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Callable, Mapping

from .api import Sandbox, SandboxProvider
from .types import SandboxMode, ExecutionMode
from .errors import SandboxUnavailableError
from .credential import CredentialStrategy, NullCredentialStrategy
from .isolation.seccomp import SeccompProgram, NullSeccompProgram
from .alert import AlertEngine, NullAlertEngine
from .sandbox_context import SandboxContext
from src.domains.runtime.agents.common.audit import AuditListener, NullAuditLogger, AuditEvent, AuditKind

log = logging.getLogger("devops-bot")


class SandboxManager:
    """沙箱管理器。

    模块级默认实例由 __init__.py 导出（sandbox_manager）。
    测试可构造新实例隔离：manager = SandboxManager(audit=FakeAudit())。
    生产环境必须显式构造覆盖（注入 FileAuditLogger / SentinelStrategy）。
    """

    def __init__(
        self, *,
        env: Mapping[str, str] | None = None,
        audit: AuditListener | None = None,
        credential: CredentialStrategy | None = None,
        seccomp: SeccompProgram = NullSeccompProgram(),
        alert_engine=None,
        registry: Mapping[str, Callable] | None = None,
        repo=None,
    ):
        self._env = dict(env) if env else dict(os.environ)
        self._audit = audit or NullAuditLogger()
        self._credential = credential or NullCredentialStrategy()
        self._seccomp = seccomp
        self._alert_engine = alert_engine
        self._registry = registry or self._default_registry()
        self._repo = repo  # SandboxRepository 实例（延迟初始化）
        self._provider: SandboxProvider | None = None
        self._template_id = ""  # 平台侧模板 ID（云沙箱用）
        self._provider_resolved = False
        self._provider_cache: dict[str, tuple] = {}  # 遗漏-2 修复：Provider 60s 缓存
        self._provider_cache_lock = threading.Lock()  # 并发保护 _provider_cache
        self._provider_configs: dict = {}  # provider 配置（cloud_worker 启动时注入）

    @property
    def provider(self) -> SandboxProvider | None:
        """唯一的探测调用点。detect 出能力事实，manager 做选择。"""
        if not self._provider_resolved:
            from .detect import detect_capabilities
            caps = detect_capabilities(self._env)
            self._provider, self._template_id = self._select_provider(caps)
            self._provider_resolved = True
        return self._provider

    def reset_provider(self):
        """清除 Provider 缓存。测试 mock 环境后调用。"""
        self._provider = None
        self._provider_resolved = False

    def set_provider_configs(self, configs: dict) -> None:
        """注入 provider 配置（cloud_worker 拉取后调此方法）。"""
        self._provider_configs = configs

    def invalidate_provider_cache(self) -> None:
        """配置变更时清空 Provider 实例缓存，下次 prepare 用新配置创建。"""
        self._provider_cache.clear()

    def get_sandbox_mode(self, exec_backend: str, runtime_id: int | None = None) -> SandboxMode:
        """查 sandbox_mode + degrade_policy，转换为 SandboxMode。

        数据源按 worker 类型决定：
        - 本地 worker（WORKER_MODE=local）：查 DB engine_sandbox_config 表（有 DB 连接）
        - 远程 worker / 云 worker：查 ConfigSync（通过心跳同步的安全策略，无 DB 连接）
        """
        if exec_backend in ("chat",):
            return SandboxMode.DISABLED

        # 先检查 ConfigSync 紧急关闭开关（所有 worker 都检查）
        from src.domains.runtime.agents.sandbox.config_sync import get_config_sync
        _cs = get_config_sync()
        _agent_sandbox = _cs.get("agent_sandbox").lower()
        if _agent_sandbox in ("off", "disabled", "false", "0", "no"):
            return SandboxMode.DISABLED

        # 判断 worker 类型：本地 worker 查 DB，远程/云 worker 查 ConfigSync
        import os as _os
        _worker_mode = _os.environ.get("WORKER_MODE", "").lower()
        if not _worker_mode:
            # 兼容旧逻辑：未设 WORKER_MODE 时从 RUNTIME_SERVER_URL 判断
            _rts = _os.environ.get("RUNTIME_SERVER_URL", "")
            _is_local = ("localhost" in _rts) or ("127.0.0.1" in _rts) or _rts.startswith("127.")
        else:
            _is_local = (_worker_mode == "local")

        if _is_local and runtime_id is not None:
            # 本地 worker：查 DB（有 DB 连接 + 有 runtime_id）
            try:
                if self._repo is None:
                    from .repository import SandboxRepository
                    self._repo = SandboxRepository()
                mode_str, degrade_str = self._repo.query_sandbox_config(runtime_id, exec_backend)
                if mode_str:
                    if mode_str == "disabled":
                        return SandboxMode.DISABLED
                    if mode_str == "required":
                        return SandboxMode.REQUIRED
                    if degrade_str == "strict":
                        return SandboxMode.REQUIRED
                    return SandboxMode.PREFERRED
            except Exception as e:
                log.warning(f"get_sandbox_mode DB error (defaulting to REQUIRED): {e}")
            return SandboxMode.REQUIRED

        # 远程 worker / 云 worker / 本地无 runtime_id：查 ConfigSync
        env_mode = _cs.get("sandbox_mode").lower()
        if env_mode in ("disabled", "off"):
            return SandboxMode.DISABLED
        if env_mode in ("preferred", "optional"):
            return SandboxMode.PREFERRED
        return SandboxMode.REQUIRED

    def prepare(
        self, exec_backend: str, cwd: str, ctx: SandboxContext,
        state_reporter: Callable | None = None,
        llm_env: dict[str, str] | None = None,
        server_env: dict[str, str] | None = None,
    ) -> Sandbox:
        """准备沙箱。fail-closed 逻辑集中在此。

        上下文从 ctx 参数传入（不查 DB），state_reporter / llm_env 由调用方注入。
        返回值永远是 Sandbox 对象（NullSandbox 或具体实现），不会 None。
        """
        # 断言关键字段——空 ctx 不允许继续（本机直连兼容路径无 task_id 时记 warning，不阻断）
        if not ctx.task_id:
            log.warning("prepare: ctx.task_id is empty (local direct mode?), proceeding with caution")

        # 1. 查 sandbox_mode（按 WORKER_MODE 判断数据源，不再有 TODO）
        mode = self.get_sandbox_mode(exec_backend, ctx.runtime_id or None)
        if mode == SandboxMode.DISABLED:
            return self._create_null("sandbox_mode=disabled")

        # 2. 沙箱开关——从 ConfigSync 读引擎维度开关（未配的引擎 fallback 到全局）
        # ConfigSync 合并规则：远程 > 上次已知良好 > 安全默认
        from src.domains.runtime.agents.sandbox.config_sync import get_config_sync
        _cs = get_config_sync()
        _engine_sandbox = _cs.get_engine_sandbox(exec_backend).lower()
        if _engine_sandbox not in ("1", "true", "on", "yes"):
            log.warning(
                f"sandbox disabled by config (engine_sandbox={_engine_sandbox} "
                f"backend={exec_backend}, mode={mode.value})")
            # 关闭写审计——事后可追溯"哪段时间跑在无沙箱模式"
            self._audit.log(AuditEvent(
                ts=self._now(), task_id=ctx.task_id,
                kind=AuditKind.POLICY_DECISION, outcome="degrade",
                detail={"reason": f"engine_sandbox=off (engine={exec_backend})",
                        "backend": exec_backend, "mode": mode.value}))
            return self._create_null(f"engine_sandbox=off ({exec_backend})")

        # 3. 解析 Provider——从 ctx.sandbox_provider 选，fallback bwrap
        provider, _template_id = self._select_provider_from_context(ctx, exec_backend)

        # 4. fail-closed 决策
        if provider is None or not provider.is_available():
            if mode == SandboxMode.REQUIRED:
                self._audit.log(AuditEvent(
                    ts=self._now(), task_id=ctx.task_id,
                    kind=AuditKind.POLICY_DECISION, outcome="deny",
                    detail={"reason": "provider unavailable, mode=required"}))
                raise SandboxUnavailableError(
                    f"沙箱不可用，sandbox_mode=required 拒绝执行")
            # PREFERRED 降级——写审计，事后可追溯
            self._audit.log(AuditEvent(
                ts=self._now(), task_id=ctx.task_id,
                kind=AuditKind.POLICY_DECISION, outcome="degrade",
                detail={"reason": "bwrap unavailable, mode=preferred",
                        "backend": exec_backend}))
            log.warning(
                f"sandbox degraded: provider unavailable "
                f"(backend={exec_backend}, mode=preferred)")
            return self._create_null("provider unavailable")

        # 5. engine_type——从 ctx 取，fallback 推断（记 warning）
        _engine_type = ctx.engine_type
        if not _engine_type and exec_backend:
            from src.domains.runtime.engine.agent import backend_for_engine
            _engine_type = backend_for_engine(exec_backend)
            if _engine_type:
                log.warning(f"prepare: ctx.engine_type empty, fallback to {_engine_type}")
            else:
                log.error(f"prepare: backend_for_engine returned empty for '{exec_backend}', engine_type unknown")

        # 打印 ctx 用于验证
        log.info(f"prepare: ctx task_id={ctx.task_id} issue_id={ctx.issue_id} repo_key={ctx.repo_key} "
                 f"user_id={ctx.user_id} engine_type={_engine_type} provider={ctx.sandbox_provider or 'bwrap'}")

        # 6. Provider 创建沙箱实例（传完整上下文 + template_id + state_reporter + engine_type）
        sandbox = provider.create_sandbox(
            cwd=cwd, exec_backend=exec_backend, task_id=ctx.task_id,
            runtime_id=ctx.runtime_id or None,
            issue_id=ctx.issue_id, repo_key=ctx.repo_key, user_id=ctx.user_id,
            template_id=_template_id, engine_type=_engine_type,
            state_reporter=state_reporter,
            skill_name=ctx.skill_name, skill_id=ctx.skill_id,
        )
        if llm_env:
            sandbox.set_llm_env(llm_env)
        # [egress 首通修复 2026-11-16] 有效 server URL 写入建箱 env——_build_egress_network
        # 第 1 优先级从 _init_envs.GRAPE_SERVER_URL 推断 allow_out（内网 IP 形态必须显式
        # 放行；公网域名走平台 SNI 代理不需要）。_init_envs 是 CubeSandbox 专属属性
        # （Sandbox Protocol 不声明）——getattr 鸭子判定，bwrap/NullSandbox 安全跳过。
        if server_env:
            _init_envs = getattr(sandbox, "_init_envs", None)
            if isinstance(_init_envs, dict):
                _init_envs.update(server_env)
        return sandbox

    def _select_provider_from_context(self, ctx: SandboxContext, exec_backend: str) -> tuple[SandboxProvider | None, str]:
        """从 ctx 选 provider，fallback bwrap。不查 DB。

        优先级：
        1. ctx.provider_id 索引 _provider_configs → platform → registry 匹配
           （provider_id=0 时 fallback 到旧逻辑 ctx.sandbox_provider）
        2. bwrap（本地可用，template_id=""）
        3. None → fail-closed 报错
        """
        # 1. 云沙箱——优先用 provider_id 索引
        _provider_id = ctx.provider_id
        if _provider_id or ctx.sandbox_provider:
            # provider_id=0 时 fallback 到旧逻辑
            if _provider_id:
                _cfg = self._provider_configs.get(_provider_id, {})
                _platform = _cfg.get("platform", "") or ctx.platform or self._get_default_cloud_platform() or ""
                _cache_key = f"pid:{_provider_id}"
            else:
                _cfg = self._provider_configs.get(ctx.sandbox_provider, {})
                _platform = ctx.sandbox_provider
                _cache_key = ctx.sandbox_provider

            # 遗漏-2 修复：60s 缓存 Provider 实例 + health 结果，避免高并发 health check 风暴
            # 加锁防止并发 miss 创建多余 Provider 实例（DevBox health check 连接泄漏）
            with self._provider_cache_lock:
                _cached = self._provider_cache.get(_cache_key)
                _now = time.time()
                if _cached and (_now - _cached[1] < 60):
                    return (_cached[0], ctx.template_id)

                factory = self._registry.get(_platform)
                if factory:
                    provider = factory(audit=self._audit, credential=self._credential, seccomp=self._seccomp, alert_engine=self._alert_engine, config=_cfg)
                    # 检查 is_configured + health
                    if hasattr(provider, 'is_configured') and hasattr(provider, 'health'):
                        if provider.is_configured() and provider.health().healthy:
                            self._provider_cache[_cache_key] = (provider, _now)
                            return (provider, ctx.template_id)
                        else:
                            log.warning(f"cloud provider '{_platform}' (provider_id={_provider_id}) not available (configured/health)")
                    else:
                        if provider.is_available():
                            self._provider_cache[_cache_key] = (provider, _now)
                            return (provider, ctx.template_id)
                else:
                    log.error(
                        f"cloud provider '{_platform}' not in registry "
                        f"(registered={list(self._registry.keys())}), will fallback to bwrap"
                    )

        # 2. bwrap
        from .detect import detect_capabilities
        caps = detect_capabilities(self._env)
        if caps.get("bwrap_available"):
            factory = self._registry["bwrap"]
            return (factory(audit=self._audit, credential=self._credential, seccomp=self._seccomp, alert_engine=self._alert_engine), "")

        # 3. 都不可用 → None
        return (None, "")

    def _select_provider(self, caps: dict, exec_backend: str = "", runtime_id: int | None = None) -> tuple[SandboxProvider | None, str]:
        """@deprecated 旧方法——prepare() 不再调用此方法。保留供测试/外部引用。
        新代码用 _select_provider_from_context。
        """
        # 1. 云沙箱
        cloud_provider, template_id = self._select_cloud_provider(caps, exec_backend=exec_backend, runtime_id=runtime_id)
        if cloud_provider is not None:
            return (cloud_provider, template_id)

        # 2. bwrap
        if caps.get("bwrap_available"):
            factory = self._registry["bwrap"]
            return (factory(audit=self._audit, credential=self._credential, seccomp=self._seccomp, alert_engine=self._alert_engine), "")

        # 3. 显式允许裸跑 → NullProvider（安全兜底：detect_capabilities 从不设
        #    allow_unsaved=True，此分支当前不触发。保留给未来紧急逃生开关用）
        if caps.get("allow_unsandboxed"):
            factory = self._registry.get("null")
            if factory:
                return (factory(audit=self._audit, credential=self._credential, seccomp=self._seccomp, alert_engine=self._alert_engine), "")

        # 4. 都不可用 → None
        return (None, "")

    def _select_cloud_provider(self, caps: dict, exec_backend: str = "", runtime_id: int | None = None) -> tuple[SandboxProvider | None, str]:
        """@deprecated 旧方法——prepare() 不再调用此方法（改用 _select_provider_from_context）。
        保留供 _select_provider 旧路径和测试引用。

        从 sandbox_templates 表按 engine_name 查模板。

        返回 (provider, template_id)——provider 是 SDK 实例，template_id 传给 create_sandbox。
        模板路由：engine_name → sandbox_templates → provider + template_id。

        三级 fallback：
        1. DB sandbox_templates（按 engine_name 匹配）
        2. engine_sandbox_config（旧表，兼容）
        3. 配置文件
        """
        # 懒加载缓存
        cache_key = f"{runtime_id or 0}:{exec_backend}"
        if not hasattr(self, '_cloud_provider_cache'):
            self._cloud_provider_cache = {}  # type: dict
            self._cloud_provider_cache_time = {}  # type: dict

        import time as _time
        now = _time.time()
        cached = self._cloud_provider_cache.get(cache_key)
        cached_time = self._cloud_provider_cache_time.get(cache_key, 0.0)
        if cached and (now - cached_time) < 60:
            return cached

        # 1. 查 sandbox_templates 表——按 engine_name 匹配（通过 Repository DAO）
        template_id = ""
        provider_name = ""
        try:
            if self._repo is None:
                from .repository import SandboxRepository
                self._repo = SandboxRepository()
            template_id, provider_name = self._repo.query_sandbox_template(exec_backend, runtime_id)
        except Exception:
            pass

        if not provider_name:
            self._cloud_provider_cache[cache_key] = (None, "")
            self._cloud_provider_cache_time[cache_key] = now
            return (None, "")

        factory = self._registry.get(provider_name)
        if not factory:
            log.error(f"cloud provider '{provider_name}' not in registry (registered={list(self._registry.keys())})")
            self._cloud_provider_cache[cache_key] = (None, "")
            self._cloud_provider_cache_time[cache_key] = now
            return (None, "")

        provider = factory(audit=self._audit, credential=self._credential, seccomp=self._seccomp, alert_engine=self._alert_engine)

        # 检查 is_configured + health
        if hasattr(provider, 'is_configured') and hasattr(provider, 'health'):
            if provider.is_configured() and provider.health().healthy:
                result = (provider, template_id)
                self._cloud_provider_cache[cache_key] = result
                self._cloud_provider_cache_time[cache_key] = now
                return result
            else:
                log.warning(f"cloud provider '{provider_name}' not available")
        else:
            if provider.is_available():
                result = (provider, template_id)
                self._cloud_provider_cache[cache_key] = result
                self._cloud_provider_cache_time[cache_key] = now
                return result

        self._cloud_provider_cache[cache_key] = (None, "")
        self._cloud_provider_cache_time[cache_key] = now
        return (None, "")

    def _create_null(self, reason: str):
        from .backends.null import NullSandbox
        return NullSandbox(reason=reason)

    @staticmethod
    def _default_registry():
        from .backends import default_registry
        return default_registry()

    @staticmethod
    def _get_default_cloud_platform():
        from .backends import get_default_cloud_platform
        return get_default_cloud_platform()

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
