"""认证路由：OAuth 登录、密码登录、用户信息、登出"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user
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
    token = user_db.create_session_token(user["id"])
    return {
        "token": token,
        "user": {"id": user["id"], "username": user["username"]}
    }


@router.get("/login/gitcode")
async def gitcode_oauth_login(request: Request):
    from src.config import get_account
    acc = get_account()
    client_id = acc.get("gitcode_oauth_client_id", "")
    if not client_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode OAuth 未配置"})

    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI") or acc.get("gitcode_oauth_redirect_uri", "")
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

    from src.config import get_account
    acc = get_account()
    client_id = acc.get("gitcode_oauth_client_id", "")
    client_secret = acc.get("gitcode_oauth_client_secret", "")

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
    if not gitcode_id:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取用户信息"})

    local_user = user_db.get_user_by_gitcode_id(gitcode_id)
    if not local_user:
        local_user = user_db.get_user_by_username(gitcode_login)
        if not local_user:
            result = user_db.create_user(gitcode_login, hashlib.sha256(os.urandom(16)).hexdigest(), 0)
            if not result:
                raise HTTPException(status_code=500, detail={"code": 500, "message": "创建用户失败"})
            local_user = user_db.get_user(result["id"])

    user_db.link_gitcode_user(local_user["id"], gitcode_id, access_token, refresh_token)

    user_acc = user_db.get_user_account(local_user["username"]) or {}
    user_acc["gitcode_api_token"] = access_token
    user_db.save_user_account(local_user["username"], user_acc)

    token = user_db.create_session_token(local_user["id"])
    return {"token": token, "user": {"id": local_user["id"], "username": local_user["username"]}}


@router.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account(user["username"]) or {}
    managed_repos_raw = user_db.get_managed_repos(user["id"])
    managed = [{"key": r["full_name"], "full_name": r["full_name"], "is_admin": r["is_admin"]}
               for r in managed_repos_raw[:10]]
    return {
        "username": user["username"],
        "status": "online",
        "managed_repos": managed,
        "gitcode_bound": bool(user_db.get_credential(user["id"], "gitcode_oauth")),
    }


@router.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    user_db.clear_token(user["token"])
    return {"message": "已登出"}
