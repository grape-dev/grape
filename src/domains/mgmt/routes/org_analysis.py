"""组织仓库 AI 分析与输出分支相关 API。"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.api.deps import require_no_session_token, require_org_api_user
from src.domains.mgmt import org_analysis as service
from src.domains.mgmt.routes.org_repos import (
    _error,
    _require_admin,
    _require_member,
    _require_team_org,
)

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


def _require_org_actor(user: dict, org_id: str) -> None:
    """JWT/API Key 走成员校验；worker session token 按其 repo 作用域校验组织归属。

    Autopilot 分析任务用 session token 回调；token 的 repo_key 是组织仓库，
    只要该仓库属于目标组织即可代表组织级分析任务，避免要求 user_id 恰好是成员。
    """
    if str(user.get("auth_type") or "") == "session_token":
        rk = str(user.get("repo_key") or "")
        if not rk:
            raise _error(403, "PERMISSION_DENIED", "session token has no repository scope")
        from src.domains.mgmt.user.org_db import list_org_repos

        if not any(str(r.get("repo_key") or "") == rk for r in list_org_repos(org_id) or []):
            raise _error(403, "PERMISSION_DENIED", "session token repository is not linked to this organization")
        return
    _require_member(user["id"], org_id)


class AIResultBody(BaseModel):
    result: dict = Field(default_factory=dict)
    engine_id: int = 0
    source_commit: str = ""


class RunAnalysisBody(BaseModel):
    mode: str = "all"
    repo_key: str = ""


@router.get("/{org_id}/repo-analysis/targets",
            operation_id="org_repo_analysis_targets",
            summary="List AI analysis target repositories")
def targets(org_id: str, mode: str = "all", repo_key: str = "",
            user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_org_actor(user, org_id)
    return {"org_id": org_id, "mode": mode,
            "repos": service.analysis_targets(org_id, mode=mode, repo_key=repo_key)}


@router.get("/{org_id}/repo-analysis/context/{repo_key:path}",
            operation_id="org_repo_analysis_context",
            summary="Build AI analysis context for one repository")
def context(org_id: str, repo_key: str,
            user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_org_actor(user, org_id)
    data = service.context_for_repo(org_id, repo_key)
    if not data:
        raise _error(404, "REPO_NOT_IN_ORG", "Repository is not linked to this organization")
    return data


@router.post("/{org_id}/repo-map/{repo_key:path}/ai-result",
             operation_id="org_repo_map_ai_result",
             summary="Save AI repository analysis result")
def save_ai_result(org_id: str, repo_key: str, body: AIResultBody,
                   user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_org_actor(user, org_id)
    result = body.result
    if not isinstance(result, dict) or not result:
        raise _error(400, "INVALID_RESULT", "result JSON object is required")
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw.encode("utf-8")) > 256 * 1024:
        raise _error(400, "RESULT_TOO_LARGE", "analysis result exceeds 256KB")
    result_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    saved = service.save_result(
        org_id, repo_key, result,
        engine_id=int(body.engine_id or 0),
        source_commit=str(body.source_commit or ""),
        triggered_by="worker",
        result_hash=result_hash,
    )
    if not saved:
        raise _error(404, "REPO_MAP_NOT_FOUND", "Repository map target not found")
    return {"ok": True, "repo_key": repo_key, "confidence": saved.get("confidence"),
            "analyzed_at": saved.get("analyzed_at"), "result_hash": result_hash}


@router.get("/{org_id}/repo-analysis/docs",
            operation_id="org_repo_analysis_docs",
            summary="Read AI analysis documents for dispatch")
def docs(org_id: str, user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_org_actor(user, org_id)
    return service.analysis_docs(org_id)


@router.post("/{org_id}/repo-analysis/run",
             operation_id="org_repo_analysis_run",
             summary="Trigger AI analysis (manual)")
def run_analysis(org_id: str, body: RunAnalysisBody,
                 user: dict = Depends(require_no_session_token)):
    org = _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    mode = (body.mode or "all").strip().lower()
    if mode == "single":
        if not body.repo_key:
            raise _error(400, "INVALID_TARGET", "repo_key is required for single mode")
        return service.request_repo_analysis(
            org_id, body.repo_key, trigger="manual", actor_id=user["id"])
    return service.request_all_analysis(org_id, trigger="manual", actor_id=user["id"])


@router.get("/{org_id}/repo-analysis/status",
            operation_id="org_repo_analysis_status",
            summary="Get AI analysis status")
def status(org_id: str, repo_key: str = "",
           user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_org_actor(user, org_id)
    if repo_key:
        return {"repo_key": repo_key, "status": service.status_for_repo(org_id, repo_key)}
    return {"org_id": org_id, "automation": service.automation_task(org_id)}


@router.post("/{org_id}/repo-analysis/automation/ensure",
             operation_id="org_repo_analysis_automation_ensure",
             summary="Ensure the daily analysis automation task")
def ensure_automation(org_id: str, user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    return service.ensure_analysis_automation(org_id, actor_id=user["id"])
