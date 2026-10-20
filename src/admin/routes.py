"""Admin 管理后台 API —— /api/admin/*

- admin 账号独立于 user_accounts（admin_accounts 表）
- 认证：JWT + role=admin（require_admin）
- 配置中心：GitCode/GitHub bot token + OAuth client_id/secret + bot_usernames
  保存到 DB meta 表（Issue #345: DB 是唯一权威源）
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from src.admin import db as admin_db
from src.api.deps import require_admin
from src.config import log
from src.auth.cookie_utils import _cookie_secure

router = APIRouter(prefix="/api/admin", tags=["admin"])

_COOKIE_NAME = "refresh_token"
_COOKIE_PATH = "/api/admin"
_COOKIE_MAX_AGE = 7 * 24 * 3600


def _set_refresh_cookie(response: Response, refresh_token: str):
    response.set_cookie(
        key=_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path=_COOKIE_PATH,
        max_age=_COOKIE_MAX_AGE,
    )


class AdminLoginBody(BaseModel):
    username: str
    password: str


class AdminChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class ConfigBody(BaseModel):
    gitcode_api_token: str = ""
    gitcode_oauth_client_id: str = ""
    gitcode_oauth_client_secret: str = ""
    github_bot_token: str = ""
    github_bot_username: str = ""
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    bot_usernames: list[str] = []
    webhook_url: str = ""
    gitcode_webhook_token: str = ""


class VerifyTokenBody(BaseModel):
    provider: str  # gitcode | github
    token: str


@router.post("/login", operation_id="admin_login", summary="Admin login",
             description="Admin username/password login. Returns access token (localStorage) + refresh token (HttpOnly cookie).")
def admin_login(body: AdminLoginBody, response: Response, request: Request):
    from src.auth import login_throttle
    from src.auth.audit import log_audit
    _ip = request.client.host if request.client else ""
    _identifier = "admin:" + body.username.strip()
    # P0-2: 暴力破解防护——复用 login_throttle（账号维度 + IP 维度）
    throttle = login_throttle.check_throttle(_identifier, _ip)
    if not throttle.get("allowed"):
        _msg = throttle.get("message", "Login rate limited")
        _retry = throttle.get("retry_after", 0)
        # P2-5: 账号锁定审计日志
        log_audit("admin_login_failed", operator_id=0, ip=_ip,
                  detail={"identifier": body.username.strip(), "locked": True,
                          "retry_after": _retry})
        raise HTTPException(status_code=429, detail={"code": 429, "message": _msg, "retry_after": _retry})
    admin = admin_db.verify_admin(body.username.strip(), body.password)
    if not admin:
        login_throttle.record_failure(_identifier, _ip)
        log_audit("admin_login_failed", operator_id=0, ip=_ip,
                  detail={"identifier": body.username.strip()})
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "INVALID_CREDENTIALS", "message": "Invalid username or password"})
    login_throttle.clear_failures(_identifier, _ip)
    from src.api.jwt_utils import create_access_token, create_refresh_token
    access_token = create_access_token(admin["id"], admin["username"],
                                       admin["token_version"], role="admin")
    refresh_token = create_refresh_token(admin["id"], admin["username"],
                                         admin["token_version"], role="admin")
    _set_refresh_cookie(response, refresh_token)
    admin_db.touch_admin_login(admin["id"])
    from src.user.db import get_meta_value
    return {
        "token": access_token,
        "user": {"id": admin["id"], "username": admin["username"]},
        "pwd_initial": get_meta_value("admin_pwd_initial") == "1",
    }


@router.post("/refresh", operation_id="admin_refresh", summary="Admin refresh token")
def admin_refresh(request: Request, response: Response):
    xrw = request.headers.get("X-Requested-With", "")
    if xrw != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail={"code": 403, "error_code": "MISSING_CSRF_HEADER", "message": "Missing CSRF header"})
    refresh = request.cookies.get(_COOKIE_NAME, "")
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "MISSING_REFRESH_TOKEN", "message": "Missing refresh_token"})
    from src.api.jwt_utils import verify_refresh_token, create_access_token, create_refresh_token
    payload = verify_refresh_token(refresh)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "REFRESH_TOKEN_INVALID", "message": "refresh_token expired or invalid"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    db_ver = admin_db.get_admin_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_EXPIRED", "message": "Session expired"})
    new_access = create_access_token(uid, username, db_ver, role="admin")
    new_refresh = create_refresh_token(uid, username, db_ver,
                                       max_exp=int(payload.get("max_exp", 0)),
                                       role="admin")
    _set_refresh_cookie(response, new_refresh)
    return {"token": new_access}


@router.post("/logout", operation_id="admin_logout", summary="Admin logout")
def admin_logout(response: Response, admin: dict = Depends(require_admin)):
    admin_db.increment_admin_token_version(admin["id"])
    response.delete_cookie(key=_COOKIE_NAME, path=_COOKIE_PATH)
    return {"message": "logged out"}


@router.post("/change-password", operation_id="admin_change_password", summary="Admin change password")
def admin_change_password(body: AdminChangePasswordBody, response: Response,
                          admin: dict = Depends(require_admin)):
    _admin = admin_db.get_admin_by_username(admin["username"])
    if not _admin:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_NOT_FOUND", "message": "Account not found"})
    # P1-1: 用 verify_password 兼容旧 SHA-256 和新 bcrypt
    from src.auth.password import verify_password
    if not verify_password(body.old_password, _admin["password_hash"]):
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "OLD_PASSWORD_INCORRECT", "message": "Old password incorrect"})
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "PASSWORD_TOO_SHORT", "message": "New password must be at least 8 characters"})
    # P1-3: 接入密码强度校验（与注册流程对齐）
    from src.auth.password import check_password_strength
    strength_err = check_password_strength(body.new_password, username=admin["username"])
    if strength_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "WEAK_PASSWORD", "message": strength_err})
    admin_db.update_admin_password(_admin["id"], body.new_password)
    admin_db.increment_admin_token_version(_admin["id"])
    from src.user.db import set_meta_value
    set_meta_value("admin_pwd_initial", "0")
    response.delete_cookie(key=_COOKIE_NAME, path=_COOKIE_PATH)
    from src.auth.audit import log_audit
    log_audit("admin_password_changed", operator_id=admin["id"])
    return {"message": "Password changed, please log in again"}


@router.get("/status", operation_id="admin_status", summary="Admin status",
            description="Public: whether admin initialized & password is still default. No auth.")
def admin_status():
    from src.user.db import get_meta_value
    return {
        "admin_initialized": get_meta_value("admin_initialized") == "1",
        "pwd_initial": get_meta_value("admin_pwd_initial") == "1",
    }


# ====== 配置中心 ======

@router.get("/config", operation_id="admin_config_get", summary="Get platform config (masked)",
            description="Returns masked config from DB meta table. Admin only.")
def get_config(admin: dict = Depends(require_admin)):
    from src.admin.config_store import read_public_config
    from src.user.db import get_meta_value
    config = read_public_config()
    return {
        "config": config,
        "oauth_status": config.get("oauth_status", {}),
        "pwd_initial": get_meta_value("admin_pwd_initial") == "1",
    }


@router.put("/config", operation_id="admin_config_put", summary="Save platform config",
            description="Saves bot config to DB meta table, OAuth to oauth-providers.json + meta. Admin only.")
def put_config(body: ConfigBody, admin: dict = Depends(require_admin)):
    payload = body.model_dump(exclude_none=True)
    # 只保存非空字段（脱敏读、增量写）
    from src.admin.config_store import save_config
    cleaned = {k: v for k, v in payload.items() if v is not None and v != "" and v != []}
    result = save_config(cleaned)
    from src.auth.audit import log_audit
    log_audit("admin_config_saved", operator_id=admin["id"],
              detail={"fields": sorted(cleaned.keys())})
    return {"message": "Config saved", "config": result}


@router.post("/config/verify-token", operation_id="admin_config_verify_token",
             summary="Verify platform token", description="Calls platform /user API to validate token and returns username.")
def verify_platform_token(body: VerifyTokenBody, admin: dict = Depends(require_admin)):
    if body.provider not in ("gitcode", "github"):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "INVALID_PROVIDER", "message": "provider must be gitcode or github"})
    if not body.token:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_REQUIRED", "message": "token is required"})
    try:
        from src.admin.config_store import verify_token
        info = verify_token(body.provider, body.token)
        return {"ok": bool(info.get("login")), "login": info.get("login", ""), "name": info.get("name", "")}
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"Token verification failed: {str(e)[:200]}"})

# ===========================================================================
# D6: 用户管理路由（需求 21）
# ===========================================================================

class DisableUserBody(BaseModel):
    reason: str = ""


class WhitelistBody(BaseModel):
    type: str  # email / domain
    value: str
    remark: str = ""


class AllowSignupBody(BaseModel):
    enabled: bool


@router.get("/users", operation_id="admin_users_list",
             summary="List grape users with pagination and search")
def admin_list_users(page: int = 1, per_page: int = 20, q: str = "",
                     admin: dict = Depends(require_admin)):
    """分页查询 grape_accounts 用户列表。"""
    result = admin_db.list_grape_users(page=page, per_page=per_page, q=q)
    return result


@router.get("/users/{user_id}", operation_id="admin_user_detail",
             summary="Get grape user detail")
def admin_user_detail(user_id: int, admin: dict = Depends(require_admin)):
    """获取用户详情（含绑定信息、是否有密码）。"""
    detail = admin_db.get_grape_user_detail(user_id)
    if not detail:
        raise HTTPException(status_code=404, detail={"code": 404, "error_code": "USER_NOT_FOUND", "message": "User not found"})
    return detail


@router.post("/users/{user_id}/disable", operation_id="admin_user_disable",
              summary="Disable grape user")
def admin_disable_user(user_id: int, body: DisableUserBody,
                       admin: dict = Depends(require_admin)):
    """禁用用户（status=0）。"""
    # 需求21: 管理员不可禁用自己
    from src.auth.grape_account_db import get_grape_account_by_id
    target_user = get_grape_account_by_id(user_id)
    if target_user and target_user["username"] == admin.get("username"):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "CANNOT_DISABLE_SELF",
            "message": "Cannot disable your own account",
        })
    ok = admin_db.disable_grape_user(user_id, body.reason)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USER_NOT_ACTIVE", "message": "User not found or not in active status"})
    # 清除鉴权缓存
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user_id)
    # 记录审计日志
    from src.auth import audit as auth_audit
    auth_audit.log_audit("account_disabled", operator_id=admin["id"],
                         user_id=user_id, detail={"reason": body.reason})
    return {"message": "User disabled"}


@router.post("/users/{user_id}/restore", operation_id="admin_user_restore",
              summary="Restore grape user (from disabled or cooldown)")
def admin_restore_user(user_id: int, admin: dict = Depends(require_admin)):
    """恢复用户（status=0 禁用 或 status=3 冷静期 → status=1）。"""
    ok = admin_db.restore_grape_user(user_id)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USER_NOT_RESTORABLE", "message": "User not found or not in restorable status"})
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user_id)
    from src.auth import audit as auth_audit
    auth_audit.log_audit("account_restored", operator_id=admin["id"],
                         user_id=user_id)
    # 需求22: 恢复后通知用户（如有邮箱）
    from src.auth.grape_account_db import get_grape_account_by_id
    restored_user = get_grape_account_by_id(user_id)
    if restored_user and restored_user.get("email"):
        from src.auth.email_service import get_email_service
        try:
            get_email_service().send_notification(
                restored_user["email"],
                "Account restored",
                "Your Grape account has been restored by an administrator. You can now log in normally."
            )
        except Exception:
            pass  # 通知邮件发送失败不影响恢复操作
    return {"message": "User restored"}


@router.get("/signup-whitelist", operation_id="admin_signup_whitelist_list",
             summary="List signup whitelist")
def admin_list_whitelist(admin: dict = Depends(require_admin)):
    """获取注册白名单列表。"""
    return {"whitelist": admin_db.list_signup_whitelist()}


@router.post("/signup-whitelist", operation_id="admin_signup_whitelist_add",
              summary="Add signup whitelist entry")
def admin_add_whitelist(body: WhitelistBody, admin: dict = Depends(require_admin)):
    """添加注册白名单。"""
    if body.type not in ("email", "domain"):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "INVALID_WHISTLIST_TYPE", "message": "type must be email or domain"})
    ok = admin_db.add_signup_whitelist(body.type, body.value, body.remark)
    if not ok:
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "WHITELIST_ENTRY_EXISTS", "message": "Whitelist entry already exists"})
    from src.auth.audit import log_audit
    log_audit("admin_signup_whitelist_added", operator_id=admin["id"],
              detail={"type": body.type, "value": body.value})
    return {"message": "Added"}


@router.delete("/signup-whitelist/{item_id}", operation_id="admin_signup_whitelist_delete",
                summary="Delete signup whitelist entry")
def admin_delete_whitelist(item_id: int, admin: dict = Depends(require_admin)):
    """删除注册白名单。"""
    ok = admin_db.delete_signup_whitelist(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "error_code": "WHITELIST_ENTRY_NOT_FOUND", "message": "Whitelist entry not found"})
    from src.auth.audit import log_audit
    log_audit("admin_signup_whitelist_deleted", operator_id=admin["id"],
              detail={"item_id": item_id})
    return {"message": "Deleted"}


@router.put("/allow-signup", operation_id="admin_allow_signup_put",
             summary="Toggle open signup")
def admin_allow_signup(body: AllowSignupBody, admin: dict = Depends(require_admin)):
    """开启/关闭开放注册。"""
    from src.user import db as user_db
    user_db.set_meta_value("allow_signup", "1" if body.enabled else "0")
    from src.auth.audit import log_audit
    log_audit("admin_allow_signup_toggled", operator_id=admin["id"],
              detail={"enabled": body.enabled})
    return {"message": "Updated", "allow_signup": body.enabled}


@router.get("/bot-token-health", operation_id="admin_bot_token_health",
             summary="Get global bot token health status")
def get_bot_token_health(admin: dict = Depends(require_admin)):
    """Issue #345: 查询全局 bot token 健康状态。

    R16.2.3: 返回 per-provider 过期标记（global_*_bot_token_status）+ 仓库级 token 状态。
    从 DB meta 表读取最近一次探活结果（集群共享）。
    """
    import json
    from src.user.db import get_meta_value
    raw = get_meta_value("bot_token_health")
    result = {}
    if raw:
        try:
            result = json.loads(raw)
        except Exception:
            result = {}
    if not result:
        result = {"overall": "healthy", "platforms": {},
                  "checked_at": "", "note": "Health check not yet executed"}
    # R16.2.3: 补充 per-provider 过期标记
    result["global_gitcode_bot_token_status"] = get_meta_value("global_gitcode_bot_token_status") or ""
    result["global_github_bot_token_status"] = get_meta_value("global_github_bot_token_status") or ""
    return result


# ===== OAuth Login Whitelist Management (Issue #332) =====

class LoginWhitelistBody(BaseModel):
    provider: str  # "gitcode" or "github"
    username: str
    remark: str = ""


class ToggleLoginWhitelistBody(BaseModel):
    enabled: bool


@router.get("/whitelist", operation_id="admin_login_whitelist_list",
             summary="List OAuth login whitelist")
def admin_list_login_whitelist(admin: dict = Depends(require_admin)):
    """Issue #332: List OAuth login whitelist entries."""
    return {"whitelist": admin_db.list_login_whitelist(),
            "enabled": admin_db.get_login_whitelist_enabled()}


