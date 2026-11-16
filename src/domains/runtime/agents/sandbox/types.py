"""沙箱类型定义（零依赖）。

枚举 + 值对象 + Protocol，不 import 任何 sandbox 子模块。
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Protocol


class SandboxState(Enum):
    """沙箱实例生命周期状态。"""
    PREPARED = "prepared"
    ACTIVE = "active"
    CLEANED = "cleaned"


class IsolationLevel(Enum):
    """隔离级别（从弱到强）。Provider 能力属性。"""
    PROCESS = "process"
    CONTAINER = "container"
    MICROVM = "microvm"


class SandboxMode(Enum):
    """沙箱模式——配置项，选后端。Manager 决策的结果。

    SandboxMode = 配置项（required/preferred/disabled）
    IsolationLevel = 能力属性（process/container/microvm）
    """
    REQUIRED = "required"
    PREFERRED = "preferred"
    DISABLED = "disabled"


class ExecutionMode(str, Enum):
    """执行模式——决定 executor 走哪条执行路径。

    executor 按 sandbox.execution_mode match/case 分叉，用 cast() 收窄类型。
    不用 isinstance，不用 is_cloud 布尔。
    """
    LOCAL_BWRAP = "local_bwrap"   # bwrap 进程级隔离，本地 Popen
    CLOUD_SDK = "cloud_sdk"       # 云沙箱 VM 级隔离，SDK 远程执行
    NONE = "none"                 # 无沙箱（仅 caps 显式 allow_unsandboxed=True 时选 NullSandbox）


class Capability(Enum):
    """云沙箱能力声明。不支持的能力显式抛 UnsupportedCapability，不静默降级。"""
    STREAMING_STDOUT = "streaming_stdout"    # 流式 stdout 回调
    STREAMING_STDERR = "streaming_stderr"    # stdout/stderr 分离（Pty 可能不支持）
    INTERACTIVE_STDIN = "interactive_stdin"  # stdin 管道
    BACKGROUND = "background"                # 后台异步执行
    FILE_WRITE = "file_write"                # 文件写入（files API）
    FILE_READ = "file_read"                  # 文件读取（files API）


class CredentialMode(str, Enum):
    """凭证注入模式——决定凭证安全级别。

    ENVS: 凭证以环境变量注入进程（agent 可见）
    VAULT: 凭证由平台代理注入出站请求（agent 不可见，不进沙箱）
    PROXY: 凭证由出站代理注入（目标态）
    """
    ENVS = "envs"
    VAULT = "vault"
    PROXY = "proxy"


class UnsupportedCapability(Exception):
    """调用方请求了沙箱不支持的能力。不静默降级，显式报错。"""


@dataclass
class HealthStatus:
    """平台健康状态——health() 返回的缓存值，不阻塞。"""
    healthy: bool
    last_check: float = 0.0  # 时间戳（秒）
    reason: str = ""         # 不健康时的原因


@dataclass
class CommandResult:
    """命令执行结果——ExecHandle.wait() 的返回值。"""
    exit_code: int
    stdout: str
    stderr: str
    duration: float = 0.0  # 执行耗时（秒）


class ExecHandle(Protocol):
    """执行句柄——统一本地进程和远程 SDK 会话。

    run_command() / _dispatch_execution() 永远返回 ExecHandle，
    不返回 CommandResult | ExecHandle 联合类型。
    CommandResult 是 handle.wait() 的结果。
    """
    def wait(self, timeout: float | None = None) -> CommandResult: ...
    def stream_stdout(self) -> Iterator[str]: ...
    def send_stdin(self, data: str | bytes) -> None: ...
    def close_stdin(self) -> None: ...
    def kill(self) -> None: ...
    @property
    def pid(self) -> int | None: ...
    @property
    def exit_code(self) -> int | None: ...


class AlertSeverity(Enum):
    """告警严重级别——只描述级别语义，规则映射在 _AlertConstants。"""
    CRITICAL = ("critical", 40)   # 不可逆破坏：rm -rf / / force push / DROP TABLE
    HIGH = ("high", 30)           # 高风险：force delete branch / hard reset / TRUNCATE / rm .env
    MEDIUM = ("medium", 20)       # 中风险：rollback / revert
    LOW = ("low", 10)             # 低风险：非标端口等需关注但不紧急
    INFO = ("info", 0)            # 仅审计不告警：close Issue / 配置漂移（默认关闭）

    def __new__(cls, label: str, weight: int):
        obj = object.__new__(cls)
        obj._value_ = label        # 决定 .value 和 AlertSeverity("critical") 反查
        obj.weight = weight        # type: ignore[attr-defined]
        return obj

    weight: int  # type hint for Pyright

    def __lt__(self, other):
        if self.__class__ is other.__class__:
            return self.weight < other.weight
        return NotImplemented


@dataclass
class WhitelistRule:
    """DB 行映射——五维白名单规则。加载后编译为 CompiledRule。"""
    id: int
    backend_name: str
    domain: str
    method: str
    path_prefix: str = "*"
    scheme: str = "https"
    port: int = 443


@dataclass(frozen=True)
class CompiledRule:
    """编译后的白名单规则——path_prefix 在加载时归一化一次。不可变。"""
    id: int
    domain: str            # 已 lower
    method: str            # 已 upper
    path_prefix_norm: str  # 已 normalize_path
    scheme: str            # 已 lower
    port: int
