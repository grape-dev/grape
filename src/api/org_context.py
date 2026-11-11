"""组织上下文解析——从 HTTP 请求中解析当前组织上下文。

优先级：
1. X-Org-ID header
2. X-Org-Slug header → 查 organization 表
3. ?org_id query
4. ?org_slug query → 查 organization 表
5. 未指定 → 返回用户的个人组织 ID（兼容现有行为）

校验：用户必须是该组织的成员（org_member 表），否则 403。

D105 第三批：org_id 全链路 str，哨兵 0 → ''（''=全局/未归属；MySQL TEXT 列与 0
数值比较会把非数字串强转 0 误匹配，哨兵必须用空串）。
"""
from __future__ import annotations

from fastapi import Request, HTTPException

from src.domains.mgmt.user.org_db import (
    get_org_by_slug, get_member_role, get_personal_org_id, is_org_member,
)


def resolve_org_context(request: Request, user_id: str) -> str:
    """解析当前请求的组织上下文，返回 org_id（str；未归属为 ''）。

    未指定时回退到用户的个人组织（兼容现有 API 行为）。
    指定时校验成员关系，非成员 → 403。
    """
    org_id = ""

    # 1. X-Org-ID header（不透明 id 透传，不 int() 强转——uuid 形态 org_id 合法）
    org_id = (request.headers.get("X-Org-ID", "") or "").strip()

    # 2. X-Org-Slug header
    if not org_id:
        slug = request.headers.get("X-Org-Slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = str(org["id"])

    # 3. ?org_id query
    if not org_id:
        org_id = (request.query_params.get("org_id", "") or "").strip()

    # 4. ?org_slug query
    if not org_id:
        slug = request.query_params.get("org_slug", "")
        if slug:
            org = get_org_by_slug(slug)
            if org:
                org_id = str(org["id"])

    # 5. 未指定 → 回退到个人组织
    if not org_id:
        org_id = get_personal_org_id(user_id)
    else:
        # 校验成员关系
        if not is_org_member(user_id, org_id):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})

    return org_id