@router.post("/whitelist", operation_id="admin_login_whitelist_add",
              summary="Add OAuth login whitelist entry")
def admin_add_login_whitelist(body: LoginWhitelistBody, admin: dict = Depends(require_admin)):
    """Issue #332: Add OAuth login whitelist entry."""
    if body.provider not in ("gitcode", "github"):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "INVALID_PROVIDER", "message": "provider must be gitcode or github"})
    if not body.username.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_REQUIRED", "message": "username required"})
    ok = admin_db.add_login_whitelist(body.provider, body.username.strip(), body.remark)
    if not ok:
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "WHITELIST_ENTRY_EXISTS", "message": "Entry already exists"})
    from src.auth.audit import log_audit
    log_audit("admin_whitelist_added", operator_id=admin["id"],
              detail={"provider": body.provider, "username": body.username.strip()})
    return {"message": "Added"}


@router.delete("/whitelist/{item_id}", operation_id="admin_login_whitelist_delete",
                summary="Delete OAuth login whitelist entry")
def admin_delete_login_whitelist(item_id: int, admin: dict = Depends(require_admin)):
    """Issue #332: Delete OAuth login whitelist entry."""
    ok = admin_db.delete_login_whitelist(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "error_code": "WHITELIST_ENTRY_NOT_FOUND", "message": "Entry not found"})
    from src.auth.audit import log_audit
    log_audit("admin_whitelist_deleted", operator_id=admin["id"],
              detail={"item_id": item_id})
    return {"message": "Deleted"}


