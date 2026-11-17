"""统一分类字典路由（Issue #2761）。

`/api/categories/tree?scope=scene|skill` 返回两级分类树，供场景/技能表单与筛选使用。
旧 `/api/scenes/market/categories`、`/api/skills/categories` 保留原一级列表（旧分类 tab）。
"""
from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import require_no_session_token

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get(
    "/tree",
    operation_id="categories_tree",
    summary="Category tree",
    description="Unified one/two-level category dictionary (Issue #2761); scope=scene|skill filters applies_to. Only is_active=1.",
)
def category_tree(scope: str = "", user: dict = Depends(require_no_session_token)):
    scope = (scope or "").strip().lower()
    if scope not in ("", "scene", "skill"):
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "scope 仅支持 scene / skill"})
    from src.domains.mgmt.user.db import list_category_tree

    return {"scope": scope or "all", "categories": list_category_tree(scope)}
