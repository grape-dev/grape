"""Organization demand cross-repo dispatch APIs (Issue #2112)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.deps import require_no_session_token, require_org_api_user
from src.kernel.issue_ident import native_issue_number
from src.domains.mgmt.org_dispatch import (
    cancel_dispatch,
    create_dispatch,
    get_dispatch_detail,
    list_dispatches,
    retry_target,
)
from src.domains.mgmt.routes.org_repos import (
    _error,
    _require_admin,
    _require_member,
    _require_team_org,
)

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class CreateDispatchBody(BaseModel):
    org_issue_number: str | int = ""
    targets: list[str] = Field(default_factory=list)
    context: dict | str | None = None
    title: str = ""
    description: str = ""
    scene_id: str = ""
    # Issue #2251：完成判定口径 issue_done（默认）/ pr_merged / task_terminal
    done_when: str = "issue_done"


@router.post("/{org_id}/dispatches", operation_id="org_dispatches_create",
             summary="Create organization cross-repo dispatch")
async def create(org_id: str, body: CreateDispatchBody,
                 user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    payload, status = await create_dispatch(
        org_id,
        org_issue_number=native_issue_number(body.org_issue_number),
        targets=list(body.targets or []),
        context=body.context or {},
        title=body.title or "",
        description=body.description or "",
        scene_id=body.scene_id or "",
        user=user,
        done_when=body.done_when or "issue_done",
    )
    return JSONResponse(status_code=status, content=payload)


@router.get("/{org_id}/dispatches", operation_id="org_dispatches_list",
            summary="List organization dispatches")
def list_items(org_id: str, limit: int = 50, offset: int = 0,
               org_issue_number: str = "",
               user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_member(user["id"], org_id)
    return list_dispatches(org_id, limit=limit, offset=offset,
                           org_issue_number=org_issue_number)


@router.get("/{org_id}/dispatches/{dispatch_id}/context",
            operation_id="org_dispatches_context",
            summary="Get dispatch context snapshot")
def context(org_id: str, dispatch_id: str,
            user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_member(user["id"], org_id)
    from src.domains.mgmt.org_context_snapshot import build_dispatch_context_bundle

    bundle = build_dispatch_context_bundle(org_id, dispatch_id)
    if not bundle:
        raise _error(404, "ORG_DISPATCH_NOT_FOUND", "Organization dispatch not found")
    return bundle


@router.get("/{org_id}/dispatches/{dispatch_id}", operation_id="org_dispatches_detail",
            summary="Get organization dispatch detail")
def detail(org_id: str, dispatch_id: str,
           user: dict = Depends(require_org_api_user)):
    _require_team_org(org_id)
    _require_member(user["id"], org_id)
    return get_dispatch_detail(org_id, dispatch_id)


@router.post("/{org_id}/dispatches/{dispatch_id}/cancel",
             operation_id="org_dispatches_cancel", summary="Cancel organization dispatch")
def cancel(org_id: str, dispatch_id: str,
           user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    return cancel_dispatch(org_id, dispatch_id, user=user)


@router.post("/{org_id}/dispatches/{dispatch_id}/targets/{repo_key:path}/retry",
             operation_id="org_dispatches_target_retry", summary="Retry failed dispatch target")
async def retry(org_id: str, dispatch_id: str, repo_key: str,
                user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    if not (repo_key or "").strip():
        raise _error(400, "INVALID_TARGET", "repo_key is required")
    return await retry_target(org_id, dispatch_id, repo_key, user=user)
