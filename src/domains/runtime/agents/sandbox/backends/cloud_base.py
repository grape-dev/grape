"""云沙箱公共基类——生命周期管理、kill 模板方法、health 缓存。

零 DB 依赖——cloud_base 不管部署在 A 机器还是 B 机器都不直接查 DB。
状态上报通过 state_reporter 回调注入：
- A 机器（manager.prepare）：传 DB 版回调，直接写 grape_t_cloud_sandboxes 表
- B 机器（cloud_worker）：传 HTTP 版回调，调 report_sandbox_status API
- state_reporter=None：不上报（debugging 用）

LLM 凭据统一从外部注入 _llm_env，不在基类查 DB。

每环节新建模式（2026-09-25 确认）：
- __enter__: 直接 create + init，不复用已有沙箱
- __exit__: 调 cleanup() 关 SDK 连接，不 kill 远端沙箱
- kill 由 cloud_worker finally 统一处理

_init_sandbox 编排（v4 重构——模板方法 + 能力声明 + EnginePlugin）：
- 基类编排固定流程：check_environment → extract_llm_cred → install → grape-cli → configure → clone
- 引擎安装/配置按 engine_type 策略分派（engine_strategies.py）
- 平台差异通过 PlatformAdapter 能力声明吸收（supports_long_cmd / needs_entry_point_wrapper）
- cred 进 ctx（ctx.llm_cred），install/configure 签名统一
- inject_env_var 通用辅助，不在 PlatformAdapter Protocol 上

平台子类只需实现：
- _do_create() → 平台 SDK create
- _do_connect(sandbox_id) → 平台 SDK connect（idle reaper kill 用）
- _do_kill() → 平台 SDK kill
- _do_run_command(...) → 平台 SDK commands.run（适配层）
- _do_write_file(...) → 平台 SDK filesystem.write
- _do_read_file(...) → 平台 SDK filesystem.read
- _do_refresh() → 平台 SDK refresh/set_timeout
- _do_is_running() → 平台 SDK is_running
- _do_reconnect() → 平台 SDK reconnect
- PlatformAdapter 原语：run_cmd / run_long_cmd / write_file / get_home / check_environment
- 能力声明：supports_long_cmd / needs_entry_point_wrapper
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterator

# ---- USP ExecSession 协议（docs/sandbox/usp-sandbox-protocol-spec.md §2）----
# 语义锚点 E2B CommandHandle：commands 与 pty 返回统一 handle 形态。
# 核心代码（executor/hermes_acp）只依赖本协议，平台 SDK 类型不出 backends。


class ExecSession:
    """执行会话契约——PTY 型会话的协议基类。

    消费面与既有 PtyHandle 一一对应（send_stdin/迭代/wait/kill/pid），
    迁移期 mock/fake 只换类型不改行为。

    wait() 契约：**永不抛平台 SDK 异常**——平台实现的 SDK 异常退出码
    提取（executor 时代 `exit (?:status|code) (\\d+)` 正则）在各自实现内
    消化，契约只承诺返回 int（异常不可解析时 -1）。

    close_stdin 契约：平台差异吸收——cube SDK PtyHandle 无此方法（no-op）、
    devbox CloseStdin 可能 500（容错吞掉）。实现不得向调用方抛错。
    """

    def send_stdin(self, data) -> None:
        raise NotImplementedError

    def close_stdin(self) -> None:
        raise NotImplementedError

    def __iter__(self) -> Iterator[bytes]:
        raise NotImplementedError

    def wait(self, timeout=None) -> int:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    @property
    def pid(self) -> int:
        raise NotImplementedError


from ..types import (
    ExecutionMode, Capability, CredentialMode, CommandResult,
    ExecHandle, HealthStatus, UnsupportedCapability, SandboxState,
)
from .engine_strategies import SandboxContext, get_engine_plugin

log = logging.getLogger(__name__)

# health 缓存 TTL（秒）——健康时缓存 30s，不健康时缓存 5s（快速重试）
_HEALTH_TTL_HEALTHY = 30
_HEALTH_TTL_UNHEALTHY = 5


class CloudSandboxBase:
    """云沙箱公共逻辑——生命周期管理、refresh 定时器、kill 清理。

    零 DB 依赖——状态上报通过 state_reporter 回调，LLM 凭据从 _llm_env 读取。
    不继承 Protocol——靠结构匹配 CloudSandbox Protocol。
    平台子类继承此基类，实现 _do_* 抽象方法。
    """

    execution_mode = ExecutionMode.CLOUD_SDK

    def __init__(
        self,
        *,
        issue_id: str,
        repo_key: str,
        provider_name: str,
        platform: str = "",
        user_id: int = 0,
        engine_type: str = "",
        state_reporter: Callable | None = None,
        provider_id: "int | str" = 0,
    ):
        self.issue_id = issue_id
        self.repo_key = repo_key
        self.provider_name = provider_name
        # [沙箱平台列显示] provider_id 随上报传给服务端——grape_t_cloud_sandboxes.provider_id
        # 落库后列表 JOIN 凭据名显示（跟随改名），不再依赖建箱时快照
        self.provider_id = provider_id
        self.platform = platform or provider_name
        self.user_id = user_id
        self._engine_type = engine_type
        self._sandbox: Any = None              # SDK 沙箱实例（子类在 _do_create/_do_connect 时赋值）
        self._sandbox_id: str | None = None
        self._credentials: dict[str, str] = {}     # bind_credential 存的凭证（ENVS 模式）
        self._init_envs: dict[str, str] = {}        # executor 探测变量（_HERMES_VENV_PYTHON 等）
        self._state_reporter = state_reporter       # 状态上报回调（DB 版或 HTTP 版）
        self._llm_env: dict[str, str] = {}          # LLM 凭据（外部注入，不从 DB 查）
        self._ctx: SandboxContext | None = None     # _make_context 时构造
        self._repo_url: str = ""                     # 仓库 URL（configure_repo 设置）
        self._bot_token: str = ""                    # bot token（configure_repo 设置）
        self._work_branch: str = ""                  # 工作分支（configure_repo 设置）
        self.state = SandboxState.ACTIVE            # Protocol 契约——云沙箱创建后即为 ACTIVE
        self._killed = False                        # BUG-C 修复：kill 后 exit 不报告 running

    # ---- 状态上报 ----

    def _report_state(self, *, state: str, last_refresh_at: str = "", agent_installed: int | None = None):
        """通过 state_reporter 回调上报沙箱状态。无 reporter 时 no-op。"""
        if self._state_reporter is None:
            return
        try:
            self._state_reporter(
                state=state,
                sandbox_id=self._sandbox_id or "",
                issue_id=self.issue_id,
                repo_key=self.repo_key,
                provider=self.provider_name,
                provider_id=self.provider_id,
                last_refresh_at=last_refresh_at,
                agent_installed=agent_installed,
            )
        except Exception as e:
            log.warning(f"[{self.provider_name}] _report_state failed: {e}")

    def _post_create_hook(self) -> None:
        """SDK 对象创建后的平台特定初始化钩子。子类 override。"""
        pass

    # ---- 上下文管理 ----

    def __enter__(self) -> "CloudSandboxBase":
        """每环节新建——直接 create + init，不复用已有沙箱。"""
        log.info(f"[{self.provider_name}] creating new sandbox for issue {self.issue_id}")
        self._sandbox_id = self._do_create()
        self._report_state(state="creating")
        self._post_create_hook()
        # 初始化沙箱（DNS 修复、安装 agent、配置凭据）——子类实现
        # 失败时 kill 沙箱 + 标记 killed，防止泄漏
        try:
            self._init_sandbox()
        except Exception as e:
            log.error(f"[{self.provider_name}] _init_sandbox failed: {e}, killing sandbox")
            try:
                self._do_kill()
            except Exception:
                pass
            self._killed = True  # BUG-2 修复：标记 killed 防止 exit 覆盖为 running
            self._report_state(state="killed")
            raise
        self._report_state(state="running", agent_installed=1)
        return self

    def __exit__(self, *exc) -> None:
        """每环节新建——exit 调 cleanup() 关 SDK 连接，不 kill 远端沙箱。

        kill 由 cloud_worker finally 统一处理。
        BUG-C 修复：kill 后不再报告 state="running"（覆盖 killed 状态）。
        """
        if self._sandbox_id and not self._killed:
            self._report_state(state="running", last_refresh_at=_now_iso())
        self.cleanup()  # 关 SDK 连接，不 kill 远端沙箱

    # ---- 凭证绑定 ----

    def bind_credential(self, name: str, *, value: str | None = None, vault_ref: str | None = None):
        """绑定凭证到沙箱。

        ENVS 模式（DevBox 一期）：value 存到 _credentials，run_command() 时作为 env 注入。
        VAULT 模式（CubeSandbox）：vault_ref 调平台 API，子类 override 此方法。
        """
        if value is not None:
            self._credentials[name] = value
        elif vault_ref is not None:
            # VAULT 模式——子类应 override 处理
            raise NotImplementedError(f"{self.provider_name} does not support vault_ref credential binding")

    def set_llm_env(self, env: dict[str, str]) -> None:
        """注入 LLM 凭据环境变量。

        外部调用方（cloud_worker 从 /env API 取、manager.prepare 从 DB 查）调此方法。
        _init_sandbox 从 self._llm_env 读取，不直接查 DB。
        """
        self._llm_env = dict(env)

    # ---- 公共属性 ----

    @property
    def sandbox_id(self) -> str:
        if not self._sandbox_id:
            raise RuntimeError("sandbox not entered yet")
        return self._sandbox_id

    @property
    def capabilities(self) -> set[Capability]:
        """默认能力集——子类可 override 增减。"""
        return {Capability.STREAMING_STDOUT, Capability.INTERACTIVE_STDIN}

    @property
    def credential_mode(self) -> CredentialMode:
        """默认 ENVS 模式——子类可 override 为 VAULT。"""
        return CredentialMode.ENVS

    def supports_pty(self) -> bool:
        """声明此沙箱是否支持 PTY 交互。默认 False，CubeSandbox override 为 True。

        用于 _run_cloud_sandbox 判断是否走 PTY 分支。
        显式能力声明，替代 hasattr(sandbox._sandbox, 'pty') 隐式探测。
        """
        return False

    # ---- USP ExecSession 契约（docs/sandbox/usp-sandbox-protocol-spec.md §2）----

    def pty_create(self, rows: int = 50, cols: int = 200,
                   cwd=None, envs=None, timeout=None) -> "ExecSession":
        """创建 PTY 型 ExecSession。

        supports_pty()=False 的平台调用即 RuntimeError——显式拒绝而非
        hasattr 隐式探测。平台 SDK 类型（如 PtySize）封在各自实现内，
        核心代码只依赖 ExecSession 协议。
        """
        raise RuntimeError(f"{type(self).__name__} does not support PTY")

    # ---- 模板方法：子类必须实现 ----

    def _do_create(self) -> str:
        """平台 SDK create——返回 sandbox_id。"""
        raise NotImplementedError

    def _do_connect(self, sandbox_id: str) -> None:
        """平台 SDK connect——连接已有沙箱。"""
        raise NotImplementedError

    def _do_kill(self) -> bool:
        """平台 SDK kill——销毁沙箱。"""
        raise NotImplementedError

    def _do_refresh(self) -> None:
        """平台 SDK refresh——续期。"""
        raise NotImplementedError

    def _do_is_running(self) -> bool:
        """平台 SDK is_running——轻量存活检测。"""
        raise NotImplementedError

    def _do_reconnect(self) -> None:
        """平台 SDK reconnect——断线重连。"""
        raise NotImplementedError

    @property
    def engine_type(self) -> str:
        return self._engine_type

    # ---- PlatformAdapter 能力声明（子类 override）----

    @property
    def supports_long_cmd(self) -> bool:
        """是否支持长时间命令（gRPC deadline 适配等）。默认 False。"""
        return False

    @property
    def needs_entry_point_wrapper(self) -> bool:
        """是否需要手动创建 entry_point wrapper（pip --target 不生成时）。默认 False。"""
        return False

    # ---- PlatformAdapter 原语（子类实现）----

    def run_cmd(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        """执行命令，返回 (exit_code, stdout)。子类实现。"""
        raise NotImplementedError

    def run_long_cmd(self, cmd: str, timeout: int) -> tuple[int, str]:
        """执行长时间命令。supports_long_cmd=False 时 fallback 到 run_cmd（传 timeout）。"""
        return self.run_cmd(cmd, timeout=timeout)

    def write_file(self, path: str, content: str | bytes) -> None:
        """写文件到沙箱。子类实现。"""
        raise NotImplementedError

    def get_home(self) -> str:
        """获取沙箱 $HOME 路径。子类实现。"""
        raise NotImplementedError

    def check_environment(self) -> None:
        """平台环境检测。子类 override。CubeSandbox 预装环境默认 pass。"""
        pass

    # ---- SandboxContext 构造 ----

    def _make_context(self) -> SandboxContext:
        """构造策略操作的 context——打包平台原语 + 元数据。"""
        return SandboxContext(
            name=self.provider_name,
            platform=self.platform,
            supports_long_cmd=self.supports_long_cmd,
            needs_entry_point_wrapper=self.needs_entry_point_wrapper,
            run_cmd=self.run_cmd,
            run_long_cmd=self.run_long_cmd,
            write_file=self.write_file,
            get_home=self.get_home,
            check_environment=self.check_environment,
            repo_url=self._repo_url,
            bot_token=self._bot_token,
            work_branch=self._work_branch,
        )

    @property
    def ctx(self) -> SandboxContext:
        """惰性构造 SandboxContext。"""
        if self._ctx is None:
            self._ctx = self._make_context()
        return self._ctx

    # ---- _init_sandbox 编排 ----

    def _init_sandbox(self) -> None:
        """编排引擎初始化——固定流程，策略分派按 engine_type。

        平台子类不再 override 此方法，改为提供 PlatformAdapter 原语。
        引擎安装/配置策略从 ENGINE_REGISTRY 按 engine_type 分派。
        """
        if not self._sandbox:
            raise RuntimeError("sandbox not created")

        ctx = self.ctx

        # 1. 平台环境检测
        ctx.check_environment()

        # 2. LLM cred 写进 ctx
        ctx.llm_cred = self._extract_llm_cred()

        # 3. 引擎安装（策略分派）
        plugin = get_engine_plugin(self._engine_type)
        if plugin:
            plugin.installer.install(ctx)
            log.info(f"[{self.provider_name}] engine '{self._engine_type}' installed for {self._sandbox_id}")
        else:
            log.warning(f"[{self.provider_name}] no installer for engine_type='{self._engine_type}', skipping install")

        # 4. 引擎配置（策略分派）
        if plugin:
            plugin.configurator.configure(ctx)
            log.info(f"[{self.provider_name}] engine config applied for {self._sandbox_id}")

        # 5. 同步 ctx.env_vars 到沙箱 _credentials
        self._sync_env_vars()

    def _extract_llm_cred(self) -> dict | None:
        """从 _llm_env 提取 LLM cred dict。通用步骤，只写一次。"""
        _llm = self._llm_env or {}
        if _llm.get("_LLM_API_KEY"):
            return {
                "provider": _llm.get("_LLM_PROVIDER", ""),
                "api_key": _llm.get("_LLM_API_KEY", ""),
                "base_url": _llm.get("_LLM_BASE_URL", ""),
                "model": _llm.get("_LLM_MODEL", ""),
            }
        return None

    def _sync_env_vars(self) -> None:
        """把 ctx.inject_env_var 注入的环境变量同步到沙箱 _credentials 和 _init_envs。

        _credentials: 执行时作为环境变量注入引擎进程（_safe_env.update(_credentials)）。
        _init_envs: executor 从此读取 _HERMES_VENV_PYTHON 等探测变量。
        两个都需要写入，否则 executor 读不到 _post_install 探测的路径。
        """
        for k, v in self.ctx.env_vars.items():
            self._credentials[k] = v
            self._init_envs[k] = v

    # ---- 公共方法（委托给子类）----

    def is_running(self) -> bool:
        return self._do_is_running()

    def reconnect(self) -> None:
        self._do_reconnect()

    def refresh(self, duration: int = 300) -> None:
        """续期。成功=沙箱存活且续期。抛异常=沙箱已死。"""
        self._do_refresh()

    def kill(self) -> bool:
        result = self._do_kill()
        if result:
            self._killed = True  # BUG-C 修复：标记已 kill，exit 不再报告 running
            self._report_state(state="killed")
        else:
            # P2-6 修复：kill 失败时显式报告 state=running，确保 idle reaper 能扫到
            self._report_state(state="running")
        return result

    def send_stdin(self, data: str | bytes) -> None:
        raise NotImplementedError(f"{self.provider_name} send_stdin not implemented")

    def close_stdin(self) -> None:
        raise NotImplementedError(f"{self.provider_name} close_stdin not implemented")

    # write_file 已移到 PlatformAdapter 原语区域（上方 run_cmd / write_file / get_home）

    def read_file(self, path: str) -> str:
        raise NotImplementedError(f"{self.provider_name} read_file not implemented")

    def list_files(self, path: str) -> list:
        raise NotImplementedError(f"{self.provider_name} list_files not implemented")

    def run_command(self, cmd: str, merge_credentials: bool = True, **kwargs) -> ExecHandle:
        """在沙箱内执行命令。子类 _do_run_command 实现平台适配。

        merge_credentials=True（默认）：合并 bind_credential 绑定的凭证到 envs（主命令需要）。
        merge_credentials=False：不合并（辅助命令如 mkfifo/cat/kill 不需要 LLM Key）。
        """
        envs = dict(kwargs.pop("envs", None) or {})
        if merge_credentials:
            envs.update(self._credentials)
        kwargs["envs"] = envs
        return self._do_run_command(cmd, **kwargs)

    def _do_run_command(self, cmd: str, **kwargs) -> ExecHandle:
        raise NotImplementedError(f"{self.provider_name} _do_run_command not implemented")

    # ---- 仓库配置（通用接口）----

    def configure_repo(self, *, repo_url: str, bot_token: str,
                       work_branch: str) -> None:
        """配置仓库信息 + clone。子类 override 补 grape-cli 配置 + git clone。
        executor 调此方法，不直接设私有属性。
        只管仓库通用三要素：repo_url / bot_token / work_branch。
        grape-cli server_url / token 由子类构造时从 config 设置。
        """
        self._repo_url = repo_url
        self._bot_token = bot_token
        self._work_branch = work_branch

    @property
    def repo_dir(self) -> str:
        """沙箱内仓库目录（cwd 用）。子类 override 返回平台路径。"""
        return ""

    @property
    def skill_dir(self) -> str:
        """沙箱内 skill 目录（prompt 替换用）。子类 override 返回平台路径。"""
        return ""

    def cleanup(self) -> None:
        """释放本地资源（SDK 连接）。远程沙箱继续跑。"""
        # 子类可 override 关 SDK 连接

    def is_active(self) -> bool:
        return self._sandbox_id is not None and self._do_is_running()


class CloudProviderBase:
    """云沙箱 Provider 公共逻辑——health 缓存。

    子类实现：
    - is_configured() → 检查配置
    - _do_health_check() → 平台 API ping（轻量 /health 端点）
    - create_sandbox() → 创建 CloudSandbox 实例
    - connect_sandbox() → 从 sandbox_id 恢复
    - kill_sandbox() → 销毁沙箱
    """

    execution_mode = ExecutionMode.CLOUD_SDK

    def __init__(self, *, audit=None, credential=None, alert_engine=None):
        self._audit = audit
        self._credential = credential
        self._alert_engine = alert_engine
        self._health_cache: HealthStatus | None = None
        self._health_cache_time: float = 0.0

    def _audit_external_call(self, op: str, task_id: str, outcome: str,
                             duration_ms: int, detail: dict | None = None) -> None:
        """外部平台调用审计（arch-guard F6/C2）：把 create/connect/kill 等外部调用
        写入注入的 audit 链——调用方、操作、结果、耗时。best-effort：审计失败
        不影响主流程，仅告警。"""
        if not self._audit:
            return
        from src.domains.runtime.agents.common.audit import AuditEvent, AuditKind, now_iso
        try:
            self._audit.log(AuditEvent(
                ts=now_iso(), task_id=task_id or "",
                kind=AuditKind.SANDBOX_ACCESS, outcome=outcome,
                detail={"op": op, "duration_ms": duration_ms, **(detail or {})}))
        except Exception as e:
            log.warning(f"[cloud] external call audit write failed (op={op}): {e}")

    def is_available(self) -> bool:
        """组合判断：is_configured AND health.healthy。"""
        return self.is_configured() and self.health().healthy

    def health(self) -> HealthStatus:
        """返回缓存的健康状态，不阻塞。

        健康时缓存 30s，不健康时缓存 5s（快速重试）。
        首次调用同步 ping 一次，之后惰性刷新。
        """
        now = time.time()
        if self._health_cache:
            ttl = _HEALTH_TTL_HEALTHY if self._health_cache.healthy else _HEALTH_TTL_UNHEALTHY
            if (now - self._health_cache_time) < ttl:
                return self._health_cache

        # 缓存过期或首次——同步 ping
        try:
            healthy = self._do_health_check()
            self._health_cache = HealthStatus(healthy=healthy, last_check=now, reason="")
        except Exception as e:
            log.warning(f"health check failed: {e}")
            self._health_cache = HealthStatus(healthy=False, last_check=now, reason=str(e))

        self._health_cache_time = now
        return self._health_cache

    def _do_health_check(self) -> bool:
        """平台 API ping——子类实现。超时 1-2s。"""
        raise NotImplementedError

    def is_configured(self) -> bool:
        """配置是否完整——子类实现。"""
        raise NotImplementedError

    def isolation_level(self):
        from ..types import IsolationLevel
        return IsolationLevel.MICROVM

    def create_sandbox(self, cwd="", exec_backend="", task_id="", runtime_id=None, template_id="", **kwargs):
        """创建云沙箱——子类实现 _do_create。

        template_id 从 grape_t_sandbox_templates 表查出来传入（模板路由）。
        空字符串表示用配置文件的默认 template_id（向后兼容）。
        state_reporter 通过 kwargs 传入（调用方决定 DB 版或 HTTP 版）。
        """
        raise NotImplementedError

    def connect_sandbox(self, sandbox_id: str, issue_id: str = "", repo_key: str = ""):
        """从已有 sandbox_id 恢复连接——子类实现。

        issue_id/repo_key 可选传入供 state_reporter 使用。
        """
        raise NotImplementedError

    def kill_sandbox(self, sandbox_id: str) -> bool:
        """通用 kill——先查状态再 kill。子类实现 _do_kill_sandbox(sandbox_id)。

        已不存在的沙箱直接返回 True（标 killed），避免反复 kill 失败。
        子类的 _do_kill_sandbox 负责：connect + is_running + kill。
        """
        if not hasattr(self, '_do_kill_sandbox'):
            # 子类没实现 _do_kill_sandbox，回退到旧逻辑
            return self._legacy_kill_sandbox(sandbox_id)
        try:
            result = self._do_kill_sandbox(sandbox_id)
            log.info(f"[{self.provider_name}] kill_sandbox: {sandbox_id} result={result}")
            return result
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "404" in err_str or "does not exist" in err_str:
                log.info(f"[{self.provider_name}] kill_sandbox: {sandbox_id} already gone (not found), marking as killed")
                return True
            log.error(f"[{self.provider_name}] kill_sandbox {sandbox_id} failed: {e}")
            return False

    def _legacy_kill_sandbox(self, sandbox_id: str) -> bool:
        """旧逻辑回退——子类未实现 _do_kill_sandbox 时用。"""
        raise NotImplementedError(f"{self.provider_name} must implement _do_kill_sandbox or kill_sandbox")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
