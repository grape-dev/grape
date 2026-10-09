"""Admin 管理后台 API —— /api/admin/*

- admin 账号独立于 user_accounts（admin_accounts 表）
- 认证：JWT + role=admin（require_admin）
- 配置中心：GitCode/GitHub bot token + OAuth client_id/secret + bot_usernames
  保存落盘 configs/global-bot-token.json + 同步 meta 表
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from src.admin import db as admin_db
from src.api.deps import require_admin
from src.config import log

router = APIRouter(prefix="/api/admin", tags=["admin"])

_COOKIE_NAME = "refresh_token"
_COOKIE_PATH = "/api/admin"
_COOKIE_MAX_AGE = 7 * 24 * 3600


def _set_refresh_cookie(response: Response, refresh_token: str):
    from src.config import COOKIE_SECURE
    response.set_cookie(
        key=_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
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
    gitcode_oauth_redirect_uri: str = ""
    github_bot_token: str = ""
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_redirect_uri: str = ""
    bot_usernames: list[str] = []


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
            description="Returns masked config from global-bot-token.json. Admin only.")
def get_config(admin: dict = Depends(require_admin)):
    from src.admin.config_store import read_public_config
    from src.user.db import get_meta_value
    return {
        "config": read_public_config(),
        "oauth_status": {
            "gitcode": bool(get_meta_value("oauth_gitcode_client_id")),
            "github": bool(get_meta_value("oauth_github_client_id")),
        },
        "pwd_initial": get_meta_value("admin_pwd_initial") == "1",
    }


@router.put("/config", operation_id="admin_config_put", summary="Save platform config",
            description="Writes to configs/global-bot-token.json and syncs OAuth to meta table. Admin only.")
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