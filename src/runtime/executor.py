"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体 runtime。

M0 术语重构：原 AgentBackend/stream_agent 更名 AgentRuntime/stream_runtime
（见 agent-runtime-design.md §6）。实现与注册表以 src/agent/executor.py 为单一事实源，
本模块为 runtime 命名入口 + 兼容别名，避免双注册表分裂。
"""
from typing import Generator, AsyncGenerator
from src.agent.executor import (
    _redact, _terminate_proc, _BACKENDS,
    stream_agent, stream_chat_agent, stream_devops_agent,
    stream_opencode, consume_stream_async,
    register_backend, get_backend, discover_backends,
    _track_proc, _untrack_proc, _active_procs, _active_procs_lock,
    terminate_active_agents,
)
from .registry import register_runtime, get_runtime, discover_runtimes
from .base import AgentRuntime

_RUNTIMES = _BACKENDS


def stream_runtime(*args, **kwargs) -> Generator:
    """流式执行 Agent Runtime（原名 stream_agent，yield 事件）。"""
    yield from stream_agent(*args, **kwargs)
