"""组织上下文（brief/policy/decision）HTTP 路由（Issue #2150）。

权限：成员读；写 draft = org owner/admin 或组织仓库 repo admin；
发布/废弃 = org owner/admin。发布自动降级旧 active（非 409）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import require_no_session_token
from src.domains.mgmt import org_context_store as store
from src.domains.mgmt.auth.audit import log_audit
from src.domains.mgmt.routes.org_repos import (
    _error,
    _permission_error,
    _require_admin,
    _require_member,
    _require_repo_maintainer,
)
from src.domains.mgmt.user.org_db import get_member_role, get_org

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class ContextBody(BaseModel):
    kind: str = "brief"
    key: str = ""
    title: str = ""
    content: str = ""
    owner_id: str = ""
    valid_until: str = ""


class ContextUpdateBody(BaseModel):
    title: str | None = None
    content: str | None = None
    owner_id: str | None = None
    valid_until: str | None = None


def _org_or_404(org_id: str) -> dict:
    org = get_org(org_id)
    if not org:
        raise _error(404, "ORG_NOT_FOUND", "Organization not found")
    return org


def _require_writer(user: dict, org_id: str) -> dict:
    """写 draft 口径：org owner/admin 或组织仓库 repo admin。"""
    org = _org_or_404(org_id)
    if get_member_role(user["id"], org_id) in ("owner", "admin"):
        return org
    repo_key = (org.get("org_repo_key") or "").strip()
    if repo_key:
        try:
            _require_repo_maintainer(user, repo_key)
            return org
        except HTTPException:
            pass
    raise _permission_error()


def _audit(event_type: str, user: dict, org_id: str, row: dict) -> None:
    try:
        from src.domains.mgmt.org_context_repo import schedule_sync
        schedule_sync(org_id)
    except Exception:
        pass
    log_audit(
        event_type,
        user_id=user["id"],
        operator_id=user["id"],
        detail={
            "org_id": org_id,
            "context_id": (row or {}).get("id"),
            "kind": (row or {}).get("kind"),
            "lifecycle_status": (row or {}).get("lifecycle_status"),
        },
    )


@router.get("/{org_id}/context", operation_id="org_context_list",
            summary="List organization context")
def list_context(org_id: str, kind: str = "", status: str = "",
                 limit: int = 200, offset: int = 0,
                 user: dict = Depends(require_no_session_token)):
    _require_member(user["id"], org_id)
    # 硬上限：历史 decision 会随批次增长，禁止 API 一次拉全量。
    limit = min(max(int(limit or 200), 1), 500)
    offset = max(0, int(offset or 0))
    return {
        "contexts": store.list_contexts(
            org_id, kind=kind, status=status, limit=limit, offset=offset),
        "limit": limit,
        "offset": offset,
    }


@router.post("/{org_id}/context", operation_id="org_context_create",
             summary="Create organization context (draft)")
def create_context(org_id: str, body: ContextBody,
                   user: dict = Depends(require_no_session_token)):
    _require_writer(user, org_id)
    ctx = store.create_context(
        org_id, body.kind, body.key, body.title, body.content,
        owner_id=body.owner_id, valid_until=body.valid_until, user_id=user["id"],
    )
    _audit("org_context_create", user, org_id, ctx)
    return ctx


@router.get("/{org_id}/context/{ctx_id}", operation_id="org_context_get",
            summary="Get organization context")
def get_context(org_id: str, ctx_id: str,
                user: dict = Depends(require_no_session_token)):
    _require_member(user["id"], org_id)
    ctx = store.get_context(org_id, ctx_id)
    if not ctx:
        raise _error(404, "CONTEXT_NOT_FOUND", "Organization context not found")
    return ctx


@router.put("/{org_id}/context/{ctx_id}", operation_id="org_context_update",
            summary="Update organization context")
def update_context(org_id: str, ctx_id: str, body: ContextUpdateBody,
                   user: dict = Depends(require_no_session_token)):
    _require_writer(user, org_id)
    ctx = store.update_context(
        org_id, ctx_id, title=body.title, content=body.content,
        owner_id=body.owner_id, valid_until=body.valid_until, user_id=user["id"],
    )
    if not ctx:
        raise _error(404, "CONTEXT_NOT_FOUND", "Organization context not found")
    _audit("org_context_update", user, org_id, ctx)
    return ctx


@router.post("/{org_id}/context/{ctx_id}/publish", operation_id="org_context_publish",
             summary="Publish organization context")
def publish_context(org_id: str, ctx_id: str,
                    user: dict = Depends(require_no_session_token)):
    _require_admin(user["id"], org_id)
    ctx = store.publish_context(org_id, ctx_id, user_id=user["id"])
    if not ctx:
        raise _error(404, "CONTEXT_NOT_FOUND", "Organization context not found")
    _audit("org_context_publish", user, org_id, ctx)
    return ctx


@router.post("/{org_id}/context/{ctx_id}/deprecate", operation_id="org_context_deprecate",
             summary="Deprecate organization context")
def deprecate_context(org_id: str, ctx_id: str,
                      user: dict = Depends(require_no_session_token)):
    _require_admin(user["id"], org_id)
    ctx = store.deprecate_context(org_id, ctx_id, user_id=user["id"])
    if not ctx:
        raise _error(404, "CONTEXT_NOT_FOUND", "Organization context not found")
    _audit("org_context_deprecate", user, org_id, ctx)
    return ctx


@router.delete("/{org_id}/context/{ctx_id}", operation_id="org_context_delete",
               summary="Delete organization context")
def delete_context(org_id: str, ctx_id: str,
                   user: dict = Depends(require_no_session_token)):
    _require_writer(user, org_id)
    if not store.delete_context(org_id, ctx_id):
        raise _error(404, "CONTEXT_NOT_FOUND", "Organization context not found")
    _audit("org_context_delete", user, org_id, {"id": ctx_id})
    return {"ok": True, "id": ctx_id}
