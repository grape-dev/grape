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
def admin_login(body: AdminLoginBody, response: Response):
    admin = admin_db.verify_admin(body.username.strip(), body.password)
    if not admin:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户名或密码错误"})
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
        raise HTTPException(status_code=403, detail={"code": 403, "message": "缺少 CSRF header"})
    refresh = request.cookies.get(_COOKIE_NAME, "")
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "缺少 refresh_token"})
    from src.api.jwt_utils import verify_refresh_token, create_access_token, create_refresh_token
    payload = verify_refresh_token(refresh)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=401, detail={"code": 401, "message": "refresh_token 过期或无效"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    db_ver = admin_db.get_admin_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话已失效"})
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
        raise HTTPException(status_code=401, detail={"code": 401, "message": "账号不存在"})
    if _admin["password_hash"] != admin_db._hash_password(body.old_password):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "原密码错误"})
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "新密码长度至少 8 位"})
    admin_db.update_admin_password(_admin["id"], body.new_password)
    admin_db.increment_admin_token_version(_admin["id"])
    from src.user.db import set_meta_value
    set_meta_value("admin_pwd_initial", "0")
    response.delete_cookie(key=_COOKIE_NAME, path=_COOKIE_PATH)
    return {"message": "密码已修改，请重新登录"}


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
    return {"message": "配置已保存", "config": result}


@router.post("/config/verify-token", operation_id="admin_config_verify_token",
             summary="Verify platform token", description="Calls platform /user API to validate token and returns username.")
def verify_platform_token(body: VerifyTokenBody, admin: dict = Depends(require_admin)):
    if body.provider not in ("gitcode", "github"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "provider 必须是 gitcode 或 github"})
    if not body.token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "token 不能为空"})
    try:
        from src.admin.config_store import verify_token
        info = verify_token(body.provider, body.token)
        return {"ok": bool(info.get("login")), "login": info.get("login", ""), "name": info.get("name", "")}
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"token 校验失败: {str(e)[:200]}"})

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
        raise HTTPException(status_code=404, detail={"code": 404, "message": "用户不存在"})
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
            "message": "不可禁用自己的账号",
        })
    ok = admin_db.disable_grape_user(user_id, body.reason)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "用户不存在或非正常状态"})
    # 清除鉴权缓存
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user_id)
    # 记录审计日志
    from src.auth import audit as auth_audit
    auth_audit.log_audit("account_disabled", operator_id=admin["id"],
                         user_id=user_id, detail={"reason": body.reason})
    return {"message": "用户已禁用"}


@router.post("/users/{user_id}/restore", operation_id="admin_user_restore",
              summary="Restore grape user (from disabled or cooldown)")
def admin_restore_user(user_id: int, admin: dict = Depends(require_admin)):
    """恢复用户（status=0 禁用 或 status=3 冷静期 → status=1）。"""
    ok = admin_db.restore_grape_user(user_id)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "用户不存在或非可恢复状态"})
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
                "账号已恢复",
                "您的 Grape 账号已被管理员恢复，现在可以正常登录使用了。"
            )
        except Exception:
            pass  # 通知邮件发送失败不影响恢复操作
    return {"message": "用户已恢复"}


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
        raise HTTPException(status_code=400, detail={"code": 400, "message": "type 必须是 email 或 domain"})
    ok = admin_db.add_signup_whitelist(body.type, body.value, body.remark)
    if not ok:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "白名单条目已存在"})
    return {"message": "已添加"}


@router.delete("/signup-whitelist/{item_id}", operation_id="admin_signup_whitelist_delete",
                summary="Delete signup whitelist entry")
def admin_delete_whitelist(item_id: int, admin: dict = Depends(require_admin)):
    """删除注册白名单。"""
    ok = admin_db.delete_signup_whitelist(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "白名单条目不存在"})
    return {"message": "已删除"}


@router.put("/allow-signup", operation_id="admin_allow_signup_put",
             summary="Toggle open signup")
def admin_allow_signup(body: AllowSignupBody, admin: dict = Depends(require_admin)):
    """开启/关闭开放注册。"""
    from src.user import db as user_db
    user_db.set_meta_value("allow_signup", "1" if body.enabled else "0")
    return {"message": "已更新", "allow_signup": body.enabled}


@router.get("/bot-token-health", operation_id="admin_bot_token_health",
             summary="Get global bot token health status")
def get_bot_token_health(admin: dict = Depends(require_admin)):
    """Issue #345: 查询全局 bot token 健康状态。

    从 DB meta 表读取最近一次探活结果（集群共享）。
    """
    import json
    from src.user.db import get_meta_value
    raw = get_meta_value("bot_token_health")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"overall": "healthy", "platforms": {},
            "checked_at": "", "note": "尚未执行探活"}
