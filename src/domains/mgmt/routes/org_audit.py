"""组织审计查询路由（GET /api/orgs/{org_id}/audit）。

只读接口：组织 owner/admin 检索本组织审计日志（关联仓库绑定/解绑/迁移、
跨仓下派、组织上下文/知识、手动绑定等），member / 非成员不可见，敏感字段脱敏后返回。
注：组织供给（provisioning）链路已落审计（started/completed/failed）。

认证：require_no_session_token（JWT/API Key；tk_ 会话凭据 → 403）。
鉴权：路径型显式校验（Issue #381/#395：不用上下文型 require_org_admin，
      其回退个人组织与路径脱钩可越权）——复用 org_repos 的统一错误封装与
      org 存在性校验：org 不存在 404 ORG_NOT_FOUND，非成员/非 owner-admin 403 PERMISSION_DENIED。
"""
from fastapi import APIRouter, Depends, Query

from src.api.deps import require_no_session_token
from src.domains.mgmt.auth.audit import query_org_audit
from src.domains.mgmt.routes.org_repos import _error, _require_org
from src.domains.mgmt.user.org_db import get_member_role

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


@router.get("/{org_id}/audit", operation_id="orgs_audit", summary="Query organization audit logs")
def get_org_audit(
    org_id: str,
    event_type: str = Query("", description="事件类型或前缀（如 org_repo_bind / org_repo_ / org_dispatch_）"),
    limit: int = Query(50, description="每页条数，默认 50、最大 200（越界自动夹取）"),
    offset: int = Query(0, description="偏移量，负数自动夹取为 0"),
    since: str = Query("", description="起始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"),
    user: dict = Depends(require_no_session_token),
):
    _require_org(org_id)
    if get_member_role(user["id"], org_id) not in ("owner", "admin"):
        raise _error(403, "PERMISSION_DENIED", "Organization admin permission required")
    try:
        return query_org_audit(org_id, event_type=event_type, limit=limit,
                               offset=offset, since=since)
    except ValueError as e:
        raise _error(400, "INVALID_SINCE", str(e))
