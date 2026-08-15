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
    report_messages, report_progress, complete_task, fail_task,
    get_execution_mode, set_execution_mode,
    runtime_credential_id, runtime_execution_kind,
    enqueue_async_report, start_report_ingestion_loop, wait_task_terminal,
)
from .engine_select import (
    load_engine_selection, get_binding, set_binding, clear_binding,
    pick_engine, resolve_default_engine, query_engines,
    resolve_local_engine, engine_name, backfill_existing_bindings,
)
