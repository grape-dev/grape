"""认证路由：OAuth 登录、密码登录、用户信息、登出"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user
from src.config import log
import hashlib, os, time

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PasswordLogin(BaseModel):
    username: str
    password: str


@router.post("/login")
async def password_login(body: PasswordLogin):
    user = user_db.verify_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户名或密码错误"})
    # JWT 双 token——access（15分钟）+ refresh（7天）
    from src.api.jwt_utils import create_access_token, create_refresh_token
    token_version = 0  # TODO: 从 user_accounts.token_version 读（主动撤销用）
    access_token = create_access_token(user["id"], user["username"], token_version)
    refresh_token = create_refresh_token(user["id"], user["username"], token_version)
    return {
        "token": access_token,
        "refresh_token": refresh_token,
        "user": {"id": user["id"], "username": user["username"]}
    }


@router.post("/refresh")
async def refresh_token(body: dict):
    """用 refresh_token 换新的 access_token。

    前端 access_token 过期（401）时自动调这个接口。
    refresh_token 也过期则返回 401——前端跳登录页。
    """
    refresh = (body.get("refresh_token") or "").strip()
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "缺少 refresh_token"})
    from src.api.jwt_utils import verify_refresh_token, create_access_token
    payload = verify_refresh_token(refresh)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "refresh_token 过期或无效"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_version = payload.get("ver", 0)
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    new_access_token = create_access_token(uid, username, token_version)
    return {"token": new_access_token}


@router.get("/login/gitcode")
async def gitcode_oauth_login(request: Request):
    from src.user.db import get_meta_value
    client_id = get_meta_value("oauth_gitcode_client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode OAuth 未配置"})

    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI") or get_meta_value("oauth_gitcode_redirect_uri")
    if not redirect_uri:
        scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
        host = request.headers.get("Host", "localhost:80").split(":")[0]
        redirect_uri = f"{scheme}://{host}:80/api/auth/callback/gitcode"

    state = hashlib.sha256(os.urandom(32)).hexdigest()
    user_db.save_oauth_state(state, "gitcode", {})

    import urllib.parse
    auth_url = (f"https://gitcode.com/oauth/authorize"
                f"?client_id={urllib.parse.quote(client_id)}"
                f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
                f"&response_type=code"
                f"&scope=all"
                f"&state={urllib.parse.quote(state)}")
    return {"redirect_url": auth_url}


@router.get("/callback/gitcode")
async def gitcode_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"GitCode 授权失败: {error}"})

    state_data = user_db.validate_oauth_state(state, "gitcode")
    if state_data is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "state 无效或已过期"})

    from src.user.db import get_meta_value
    client_id = get_meta_value("oauth_gitcode_client_id")
    client_secret = get_meta_value("oauth_gitcode_client_secret")

    import urllib.request, urllib.parse, json
    token_data = None
    try:
        token_req = urllib.request.Request(
            "https://gitcode.com/oauth/token",
            data=urllib.parse.urlencode({
                "grant_type": "authorization_code", "code": code,
                "client_id": client_id, "client_secret": client_secret,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        token_resp = urllib.request.urlopen(token_req, timeout=10)
        token_data = json.loads(token_resp.read())
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"获取 access_token 失败: {e}"})

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "获取 access_token 失败"})

    gitcode_user = None
    try:
        user_req = urllib.request.Request(
            "https://api.gitcode.com/api/v5/user",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        gitcode_user = json.loads(urllib.request.urlopen(user_req, timeout=10).read())
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"获取用户信息失败: {e}"})

    gitcode_id = str(gitcode_user.get("id", ""))
    gitcode_login = gitcode_user.get("login", "")
    # 昵称：优先 nickname，次选 name（每次 OAuth 登录刷新）
    gitcode_nickname = gitcode_user.get("nickname") or gitcode_user.get("name") or ""
    if not gitcode_id:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取用户信息"})

    local_user = user_db.get_user_by_gitcode_id(gitcode_id)
    if not local_user:
        local_user = user_db.get_user_by_username(gitcode_login)

    # 白名单校验：已存在本地用户默认放行；全新用户须在白名单内（拒绝则无任何数据产生）
    if not local_user:
        from src.config import is_gitcode_user_allowed
        if not is_gitcode_user_allowed(gitcode_login):
            raise HTTPException(status_code=403, detail={
                "code": 403,
                "message": "您的 GitCode 账号不在白名单内，无法登录本系统。请联系统管理员。"
            })

    # 新用户才创建
    if not local_user:
        result = user_db.create_user(gitcode_login, hashlib.sha256(os.urandom(16)).hexdigest(), 0)
        if not result:
            raise HTTPException(status_code=500, detail={"code": 500, "message": "创建用户失败"})
        local_user = user_db.get_user(result["id"])

    user_db.link_gitcode_user(local_user["id"], gitcode_id, access_token, refresh_token)

    user_acc = user_db.get_user_account(local_user["username"]) or {}
    user_acc["gitcode_api_token"] = access_token
    if gitcode_nickname:
        user_acc["nickname"] = gitcode_nickname
    user_db.save_user_account(local_user["username"], user_acc)

    # 登录成功即建立 GitCode↔平台 user 映射（unified_user_mapping），
    # 使 /api/tasks/notify 能解析 assignee→user_id（tasks.py:52）；幂等，每次登录刷新。
    # 传空 open_id：update_unified_user_info 会保留已有 feishu_open_id（防覆盖飞书确认的绑定）。
    try:
        from src.session_manager import update_unified_user_info
        update_unified_user_info(gitcode_login, open_id="", gitcode_nickname=gitcode_nickname)
    except Exception as e:
        log.warning(f"OAuth login: write user mapping failed: {e}")

    token = user_db.create_session_token(local_user["id"])
    # 仅真正首次登录（无任何托管仓库）才 first_login=1（前端清 currentRepo 进仓库选择页）；
    # 已有托管仓库则保留上次选择的仓库（first_login=0），恢复上次的 repo
    from fastapi.responses import RedirectResponse
    has_repos = bool(user_db.get_managed_repos(local_user["id"]))
    return RedirectResponse(url=f"/?token={token}&first_login={'1' if not has_repos else '0'}", status_code=302)


@router.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account(user["username"]) or {}
    managed_repos_raw = user_db.get_managed_repos(user["id"])
    managed = [{"key": r["full_name"], "full_name": r["full_name"], "is_admin": r["is_admin"]}
               for r in managed_repos_raw[:10]]
    return {
        "username": user["username"],
        "nickname": acc.get("nickname", "") or user["username"],
        "status": "online",
        "managed_repos": managed,
        "gitcode_bound": bool(user_db.get_credential(user["id"], "gitcode_oauth")),
    }


@router.post("/logout")
async def logout():
    """登出——JWT 无状态，前端删 token 即可。"""
    return {"message": "已登出"}
