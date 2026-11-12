"""Agent Runtime 库：支持 opencode / codearts 等多 Agent CLI 执行引擎。

由 src/domains/runtime/agents 演进而来（M0 术语重构，见 agent-runtime-design.md §6）。
实现核心在 registry.py / executor.py，base/opencode/codearts 为 runtime 命名实现。

Issue #699：本包改为惰性导出（PEP 562 __getattr__）。原因：
`agents` 兼容包与 `engine` 包互相引用实现（agents.executor ↔ engine.executor/registry），
若在包初始化时 eager import，任一入口先初始化 agents 就会撞上
"partially initialized module" 循环导入。惰性化后 `from ...engine import X`
语义不变，但包初始化不再触发重依赖加载。
"""
import importlib

_LAZY = {
    # 执行引擎实现
    "AgentRuntime": (".base", "AgentRuntime"),
    "OpencodeRuntime": (".opencode", "OpencodeRuntime"),
    "CodeArtsRuntime": (".codearts", "CodeArtsRuntime"),
    "CodexRuntime": (".codex", "CodexRuntime"),
    "DshRuntime": (".dsh", "DshRuntime"),
    "AtomCodeRuntime": (".atomcode", "AtomCodeRuntime"),
    # 注册表
    "_RUNTIMES": (".registry", "_RUNTIMES"),
    "register_runtime": (".registry", "register_runtime"),
    "get_runtime": (".registry", "get_runtime"),
    "discover_runtimes": (".registry", "discover_runtimes"),
    # 流式执行
    "stream_runtime": (".executor", "stream_runtime"),
    "stream_agent": (".executor", "stream_agent"),
    "stream_opencode": (".executor", "stream_opencode"),
    "consume_stream_async": (".executor", "consume_stream_async"),
    # 任务队列 / 状态
    "enqueue_task": (".service", "enqueue_task"),
    "claim": (".service", "claim"),
    "get_task": (".service", "get_task"),
    "update_task_status": (".service", "update_task_status"),
    "cancel_task": (".service", "cancel_task"),
    "report_messages": (".service", "report_messages"),
    "report_progress": (".service", "report_progress"),
    "complete_task": (".service", "complete_task"),
    "fail_task": (".service", "fail_task"),
    "get_execution_mode": (".service", "get_execution_mode"),
    "set_execution_mode": (".service", "set_execution_mode"),
    "runtime_credential_id": (".service", "runtime_credential_id"),
    "runtime_execution_kind": (".service", "runtime_execution_kind"),
    "enqueue_async_report": (".service", "enqueue_async_report"),
    "start_report_ingestion_loop": (".service", "start_report_ingestion_loop"),
    "wait_task_terminal": (".service", "wait_task_terminal"),
    # 引擎选择
    "load_engine_selection": (".engine_select", "load_engine_selection"),
    "pick_engine": (".engine_select", "pick_engine"),
    "resolve_default_engine": (".engine_select", "resolve_default_engine"),
    "query_engines": (".engine_select", "query_engines"),
    "resolve_local_engine": (".engine_select", "resolve_local_engine"),
    "engine_name": (".engine_select", "engine_name"),
}


def __getattr__(name):
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr = spec
    mod = importlib.import_module(mod_name, __name__)
    value = getattr(mod, attr)
    globals()[name] = value  # 缓存，后续直接命中
    return value


def __dir__():
    return sorted(_LAZY)
