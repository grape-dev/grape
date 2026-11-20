"""沙箱核心 Protocol 定义（仅 Protocol，零内部依赖）。

Sandbox: 沙箱实例——wrap 返回 list[str]，context manager 管生命周期。
SandboxProvider: 抽象工厂——创建沙箱实例。

状态机：
- __enter__()：PREPARED → 启动组合件（失败抛异常）
- wrap()：纯命令构造，可多次调用（每次重新组装）
- cleanup()：清理组合件，不关外部依赖
- CLEANED 后 wrap() → RuntimeError
- CLEANED 后 cleanup() → 静默返回（幂等）

云沙箱扩展（继承 Sandbox）：
- CloudSandbox: 云沙箱实例——run_command 调 SDK，不走本地 Popen
- CloudSandboxProvider: 云沙箱工厂——create_sandbox + connect_sandbox

凭证绑定与命令执行解耦：
- bind_credential() 绑定凭证，run_command() 不接收凭证参数（一期过渡 envs 仍可用）
- CredentialMode 声明安全级别：ENVS（agent 可见）/ VAULT（不进沙箱）/ PROXY
"""
from __future__ import annotations
from typing import Any, Protocol, runtime_checkable, Callable, Iterator

from .types import (
    SandboxState, IsolationLevel, ExecutionMode, Capability,
    CredentialMode, CommandResult, ExecHandle, HealthStatus,
)


@runtime_checkable
class Sandbox(Protocol):
    """沙箱基类。所有沙箱实例至少有 execution_mode + 上下文管理。

    bwrap 特有的 wrap/popen_kwargs 在 LocalSandbox 子协议声明。
    云沙箱不需要 wrap/popen_kwargs（走 SDK run_command 而非本地 Popen）。

    不暴露 network / resource 属性——调用方不能干预内部生命周期。
    """

    execution_mode: ExecutionMode  # 基类要求，所有实现必须声明

    def cleanup(self) -> None:
        """释放本地资源（SDK 句柄/连接池/临时文件）。
        远程沙箱不受影响——继续运行。
        bwrap: NetworkProxy 停止 + seccomp 清理。
        云沙箱: 关 SDK 连接，远程沙箱继续跑。
        """
        ...

    def is_active(self) -> bool: ...
    def kill(self) -> bool:
        """销毁沙箱资源。
        CloudSandbox: kill 远端 VM（需要活的 SDK 连接，在 __exit__ 之前调）。
        BwrapSandbox: 空操作（bwrap 进程随子进程退出，cleanup 在 __exit__ 里）。
        NullSandbox: 空操作。
        返回 True=成功/无需操作，False=远端已不存在（幂等）。
        """
        ...
    def __enter__(self) -> "Sandbox": ...
    def __exit__(self, *exc) -> None:
        """bwrap: 调 cleanup()（释放本地资源）。
        云沙箱: 调 cleanup()（关 SDK 连接，不销毁——kill 由 cloud_worker finally 处理）。
        ⚠️ 云沙箱 with 退出后沙箱还活着！销毁请显式调用 kill()。
        """
        ...


@runtime_checkable
class LocalSandbox(Sandbox, Protocol):
    """本地沙箱（bwrap/null）——有 state + prepare_env/wrap/popen_kwargs 用于本地进程包装。"""

    state: SandboxState

    def prepare_env(self, env: dict[str, str], engine_backend: Any, resource_stack: Any = None) -> None:
        """准备引擎 per-task 环境（在 __enter__ 之后、wrap 之前调用一次）。

        BwrapSandbox：注入 per-task HOME + 调 engine_backend.prepare_task_env。
        NullSandbox：空操作（env 不被修改，HOME 保持宿主机路径）。
        """
        ...

    def wrap(self, cmd: list[str], proc_env: dict[str, str] | None = None) -> list[str]:
        """包装命令（含 bwrap 前缀 + sh 包装 + 原命令）。
        env 已烘进 bwrap --setenv 参数，调用方 Popen(cmd) 即可。
        纯构造，无副作用。可在 ACTIVE 状态多次调用。"""
        ...

    def popen_kwargs(self) -> dict:
        """返回 Popen 需要的额外参数（如 pass_fds）。
        无 seccomp 时返回 {}。
        有 seccomp 时返回 {"pass_fds": (fd,)}。
        """
        ...


