"""Agent 侧插件协商端点（Issue #1090 插件市场 C2 / D1-a）。

任务执行中的 Agent（携带 GRAPE_SESSION_TOKEN，session_token 作用域）调用：
- POST /api/plugins/agent/report-missing：缺插件报告 + 建议安装项 → 统一确认单
  队列（B3 契约 / #1089 plugin_install_requests，source=agent_report）；队列未
  就绪时降级回执 comment 通道文本，均不阻塞任务核心链路（200 返回）。
- GET /api/plugins/agent/installed：查询当前可用插件能力清单（补装后同任务
  刷新，绕过 TTL 缓存反映最新物化状态）。

Issue #4081（契约漂移可观测化，过渡期语义）：
- 上报结果计入 `grape_plugin_missing_report_total{result=submitted|degraded}`，
  缺端点/降级不再只靠翻 nginx/应用日志（deploy/alerts.yml 两条规则据此告警）；
- 命名空间 `/api/plugins/agent/*` 下**本构建未实现**的路径/方法命中路由级兜底，
  返回 `501 + error_code=NOT_IMPLEMENTED`（不再裸 404/405：GET 落 SPA catch-all
  得 404、POST 得 405，调用侧无法区分「平台不支持」与「暂时故障」），并计入
  `grape_plugin_agent_unimplemented_total{method}`。兜底只覆盖本命名空间，
  不改全局 404 语义；
- 能力位经 `GET /api/plugins/agent/capabilities` 对外announce（见
  `src/api/openapi.py: plugin_agent_capabilities`），调用侧先查后调。放在本命名空间而非
  `/api/ping`，因为 nginx 把 ping 指向 worker 进程（那里没有本 router）。

作用域约束：session_token 请求的 repo_key/agent_type 取自 token 本身，不接受
越库上报；JWT/API Key 请求需显式携带 repo_key。
"""
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
from src.api.errors import (ERROR_NOT_IMPLEMENTED, NOT_IMPLEMENTED, api_error)

logger = logging.getLogger(__name__)

# 兜底路由的 WARNING 去重（Issue #4081 复审 Optional）：外部匿名扫描同一未登记路径
# 反复命中时只留一条 WARNING，后续降为 debug；计数照旧全量进指标，不丢可观测性。
_UNIMPLEMENTED_WARNED: set = set()
_UNIMPLEMENTED_WARN_CAP = 256

router = APIRouter(prefix="/api/plugins/agent", tags=["plugins"])

# 上报结果标签（与 deploy/alerts.yml 的 GrapePluginMissingReportDegraded 口径一致）
RESULT_SUBMITTED = "submitted"
RESULT_DEGRADED = "degraded"


class MissingPluginItem(BaseModel):
    name: str = ""
    reason: str = ""
    version: str = ""


class ReportMissingBody(BaseModel):
    missing: list[MissingPluginItem] = []
    task_ref: str = ""
    repo_key: str = ""       # 仅非 session_token 认证生效（session 作用域以 token 为准）
    agent_type: str = ""
    runtime_id: int = 0


def _resolve_scope(user: dict, body_repo_key: str = "", body_agent_type: str = "") -> tuple:
    """作用域解析：session_token 以 token 为准（防越库）；其余取 body。

    非 session_token（JWT/API Key 显式带 repo_key）路径与相邻双作用域端点
    （agents/events/dashboard）鉴权惯例一致：先过 check_repo_access 归属校验
    （session_token/api_key 在其内部跳过——作用域已由 token 限定）。
    """
    if (user or {}).get("auth_type") == "session_token":
        return (user.get("repo_key") or "", user.get("agent_type") or "")
    repo_key = body_repo_key or ""
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    return (repo_key, body_agent_type or "")


@router.post("/report-missing", operation_id="plugins_agent_report_missing",
             summary="Report missing plugins (agent negotiation)",
             description=(
                 "D1-a missing-plugin negotiation for the executing agent: reports required "
                 "but uninstalled plugins with suggested install items. Enqueues into the "
                 "unified plugin confirmation queue (source=agent_report); degrades to a "
                 "comment-channel payload when the queue is unavailable. Always 200 - never "
                 "blocks the task core path. Session-token requests are repo/agent scoped by "
                 "the token itself."))
def report_missing(body: ReportMissingBody, user: dict = Depends(get_current_user_or_api_key)):
    from src.domains.insight.observability import PLUGIN_MISSING_REPORT
    from src.domains.runtime.engine.plugin_state import collect_plugin_capabilities
    from src.domains.runtime.engine.plugin_negotiation import (
        build_missing_plugin_report, build_install_request, submit_install_request,
        render_negotiation_comment)

    repo_key, agent_type = _resolve_scope(user, body.repo_key, body.agent_type)
    required = [{"name": m.name, "reason": m.reason, "version": m.version}
                for m in body.missing if (m.name or "").strip()]
    installed = collect_plugin_capabilities(repo_key, runtime_id=body.runtime_id)
    report = build_missing_plugin_report(required, installed=installed, task_ref=body.task_ref)
    request = build_install_request(report, task_ref=body.task_ref)
    submit = submit_install_request(request, repo_key=repo_key, agent_type=agent_type,
                                    runtime_id=body.runtime_id)
    out = {"report": report, "request": request, "submit": submit,
           "repo_key": repo_key, "agent_type": agent_type}
    result = RESULT_SUBMITTED if submit.get("submitted") else RESULT_DEGRADED
    # Issue #4081：上报结果可观测（此前只有 debug 日志，值守无从发现降级）
    PLUGIN_MISSING_REPORT.labels(result=result).inc()
    if not submit.get("submitted"):
        # 降级 comment 通道：返回可见协商文本，Agent 回帖/写入报告，用户确认后走补装
        out["degraded_comment"] = render_negotiation_comment(report, request)
        logger.warning(
            "plugin report-missing degraded (result=%s channel=%s task_ref=%s repo_key=%s "
            "agent_type=%s missing=%d)",
            result, submit.get("channel"), body.task_ref, repo_key, agent_type,
            len(report.get("missing") or []))
    return out


