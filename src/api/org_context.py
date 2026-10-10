"""组织上下文解析——从 HTTP 请求中解析当前组织上下文。

优先级：
1. X-Org-ID header
2. X-Org-Slug header → 查 organization 表
3. ?org_id query
4. ?org_slug query → 查 organization 表
5. 未指定 → 返回用户的个人组织 ID（兼容现有行为）

校验：用户必须是该组织的成员（org_member 表），否则 403。
"""
from __future__ import annotations

from fastapi import Request, HTTPException

from src.user.org_db import (
    get_org_by_slug, get_member_role, get_personal_org_id, is_org_member,
)


def resolve_org_context(request: Request, user_id: int) -> int:
    """解析当前请求的组织上下文，返回 org_id。

    未指定时回退到用户的个人组织（兼容现有 API 行为）。
    指定时校验成员关系，非成员 → 403。
    """
    org_id = 0

    # 1. X-Org-ID header
    org_id_str = request.headers.get("X-Org-ID", "")
    if org_id_str:
        try:
            org_id = int(org_id_str)
        except ValueError:
            pass

    # 2. X-Org-Slug header
    if org_id == 0:
        slug = request.headers.get("X-Org-Slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = org["id"]

    # 3. ?org_id query
    if org_id == 0:
        qp = request.query_params.get("org_id", "")
        if qp:
            try:
                org_id = int(qp)
            except ValueError:
                pass

    # 4. ?org_slug query
    if org_id == 0:
        slug = request.query_params.get("org_slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = org["id"]

    # 5. 未指定 → 回退到个人组织
    if org_id == 0:
        org_id = get_personal_org_id(user_id)
    else:
        # 校验成员关系
        if not is_org_member(user_id, org_id):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})

    return org_id
