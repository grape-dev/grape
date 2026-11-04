"""Runtime 视图路由（A 进程专用，Issue #596 P1）。

三进程拆分（grape-three-process-split-design.md §4.1.2 / §4.2）后，
`runtime.py` 按 HTTP 方法维度拆分：

- 本文件 `runtime_view.py`：用户面查看端点（GET + 管理面 PUT 标签设置等），
  由 **A 进程（grape_server.py :38080）** 加载；
- `runtime.py`：Worker 协议端点（POST register/heartbeat/claim/report 等），
  由 **C 进程（grape_worker_server.py :38082）** 加载，同时保留在单进程
  `main.py` 模式下整体加载（`runtime.router` + `runtime_view.router` 一并
  include，行为不变——路由前缀相同、路径不重叠，FastAPI 依次注册即可）。

设计文档拆分原则（§4.2）：
- GET 端点（machine-health / tasks 列表 / install 脚本分发 / engine-onboarding
  / role-tags / scene-tags）→ 归 A（用户面查看 + 下载入口）；
- POST 端点（register / heartbeat / claim / start / complete / fail / requeue
  / skills-report / refresh-skills / recover-orphans）→ 归 C（Worker 执行态）；
- PUT 端点（tags / max-concurrent / scene-tags / alias 设置）是用户面管理操作
  → 归 A。

注意：GET /runtimes/{id}/tags、GET /tasks/{id}/env、GET /tasks/{id}/state、
GET /install/latest、GET /install/package 虽为 GET，但鉴权走 rta_ 凭据
（worker 轮询用），属于 Worker 协议数据面。为保证 worker 只打 C 端口即可完成
全部协议交互，这些「worker 数据面 GET」保留在 `runtime.py`（C 加载），
同时本文件通过 **路由复用（同一函数对象注册）** 的方式让 A 也提供——
`main.py` 单进程模式原本两端点合一，多进程模式 nginx 把 /api/runtime 的
worker 协议路径分流到 C。本文件仅注册用户面端点；worker 数据面 GET 由
`runtime.py` 的 router 提供，A 进程单进程兼容模式（main.py）同时 include
两个 router，无冲突。

实现方式：本文件不复制函数体——直接从 `runtime.py` **引用原函数**并在
`view_router` 上重新注册，保证单一实现源（改一处两端生效），
路径/operation_id 与原定义一致（FastAPI 依次注册，单进程模式下
`main.py` 只 include 本 router 或原 router 之一，避免 405 歧义）。
"""
import importlib

from fastapi import APIRouter

# 视图 router：A 进程加载（main.py 单进程模式同样加载本 router，
# runtime.py 的 router 在拆分后仅由 worker 入口与单进程模式加载）。
view_router = APIRouter(prefix="/api/runtime", tags=["runtime"])