@router.get("/installed", operation_id="plugins_agent_installed",
            summary="Current installed plugin capabilities (agent refresh)",
            description=(
                 "Returns the currently available plugin capability list (B5-materialized "
                 "state, degrade-first). Used by the agent to refresh capabilities within "
                 "the same task after a confirmed install; bypasses the TTL cache to reflect "
                 "the latest materialized state. Session-token requests are repo-scoped by "
                 "the token itself."))
def installed_plugins(repo_key: str = "", runtime_id: int = 0, scene_id: str = "",
                      user: dict = Depends(get_current_user_or_api_key)):
    from src.domains.runtime.engine.plugin_state import (
        collect_plugin_capabilities, invalidate_plugin_cache)
    repo_key, _agent_type = _resolve_scope(user, repo_key)
    # 同任务恢复刷新：绕过 TTL 缓存，读最新物化状态
    invalidate_plugin_cache()
    plugins = collect_plugin_capabilities(repo_key, runtime_id=runtime_id, scene_id=scene_id)
    return {"repo_key": repo_key, "plugins": plugins, "count": len(plugins)}


# ===== Issue #4081：能力位（调用侧「先查后调」，确定性降级）=====
# 为什么在本命名空间而不是 /api/ping：nginx 的 `location = /api/ping` 指向 worker 进程
# （deploy/nginx-grape-cluster.conf），那里没挂插件代理路由，会把「可用」误报成
# 「不可用」；`/api/plugins/*` 走 `location /` → mgmt，才是真回答这个问题的进程。
# `include_in_schema=False`：不改变 /openapi.json 端点计数（无需契约重定基）。
@router.get("/capabilities", include_in_schema=False)
def plugin_agent_capability(request: Request):
    """插件代理能力位：两端点在本实例是否已注册（`operationId` + method 匹配）。

    返回 `{available, installed, report_missing, missing_operations[]}`；数据源 =
    `contracts/required_operations.json` × `app.routes`（见
    `src/api/openapi.py: plugin_agent_capabilities`），30s 缓存。
    旧构建没有本路由 → 调用侧拿 404，按「平台不支持插件代理」降级（不重试）。

    鉴权：本探针**有意不加认证依赖**（#4081 复审已确认口径）——只暴露「本构建是否
    实现这两个操作」的布尔位与 operationId/method/path 三元组，不含任何仓库、凭据或
    业务数据；调用侧常在缺凭据的早期阶段就要先判断支持面。若后续要暴露更多信息，
    必须重新评估鉴权。
    """
    from src.api.openapi import plugin_agent_capabilities
    return plugin_agent_capabilities(request.app)


# ===== Issue #4081：过渡期语义——本构建未实现的插件代理端点返回 501 =====
# 必须是本文件的最后一条路由：已登记端点先匹配，兜底只接住「路径/方法未登记」的请求。
# 只覆盖 /api/plugins/agent/* 命名空间，不改全局 404（SPA catch-all）语义。

# 方法集合必须覆盖客户端可能用来「探测端点是否存在」的全部动词（含 HEAD/OPTIONS）：
# 漏掉的动词仍会拿裸 405，与「调用侧按 404/405/501 单一判定」的承诺留缝（复审 Optional）。
# CORS 预检由 CORSMiddleware 在路由前拦截，不会落到这里。
_UNIMPLEMENTED_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@router.api_route("/{rest:path}", methods=_UNIMPLEMENTED_METHODS,
                  include_in_schema=False)
def unimplemented_plugin_agent_operation(rest: str, request: Request):
    """命名空间兜底：未登记的路径/方法 → `501 + error_code=NOT_IMPLEMENTED`。

    背景（Issue #4081 生产实测）：旧构建没有本 router 时，GET 落 SPA catch-all 返回
    裸 404、POST 由 Starlette 返回裸 405，调用侧无法区分「平台不支持插件代理」与
    「服务暂时故障」，于是每台 runtime 反复静默失败。这里把「路径前缀已知但端点
    未实现」显式化为 501，并计入 `grape_plugin_agent_unimplemented_total{method}`。
    """
    from src.domains.insight.observability import PLUGIN_AGENT_UNIMPLEMENTED
    method = (request.method or "").upper()
    path = request.url.path
    PLUGIN_AGENT_UNIMPLEMENTED.labels(method=method).inc()
    key = (method, path)
    if key not in _UNIMPLEMENTED_WARNED and len(_UNIMPLEMENTED_WARNED) < _UNIMPLEMENTED_WARN_CAP:
        _UNIMPLEMENTED_WARNED.add(key)
        logger.warning(
            "plugin agent endpoint not implemented (method=%s path=%s) -> 501 NOT_IMPLEMENTED",
            method, path)
    else:
        logger.debug(
            "plugin agent endpoint not implemented (method=%s path=%s) -> 501 NOT_IMPLEMENTED",
            method, path)
    raise api_error(
        NOT_IMPLEMENTED,
        (f"Plugin agent endpoint {method} {path} is not implemented by this server build. "
         "Query GET /api/plugins/agent/capabilities for capability before calling; treat "
         "404/405/501 as 'plugin agent unsupported' and do not retry."),
        error_code=ERROR_NOT_IMPLEMENTED)
