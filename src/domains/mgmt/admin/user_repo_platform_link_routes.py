"""UserRepoPlatformLink（用户↔代码托管平台）：GET 只读；DELETE=用户禁用（不动凭据）。"""
from __future__ import annotations

from fastapi import APIRouter

from src.domains.mgmt.admin.api_common import Depends, not_found, require_admin, uow_write

router = APIRouter(prefix="/user-repo-platform-links", tags=["admin"])


def _item(r) -> dict:
    return {"user_id": r.user_id, "repo_provider": r.repo_provider,
             "linked_at": r.linked_at, "revoked_at": r.revoked_at or "",
             "revoked_reason": r.revoked_reason or "",
             "active": not bool(r.revoked_at)}


@router.get("", summary="用户平台关系（只读；?user_id= 必填）")
def list_user_links(user_id: str = "", admin: dict = Depends(require_admin)):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.models_platform_object import UserRepoPlatformLinkModel

    if not user_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail={"code": "VALIDATION", "field": "user_id"})
    with Session(get_engine()) as s:
        rows = s.execute(select(UserRepoPlatformLinkModel)
                         .where(UserRepoPlatformLinkModel.user_id == user_id)).scalars().all()
        return {"links": [_item(r) for r in rows], "source": "table"}


@router.get("/{user_id}/{repo_provider}", summary="单条用户平台关系")
def get_user_link(user_id: str, repo_provider: str, admin: dict = Depends(require_admin)):
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo

    with Session(get_engine()) as s:
        row = UserRepoPlatformLinkRepo.get(s, user_id, repo_provider)
        if row is None:
            raise not_found("user-repo-platform-links", f"{user_id}/{repo_provider}")
        return _item(row)


@router.delete("/{user_id}/{repo_provider}", summary="用户禁用该平台（写 disabled_by_user，不删凭据）")
def disable_platform(user_id: str, repo_provider: str, admin: dict = Depends(require_admin)):
    from src.domains.platform.hooks import on_platform_disabled

    on_platform_disabled(user_id, repo_provider)
    return {"ok": True, "user_id": user_id, "repo_provider": repo_provider,
             "revoked_reason": "disabled_by_user"}