@router.put("/whitelist/toggle", operation_id="admin_login_whitelist_toggle",
             summary="Toggle OAuth login whitelist on/off")
def admin_toggle_login_whitelist(body: ToggleLoginWhitelistBody, admin: dict = Depends(require_admin)):
    """Issue #332: Set login_whitelist_enabled to the specified value."""
    ok = admin_db.set_login_whitelist_enabled(body.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "TOGGLE_FAILED", "message": "Failed to toggle"})
    from src.auth.audit import log_audit
    log_audit("admin_whitelist_toggled", operator_id=admin["id"],
              detail={"enabled": body.enabled})
    return {"message": "Updated", "enabled": body.enabled}


# ===== User CRUD (Issue #332) =====

class CreateUserBody(BaseModel):
    username: str
    email: str = ""
    password: str
    nickname: str = ""


class EditUserBody(BaseModel):
    email: str = None
    nickname: str = None


class ResetPasswordBody(BaseModel):
    password: str


@router.post("/users", operation_id="admin_user_create",
              summary="Manually create user")
def admin_create_user(body: CreateUserBody, admin: dict = Depends(require_admin)):
    """Issue #332: Manually create a user (no OAuth/registration flow)."""
    if len(body.username) < 3 or len(body.password) < 8:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_PASSWORD_TOO_SHORT", "message": "username min 3 chars, password min 8 chars"})
    # P1-3: 接入密码强度校验
    from src.auth.password import check_password_strength
    strength_err = check_password_strength(body.password, username=body.username, email=body.email or "")
    if strength_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "WEAK_PASSWORD", "message": strength_err})
    result = admin_db.create_grape_user(body.username, body.email, body.password, body.nickname)
    if result == "username_conflict":
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "USERNAME_EXISTS", "message": "Username already exists"})
    if result == "email_conflict":
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_EXISTS", "message": "Email already in use"})
    if result is None:
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "CREATE_USER_FAILED", "message": "Create user failed"})
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "CREATE_USER_FAILED", "message": "Create user failed"})
    uid = int(result["id"])
    username = result["username"]
    # P2-1: 创建个人组织——与注册流程一致
    try:
        from src.api.routes.grape_auth import _ensure_personal_org
        _ensure_personal_org(uid, username)
    except Exception as e:
        log.warning(f"create_user: ensure_personal_org failed (will retry on first login): {e}")
    # P0-1: 审计日志
    from src.auth.audit import log_audit
    log_audit("admin_user_created", operator_id=admin["id"], user_id=uid,
              detail={"username": username, "email": result.get("email", "")})
    return result


