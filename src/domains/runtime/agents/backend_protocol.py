"""引擎后端 Protocol 定义——小而专注的能力声明。

不建一个大 Protocol 塞所有职责。按职责拆成小 Protocol，executor 按需 isinstance 查询。
AgentRuntime 基类提供所有 Protocol 的默认实现，继承的子类自动满足全部 Protocol。
非 AgentRuntime 的 backend（如 HermesACPBackend）只实现它真正需要的 Protocol。

设计决策：
- 用 @runtime_checkable + isinstance 检查能力，不用 hasattr——语义清晰、可测试、早发现
- Protocol 是"能力声明"不是"必须实现的清单"——没实现的走 fallback
- 加新能力时加新 Protocol，不往现有 Protocol 加方法——保持稳定契约
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentBackend(Protocol):
    """最小接口——所有 backend 必须满足。"""

    name: str
    binary: str

    def build_remote_invocation(
        self, prompt: str, session_id: str = ""
    ) -> tuple[list[str] | str, str | None]: ...


@runtime_checkable
class ProtocolAware(Protocol):
    """执行协议声明——ACP（JSON-RPC 握手）或 NDJSON（CLI --format json）。

    hermes 走 ACP（initialize → session/prompt），opencode/codearts/codex 走 NDJSON。
    默认 "ndjson"（AgentRuntime 基类）。
    """

    @property
    def protocol(self) -> str: ...  # "acp" 或 "ndjson"


@runtime_checkable
class CloudBackendProvider(Protocol):
    """云沙箱 backend 工厂——bwrap 和云沙箱可能用不同 backend。

    hermes: bwrap 用 HermesRuntime，云沙箱用 HermesACPBackend。
    其他引擎: 默认返回 self（同一 backend 适用于两条路径）。
    """

    def cloud_backend(self) -> AgentBackend: ...


@runtime_checkable
class PTYAware(Protocol):
    """PTY 能力声明——CubeSandbox PTY 路径用。

    wrapper 文件名、公共模块依赖、stdin_payload 需求、env 注入。
    不声明（返回 None）的引擎不走 PTY（走非 PTY 的 run_command 路径）。
    """

    def pty_wrapper_name(self) -> str | None: ...
    def pty_use_ndjson_common(self) -> bool: ...
    def pty_needs_stdin_payload(self) -> bool: ...
    def pty_prepare_env(self, env: dict, session_id: str) -> None: ...


@runtime_checkable
class SessionStaleDetector(Protocol):
    """session 失效检测——opencode ≥1.17 对失效 session 静默 EOF。

    done dict 包含 exit_code / text / stderr / parsed_count。
    只有序列化后的 opencode 有此行为，其他引擎默认不检测。
    """

    def detect_session_stale(self, done: dict) -> bool: ...
