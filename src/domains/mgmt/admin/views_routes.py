"""派生视图（非实体，独立域 /views/*）：能力总览与用户维度可用性。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.domains.mgmt.admin.api_common import Depends, require_admin

router = APIRouter(tags=["admin"])


@router.get("/capabilities", summary="全平台能力视图（派生，不含凭据）")
def get_capabilities(admin: dict = Depends(require_admin)):
    from src.domains.platform.capabilities import all_capabilities

    return {"capabilities": all_capabilities()}


@router.get("/user-platforms", summary="用户维度平台可用性（派生：关系 + 凭据来源）")
def get_user_platforms(user_id: str = "", admin: dict = Depends(require_admin)):
    if not user_id:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION", "field": "user_id"})
    from src.domains.platform.capabilities import user_platform_view

    return {"user_id": user_id, "platforms": user_platform_view(user_id)}
