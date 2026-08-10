"""Agent Runtime 库：支持 opencode / codearts 等多 Agent CLI 执行引擎。

由 src/agent 演进而来（M0 术语重构，见 agent-runtime-design.md §6）。
实现核心在 registry.py / executor.py，base/opencode/codearts 为 runtime 命名实现。
"""
from .base import AgentRuntime
from .opencode import OpencodeRuntime
from .codearts import CodeArtsRuntime
from .registry import _RUNTIMES, register_runtime, get_runtime, discover_runtimes
from .executor import stream_runtime, stream_agent, stream_opencode, consume_stream_async
from .service import (
    enqueue_task, claim, get_task, update_task_status, cancel_task,
    report_messages, report_progress, complete_task, fail_task, submit_local,
    get_execution_mode, set_execution_mode,
    enqueue_async_report, start_report_ingestion_loop, wait_task_terminal,
)
