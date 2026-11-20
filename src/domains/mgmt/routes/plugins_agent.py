"""Agent 侧插件协商端点（Issue #1090 插件市场 C2 / D1-a）。

任务执行中的 Agent（携带 GRAPE_SESSION_TOKEN，session_token 作用域）调用：
- POST /api/plugins/agent/report-missing：缺插件报告 + 建议安装项 → 统一确认单
  队列（B3 契约 / #1089 plugin_install_requests，source=agent_report）；队列未
  就绪时降级回执 comment 通道文本，均不阻塞任务核心链路（200 返回）。
- GET /api/plugins/agent/installed：查询当前可用插件能力清单（补装后同任务
  刷新，绕过 TTL 缓存反映最新物化状态）。

作用域约束：session_token 请求的 repo_key/agent_type 取自 token 本身，不接受
越库上报；JWT/API Key 请求需显式携带 repo_key。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key

router = APIRouter(prefix="/api/plugins/agent", tags=["plugins"])


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
    if not submit.get("submitted"):
        # 降级 comment 通道：返回可见协商文本，Agent 回帖/写入报告，用户确认后走补装
        out["degraded_comment"] = render_negotiation_comment(report, request)
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
