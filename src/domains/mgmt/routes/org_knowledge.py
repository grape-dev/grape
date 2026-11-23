"""组织知识库 HTTP 路由（Issue #2150）。

权限与组织上下文一致：成员读；写 draft = org owner/admin 或组织仓库 repo admin；
发布/废弃 = org owner/admin。列表支持 kind/tag/q/status 过滤。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.api.deps import require_no_session_token
from src.domains.mgmt import org_knowledge as kn
from src.domains.mgmt.auth.audit import log_audit
from src.domains.mgmt.routes.org_repos import _error, _require_admin, _require_member
from src.domains.mgmt.routes.org_context_store import _require_writer

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class KnowledgeBody(BaseModel):
    kind: str = ""
    name: str = ""
    description: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    applies_to: str = ""
    valid_until: str = ""
    review_interval_days: int = 0
    owner_id: str = ""


class KnowledgeUpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    applies_to: str | None = None
    valid_until: str | None = None
    review_interval_days: int | None = None
    owner_id: str | None = None


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
            "knowledge_id": (row or {}).get("id"),
            "kind": (row or {}).get("kind"),
            "lifecycle_status": (row or {}).get("lifecycle_status"),
        },
    )


@router.get("/{org_id}/knowledge", operation_id="org_knowledge_list",
            summary="List organization knowledge")
def list_knowledge(org_id: str, kind: str = "", tag: str = "", q: str = "",
                   status: str = "", limit: int = 200, offset: int = 0,
                   user: dict = Depends(require_no_session_token)):
    _require_member(user["id"], org_id)
    limit = min(max(int(limit or 200), 1), 500)
    offset = max(0, int(offset or 0))
    items = kn.list_knowledge(
        org_id, kind=kind, tag=tag, q=q, status=status,
        limit=limit, offset=offset)
    return {"knowledge": items, "limit": limit, "offset": offset}


@router.post("/{org_id}/knowledge", operation_id="org_knowledge_create",
             summary="Create organization knowledge (draft)")
def create_knowledge(org_id: str, body: KnowledgeBody,
                     user: dict = Depends(require_no_session_token)):
    org = _require_writer(user, org_id)
    repo_key = kn._resolve_repo_key(org, org_id)
    item = kn.create_knowledge(
        org_id, repo_key, kind=body.kind, name=body.name,
        description=body.description, content=body.content, tags=body.tags,
        applies_to=body.applies_to, valid_until=body.valid_until,
        review_interval_days=body.review_interval_days, owner_id=body.owner_id,
        created_by=str(user.get("username") or user["id"]),
    )
    _audit("org_knowledge_create", user, org_id, item)
    return item


@router.get("/{org_id}/knowledge/{knowledge_id}", operation_id="org_knowledge_get",
            summary="Get organization knowledge")
def get_knowledge(org_id: str, knowledge_id: int,
                  user: dict = Depends(require_no_session_token)):
    _require_member(user["id"], org_id)
    item = kn.get_knowledge(org_id, knowledge_id)
    if not item:
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    return {**item, "content": kn.read_knowledge_content(item)}


@router.put("/{org_id}/knowledge/{knowledge_id}", operation_id="org_knowledge_update",
            summary="Update organization knowledge")
def update_knowledge(org_id: str, knowledge_id: int, body: KnowledgeUpdateBody,
                     user: dict = Depends(require_no_session_token)):
    _require_writer(user, org_id)
    item = kn.update_knowledge(
        org_id, knowledge_id, name=body.name, description=body.description,
        content=body.content, tags=body.tags, applies_to=body.applies_to,
        valid_until=body.valid_until, review_interval_days=body.review_interval_days,
        owner_id=body.owner_id,
    )
    if not item:
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    _audit("org_knowledge_update", user, org_id, item)
    return item


@router.post("/{org_id}/knowledge/{knowledge_id}/publish",
             operation_id="org_knowledge_publish",
             summary="Publish organization knowledge")
def publish_knowledge(org_id: str, knowledge_id: int,
                      user: dict = Depends(require_no_session_token)):
    _require_admin(user["id"], org_id)
    item = kn.publish_knowledge(org_id, knowledge_id)
    if not item:
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    _audit("org_knowledge_publish", user, org_id, item)
    return item


@router.post("/{org_id}/knowledge/{knowledge_id}/deprecate",
             operation_id="org_knowledge_deprecate", summary="Deprecate organization knowledge")
def deprecate_knowledge(org_id: str, knowledge_id: int,
                        user: dict = Depends(require_no_session_token)):
    _require_admin(user["id"], org_id)
    item = kn.deprecate_knowledge(org_id, knowledge_id)
    if not item:
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    _audit("org_knowledge_deprecate", user, org_id, item)
    return item


@router.post("/{org_id}/knowledge/{knowledge_id}/archive",
             operation_id="org_knowledge_archive", summary="Archive organization knowledge")
def archive_knowledge(org_id: str, knowledge_id: int,
                      user: dict = Depends(require_no_session_token)):
    _require_admin(user["id"], org_id)
    item = kn.archive_knowledge(org_id, knowledge_id)
    if not item:
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    _audit("org_knowledge_archive", user, org_id, item)
    return item


@router.delete("/{org_id}/knowledge/{knowledge_id}", operation_id="org_knowledge_delete",
               summary="Delete organization knowledge")
def delete_knowledge(org_id: str, knowledge_id: int,
                     user: dict = Depends(require_no_session_token)):
    _require_writer(user, org_id)
    if not kn.delete_knowledge(org_id, knowledge_id):
        raise _error(404, "KNOWLEDGE_NOT_FOUND", "Organization knowledge not found")
    _audit("org_knowledge_delete", user, org_id, {"id": knowledge_id})
    return {"ok": True, "id": knowledge_id}
