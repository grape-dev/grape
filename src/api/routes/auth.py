"""认证路由：token 续期、用户信息、登出

D1 修复后仅保留 3 个路由（POST /refresh、GET /profile、POST /logout）。
密码登录和 OAuth 路由统一由 grape_auth.py 提供。

JWT 双 token 机制：
  - access_token（15分钟）——存 localStorage，JS 读取加到 header
  - refresh_token（7天滚动续期）——存 HttpOnly cookie，JS 不可读
  - token_version——改密码/logout 时 +1，旧 token 失效
"""
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from src.user import db as user_db
from src.api.deps import require_no_session_token
from src.auth.grape_account_db import get_grape_account_by_username, increment_token_version as grape_inc
from src.auth.cookie_utils import (
    set_refresh_cookie as _set_refresh_cookie,
    clear_refresh_cookie as _clear_refresh_cookie,
    get_refresh_token,
    app_base as _app_base,
)
import os

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/refresh")
def refresh_token(request: Request, response: Response):
    """用 refresh_token 换新的 access_token + refresh_token（滚动续期）。

    D3 修复：复用 deps.py 的 get_user_status_cached 校验 token_version + status。
    """
    # CSRF 保护
    xrw = request.headers.get("X-Requested-With", "")
    if xrw != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "缺少 CSRF header"})
    # refresh_token 从 HttpOnly cookie 读
    refresh = get_refresh_token(request)
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "MISSING_REFRESH_TOKEN", "message": "缺少 refresh_token"})
    from src.api.jwt_utils import verify_refresh_token, create_access_token, create_refresh_token
    payload = verify_refresh_token(refresh)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "REFRESH_TOKEN_INVALID", "message": "refresh_token 过期或无效"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "USER_NOT_FOUND", "message": "用户不存在"})

    # D3 修复：复用 deps.py 缓存校验 token_version + status
    from src.api.deps import get_user_status_cached, invalidate_user_cache
    db_ver, status, _ = get_user_status_cached(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_REVOKED", "message": "会话已失效"})
    if status != 1:
        invalidate_user_cache(uid)
        # BUG-7 修复：区分冷静期(status=3)和禁用(status=0)，需求20要求冷静期返回不同提示
        if status == 3:
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DEACTIVATING", "message": "该账号正在注销中，暂时无法登录"})
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DEACTIVATED", "message": "该账号已被停用，请联系管理员"})

    # 滚动续期
    old_max_exp = int(payload.get("max_exp", 0))
    old_login_provider = payload.get("login_provider", "")
    new_access_token = create_access_token(uid, username, db_ver, login_provider=old_login_provider)
    new_refresh_token = create_refresh_token(uid, username, db_ver, max_exp=old_max_exp,
                                             login_provider=old_login_provider)
    _set_refresh_cookie(response, new_refresh_token)
    return {"token": new_access_token}


@router.get("/profile", operation_id="auth_profile", summary="Get current user profile", description="Returns the current user profile. Supports JWT session, user API Key (grape_) or session token (tk_).")
def get_profile(user: dict = Depends(require_no_session_token)):
    """用户信息——支持 JWT 会话 / grape_ API Key / tk_ 会话 token（Issue #69）。

    D9 修复：返回 bindings 列表（已绑定的 provider 数组），兼容旧 gitcode_bound/github_bound 字段。
    """
    acc = get_grape_account_by_username(user["username"]) or {}
    managed_repos_raw = user_db.get_managed_repos(user["id"])
    managed = [{"key": r["full_name"], "full_name": r["full_name"], "is_admin": r["is_admin"]}
               for r in managed_repos_raw[:10]]

    # D9: 构建 bindings 列表（动态，不硬编码平台）
    from src.auth.grape_account_db import get_user_identities, has_password
    identities = get_user_identities(user["id"])
    bindings = [ident["provider"] for ident in identities]
    # 兼容旧字段
    gitcode_bound = "gitcode" in bindings or bool(user_db.get_credential(user["id"], "gitcode_oauth"))
    github_bound = "github" in bindings or bool(user_db.get_credential(user["id"], "github_oauth"))

    return {
        "username": user["username"],
        "nickname": acc.get("nickname", "") or user["username"],
        "email": acc.get("email", ""),
        "has_password": has_password(user["id"]),
        "status": "online",
        "managed_repos": managed,
        "bindings": bindings,
        "gitcode_bound": gitcode_bound,
        "github_bound": github_bound,
        "email_verified": acc.get("email_verified", 0),
    }


@router.post("/logout", operation_id="auth_logout", summary="Logout (revoke all tokens)", description="Logs out: increments token_version to revoke all tokens and clears the refresh cookie.")
def logout(user: dict = Depends(require_no_session_token), response: Response = None):
    """登出——token_version+1 撤销所有 token + 清 cookie + 失效缓存。"""
    # D32: 只走 grape_accounts
    grape_inc(user["id"])
    # 失效内存缓存
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user["id"])
    if response:
        _clear_refresh_cookie(response)
    return {"message": "已登出"}
