"""ChannelRepoLink（渠道↔平台授权）：GET / PUT(manual) / DELETE(仅 manual)。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.domains.mgmt.admin.api_common import Depends, not_found, require_admin, uow_write

router = APIRouter(prefix="/channel-repo-links", tags=["admin"])


def _item(r) -> dict:
    return {"oauth_provider": r.oauth_provider, "repo_provider": r.repo_provider,
             "source": r.source, "config_hash": r.config_hash}


@router.get("", summary="渠道↔平台授权集合（可按渠道/平台过滤）")
def list_links(oauth_provider: str = "", repo_provider: str = "", admin: dict = Depends(require_admin)):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.models_platform_object import ChannelRepoLinkModel

    stmt = select(ChannelRepoLinkModel)
    if oauth_provider:
        stmt = stmt.where(ChannelRepoLinkModel.oauth_provider == oauth_provider)
    if repo_provider:
        stmt = stmt.where(ChannelRepoLinkModel.repo_provider == repo_provider)
    with Session(get_engine()) as s:
        rows = s.execute(stmt.order_by(ChannelRepoLinkModel.oauth_provider,
                                       ChannelRepoLinkModel.repo_provider)).scalars().all()
        return {"links": [_item(r) for r in rows], "source": "table"}


@router.get("/{oauth_provider}/{repo_provider}", summary="单条授权关系")
def get_link(oauth_provider: str, repo_provider: str, admin: dict = Depends(require_admin)):
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.repositories_platform_object import ChannelRepoLinkRepo

    with Session(get_engine()) as s:
        row = ChannelRepoLinkRepo.get(s, oauth_provider, repo_provider)
        if row is None:
            raise not_found("channel-repo-links", f"{oauth_provider}/{repo_provider}")
        return _item(row)


@router.put("/{oauth_provider}/{repo_provider}", summary="管理面写关系（source=manual，导入不覆盖）")
def put_link(oauth_provider: str, repo_provider: str,
             admin: dict = Depends(require_admin)):
    from src.kernel.persistence.repositories_platform_object import (
        ChannelRepoLinkRepo,
        RepoPlatformRepo,
    )

    if oauth_provider == repo_provider:
        # 同域不是授权：渠道自己即平台，运行时天然可驱动，配一条 c→c 只会造无意义数据
        raise HTTPException(status_code=422, detail={
            "code": "SAME_DOMAIN_PAIR", "resource": "channel-repo-links",
            "detail": "同域配对无意义：渠道自己就是该代码托管平台，登录天然可驱动；"
                      "只有跨名平台才需要显式配对"})

    def _do(s):
        # 目标平台必须存在（与导入期校验一致：显式声明缺失目标 = 配置错误）
        if RepoPlatformRepo.get(s, repo_provider) is None:
            raise HTTPException(status_code=422, detail={
                "code": "TARGET_NOT_FOUND", "resource": "repo-platforms", "id": repo_provider,
                "detail": "关系目标平台不存在（先在 platforms/<p>.json 建卡并导入）"})
        ChannelRepoLinkRepo.set_manual(s, oauth_provider, repo_provider)

    uow_write(_do)
    return {"ok": True, "oauth_provider": oauth_provider, "repo_provider": repo_provider,
             "source": "manual"}


@router.delete("/{oauth_provider}/{repo_provider}", summary="删除 manual 关系（config/derived 行归导入）")
def delete_link(oauth_provider: str, repo_provider: str, admin: dict = Depends(require_admin)):
    from src.kernel.persistence.repositories_platform_object import ChannelRepoLinkRepo

    hit = {}

    def _do(s):
        hit["ok"] = ChannelRepoLinkRepo.delete_manual(s, oauth_provider, repo_provider)

    uow_write(_do)
    if not hit.get("ok"):
        raise HTTPException(status_code=409, detail={"code": "NOT_MANUAL",
                                                      "resource": "channel-repo-links",
                                                      "detail": "只允许删除 source=manual 的关系"})
    return {"ok": True}