@runtime_checkable
class CloudSandbox(Sandbox, Protocol):
    """云沙箱——execution_mode = CLOUD_SDK。

    不走本地 Popen，走 run_command() 调 SDK。
    生命周期 = issue（A2A 链路复用），不是 = agent 进程。
    DevBox 和 CubeSandbox 共用此契约，适配层补齐 SDK 差异。

    凭证绑定与命令执行解耦：bind_credential() 绑定凭证，run_command() 不接收凭证参数。
    """
    sandbox_id: str

    @property
    def capabilities(self) -> set[Capability]: ...

    @property
    def credential_mode(self) -> CredentialMode: ...

    def bind_credential(
        self,
        name: str,
        *,
        value: str | None = None,      # ENVS 模式用（DevBox 一期）
        vault_ref: str | None = None,  # VAULT 模式用（CubeSandbox）
    ) -> None:
        """绑定凭证到沙箱。run_command() 不再接收凭证参数。

        ENVS 模式：value 存到 sandbox 实例，run_command() 时作为 env 注入子进程。
        VAULT 模式：vault_ref 调平台 Vault API，凭证不进 worker 进程。
        """
        ...

    def set_llm_env(self, env: dict[str, str]) -> None:
        """注入 LLM 凭据环境变量。_init_sandbox 从此读取，不查 DB。"""
        ...

    def run_command(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,  # 一期过渡：DevBox 仍可用 envs 传凭证；三期 deprecated
        cwd: str | None = None,
        stdin: bool = False,
        timeout: float | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,  # 不支持 STREAMING_STDERR 时抛 UnsupportedCapability
        background: bool = False,       # 不支持 BACKGROUND 时抛 UnsupportedCapability
    ) -> ExecHandle:
        """在沙箱内执行命令，返回 ExecHandle（永远返回句柄，不返回 CommandResult）。

        background=False 时内部自动 wait() 并返回已完成句柄（exit_code 已填充）。
        """
        ...

    def send_stdin(self, data: str | bytes) -> None: ...
    def close_stdin(self) -> None: ...       # EOF 通知，远端知道输入结束

    def write_file(self, path: str, data: str | bytes) -> None: ...
    def read_file(self, path: str) -> str: ...
    def read_file_bytes(self, path: str) -> bytes: ...
    def file_exists(self, path: str) -> bool: ...
    def list_files(self, path: str) -> list: ...

    # 存活检测 + 续期
    def is_running(self) -> bool: ...         # 轻量存活检测
    def reconnect(self) -> None: ...          # 断线重连
    def refresh(self, duration: int = 300) -> None:
        """续期。成功=沙箱存活且续期。
        抛 SandboxNotFound/SandboxExpired=沙箱已死。
        """
        ...

    def kill(self) -> bool: ...               # 销毁远程沙箱
    def __enter__(self) -> "CloudSandbox": ...


@runtime_checkable
class SandboxProvider(Protocol):
    """沙箱 Provider——抽象工厂，创建沙箱实例。

    Provider 全局可复用（不绑定单个任务），只判断环境能力。
    每次任务调用 create_sandbox 创建独立的 Sandbox 实例。
    """

    name: str

    def is_available(self) -> bool:
        """微秒级检测，不联网不启动子进程。"""
        ...

    def isolation_level(self) -> IsolationLevel:
        """声明隔离级别。"""
        ...

    def create_sandbox(
        self, cwd: str, exec_backend: str, task_id: str = "",
        runtime_id: int | None = None,
        **kwargs,
    ) -> Sandbox:
        """创建沙箱实例。

        exec_backend 是执行引擎名（opencode/hermes/codex），用于选择白名单路径。
        不是 sandbox backend 名——Provider 自己知道名字。
        """
        ...


@runtime_checkable
class CloudSandboxProvider(SandboxProvider, Protocol):
    """云沙箱 Provider——继承 SandboxProvider，扩展云特有方法。

    与 SandboxProvider 的区别：
    - create_sandbox 返回 CloudSandbox，不是 Sandbox（协变）
    - 有 connect_sandbox——复用已有沙箱
    - 有 kill_sandbox——销毁沙箱（issue 关闭时由 event_pipeline 调用）
    - is_available 拆成 is_configured + health（缓存），不同步 ping
    - 暴露 execution_mode = CLOUD_SDK
    """

    name: str
    execution_mode: ExecutionMode  # = ExecutionMode.CLOUD_SDK

    def is_configured(self) -> bool:
        """配置是否完整。同步、本地、无网络。检查 API Key 是否填写、SDK 是否安装。"""
        ...

    def health(self) -> "HealthStatus":
        """平台可达性。返回缓存状态（TTL 30s），不阻塞。"""
        ...

    def is_available(self) -> bool:
        """组合判断：is_configured AND health.healthy。"""
        ...

    def isolation_level(self) -> IsolationLevel: ...

    def create_sandbox(
        self,
        cwd: str = "",
        exec_backend: str = "",
        task_id: str = "",
        runtime_id: int | None = None,
        *,
        issue_id: str = "",
        repo_key: str = "",
        template_id: str = "",
        envs: dict[str, str] | None = None,
        timeout: int = 3600,
    ) -> CloudSandbox:
        """创建云沙箱实例。

        兼容基类签名（cwd/exec_backend/task_id/runtime_id）+ 云特有关键字参数。
        基类参数在云沙箱场景可为空——云沙箱用 issue_id/repo_key 定位。
        """
        ...

    def connect_sandbox(self, sandbox_id: str) -> CloudSandbox:
        """从已有 sandbox_id 恢复连接（A2A 链路复用）。"""
        ...

    def kill_sandbox(self, sandbox_id: str) -> bool:
        """销毁沙箱（issue 关闭时由 event_pipeline 调用）。"""
        ...