def _register_runtime_views() -> None:
    """把 runtime.py 中的用户面端点注册到 view_router（单一实现源）。

    - 使用原函数对象注册（同一 def），不复制代码；
    - operation_id / summary / description 保持与原定义一致，
      OpenAPI 契约不变（§4.1.1：A 进程落盘 openapi.json 供前端）；
    - 注册顺序按 runtime.py 源码顺序，与原 include 顺序一致。
    """
    from src.api.routes import runtime as _rt

    # (method, path, function_name, kwargs) —— 与 runtime.py 原定义一一对应
    _view_endpoints = [
        ("get", "/admin/event-bus", "event_bus_debug", dict(
            operation_id="runtime_event_bus", summary="Event bus debug")),
        ("get", "/admin/machine-health", "machine_health_overview", dict(
            operation_id="runtime_machine_health",
            summary="Machine health overview",
            description=(
                "Machine health overview for all engines: {runtime_id, name, health_score, "
                "total_tasks, failed_tasks, consecutive_failures, failure_rate, "
                "health_abnormal, health_reason, last_failure_at, ...}. Issue #498: "
                "health_abnormal/health_reason expose the unified abnormality verdict "
                "(heartbeat_lost > consecutive_failures > low_score) with small-sample "
                "exemption and failure decay self-heal."))),
        ("get", "/admin/machine-health/{runtime_id}", "machine_health_detail", dict(
            operation_id="runtime_machine_health_detail",
            summary="Single runtime health detail",
            description=(
                "Health detail for a single engine. Issue #498: includes "
                "health_abnormal/health_reason (unified verdict), failed_tasks_effective "
                "(decayed-window failures) and last_failure_at for self-heal observability."))),
        ("get", "/tasks/{task_id}/status", "get_task_status", dict(
            operation_id="runtime_task_status",
            summary="Task status polling",
            description=(
                "Task status polling (chat front-end D5 typewriter fallback). Issue #539: "
                "also the authorized query path referenced by pipeline segment "
                "output.task_ref.query_path — returns full result_text for authorized "
                "callers; pipeline summaries only carry a redacted result_ref summary."))),
        ("get", "/runtimes/{runtime_id}/tasks", "list_runtime_tasks", dict(
            operation_id="runtime_tasks_list",
            summary="List runtime tasks",
            description="Tasks of a device (front-end device view).")),
        ("get", "/runtimes/{runtime_id}/sessions", "list_runtime_sessions", dict(
            operation_id="runtime_sessions_list",
            summary="List runtime sessions",
            description=(
                "Engine sessions filtered by agent_tasks.runtime_id; only sessions "
                "with matching tasks are returned."))),
        ("get", "/credentials/{cid}/tasks", "list_credential_tasks", dict(
            operation_id="runtime_credential_tasks",
            summary="Tasks aggregated by credential",
            description="Tasks aggregated by credential across all its engine instances.")),
        ("get", "/install", "runtime_install_script", {}),
        ("get", "/install.ps1", "runtime_install_script_ps1", {}),
        ("get", "/start-worker.ps1", "runtime_start_worker_script_ps1", {}),
        ("get", "/grape-worker.tar.gz", "runtime_worker_package_public", {}),
        ("get", "/engine-onboarding", "runtime_engine_onboarding", dict(
            operation_id="runtime_engine_onboarding",
            summary="Engine onboarding",
            description="Returns runtime/worker engine onboarding info for a repo.")),
        ("get", "/runtimes/{runtime_id}/role-tags", "get_runtime_tags", dict(
            operation_id="runtime_role_tags",
            summary="Runtime role tags",
            description="Role tags of an engine (web admin view).")),
        ("put", "/runtimes/{runtime_id}/tags", "set_runtime_tags", dict(
            operation_id="runtime_tags_set",
            summary="Set role tags",
            description=(
                "Sets an engine's role tags (mutual-exclusion applied). "
                "engine_capabilities is the single source; agent_tags is the derived cache."))),
        ("put", "/runtimes/{runtime_id}/max-concurrent", "set_runtime_max_concurrent", dict(
            operation_id="runtime_max_concurrent_set",
            summary="Set max concurrent",
            description="Sets the engine-level max concurrency.")),
        ("get", "/scene-tags", "list_scene_tags", dict(
            operation_id="runtime_scene_tags_list",
            summary="List scene tags",
            description=(
                "All scenes and their available role tags for the scene picker: "
                "[{name, display_name, roles: [...]}]."))),
        ("get", "/runtimes/{runtime_id}/scene-tags", "get_runtime_scene_tags", dict(
            operation_id="runtime_scene_tags_get",
            summary="Runtime scene tags",
            description="Scene tags of an engine.")),
        ("put", "/runtimes/{runtime_id}/scene-tags", "set_runtime_scene_tags", dict(
            operation_id="runtime_scene_tags_set",
            summary="Set runtime scene tags",
            description=(
                "Sets an engine's scene tags (metadata constraining selectable role tags)."))),
        ("put", "/runtimes/{runtime_id}/alias", "set_runtime_alias", dict(
            operation_id="runtime_alias_set",
            summary="Set runtime alias",
            description="Sets an engine alias; an empty string clears it.")),
    ]
    for method, path, func_name, kwargs in _view_endpoints:
        fn = getattr(_rt, func_name, None)
        if fn is None:
            # 原函数被重命名/删除时跳过（防御，正常不应发生）
            continue
        # APIRouter.get/put 装饰器：把原函数注册到 view_router（不改原函数）
        decorator = getattr(view_router, method)
        decorator(path, **kwargs)(fn)


_register_runtime_views()

# 向后兼容：旧导入 `from src.api.routes.runtime_view import router`
router = view_router