@router.put("/users/{user_id}", operation_id="admin_user_edit",
             summary="Edit user info")
def admin_edit_user(user_id: int, body: EditUserBody, admin: dict = Depends(require_admin)):
    """Issue #332: Edit user email/nickname."""
    result = admin_db.update_grape_user(user_id, body.email, body.nickname)
    if result == "conflict":
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_EXISTS", "message": "Email already in use"})
    if not result:
        raise HTTPException(status_code=404, detail={"code": 404, "error_code": "USER_NOT_FOUND", "message": "User not found"})
    from src.auth.audit import log_audit
    log_audit("admin_user_edited", operator_id=admin["id"], user_id=user_id,
              detail={"email": body.email, "nickname": body.nickname})
    return {"message": "Updated"}


@router.post("/users/{user_id}/reset-password", operation_id="admin_user_reset_password",
              summary="Reset user password")
def admin_reset_password(user_id: int, body: ResetPasswordBody, admin: dict = Depends(require_admin)):
    """Issue #332: Reset user password (invalidates old tokens)."""
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "PASSWORD_TOO_SHORT", "message": "password min 8 chars"})
    # P1-3: 接入密码强度校验（补传 username 保持三个密码操作一致）
    from src.auth.password import check_password_strength
    user_detail = admin_db.get_grape_user_detail(user_id)
    _username = user_detail["username"] if user_detail else ""
    strength_err = check_password_strength(body.password, username=_username)
    if strength_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "WEAK_PASSWORD", "message": strength_err})
    ok = admin_db.reset_grape_user_password(user_id, body.password)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "error_code": "USER_NOT_FOUND", "message": "User not found"})
    from src.auth.audit import log_audit
    log_audit("admin_password_reset", operator_id=admin["id"], user_id=user_id)
    return {"message": "Password reset"}


# ===== Whitelist status (signup + login combined) =====
# Note: GET /whitelist-status was removed — frontend now uses GET /whitelist
# which returns {whitelist, enabled} in a single call.
