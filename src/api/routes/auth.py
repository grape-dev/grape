"""认证路由：OAuth 登录、密码登录、用户信息、登出

JWT 双 token 机制：
  - access_token（15分钟）——存 localStorage，JS 读取加到 header
  - refresh_token（7天滚动续期）——存 HttpOnly cookie，JS 不可读
  - token_version——改密码/logout 时 +1，旧 token 失效
"""
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import require_no_session_token
from src.config import log, COOKIE_SECURE
import hashlib, os, time

router = APIRouter(prefix="/api/auth", tags=["auth"])

# HttpOnly cookie 配置
_COOKIE_NAME = "refresh_token"
_COOKIE_PATH = "/api/auth"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天


def _set_refresh_cookie(response: Response, refresh_token: str):
    """设置 refresh_token HttpOnly cookie——JS 不可读，防 XSS。"""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,  # 环境变量控制：生产 HTTPS true / 开发 HTTP false
        samesite="strict",
        path=_COOKIE_PATH,
        max_age=_COOKIE_MAX_AGE,
    )


def _clear_refresh_cookie(response: Response):
    """清除 refresh_token cookie——logout 时调用。"""
    response.delete_cookie(key=_COOKIE_NAME, path=_COOKIE_PATH)


class PasswordLogin(BaseModel):
    username: str
    password: str


@router.post("/login")
def password_login(body: PasswordLogin, response: Response):
    user = user_db.verify_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户名或密码错误"})
    # JWT 双 token——access（15分钟）+ refresh（7天滚动续期）
    from src.api.jwt_utils import create_access_token, create_refresh_token
    token_version = user.get("token_version", 0)
    access_token = create_access_token(user["id"], user["username"], token_version)
    refresh_token = create_refresh_token(user["id"], user["username"], token_version)
    # refresh_token 存 HttpOnly cookie——防 XSS
    _set_refresh_cookie(response, refresh_token)
    return {
        "token": access_token,
        "user": {"id": user["id"], "username": user["username"]}
    }


@router.post("/refresh")
def refresh_token(request: Request, response: Response):
    """用 refresh_token 换新的 access_token + refresh_token（滚动续期）。

    refresh_token 从 HttpOnly cookie 读取（不从 body 读——防 XSS）。
    CSRF 保护：要求 X-Requested-With header（同源 JS 自动加，跨站伪造无法加）。
    前端 access_token 过期（401）时自动调这个接口。
    refresh_token 也过期则返回 401——前端跳登录页。
    """
    # CSRF 保护——跨站请求伪造不会带自定义 header
    xrw = request.headers.get("X-Requested-With", "")
    if xrw != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "缺少 CSRF header"})
    # refresh_token 从 HttpOnly cookie 读
    refresh = request.cookies.get(_COOKIE_NAME, "")
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "缺少 refresh_token"})
    from src.api.jwt_utils import verify_refresh_token, create_access_token, create_refresh_token
    payload = verify_refresh_token(refresh)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "refresh_token 过期或无效"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    # token_version 校验——改密码/logout 后 +1，旧 refresh_token 失效
    db_ver = user_db.get_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话已失效"})
    # 滚动续期——新 access_token + 新 refresh_token
    # 新 refresh_token 继承旧 max_exp（不重新生成——30 天绝对上限）
    old_max_exp = int(payload.get("max_exp", 0))
    new_access_token = create_access_token(uid, username, db_ver)
    new_refresh_token = create_refresh_token(uid, username, db_ver, max_exp=old_max_exp)
    _set_refresh_cookie(response, new_refresh_token)
    return {"token": new_access_token}


@router.get("/login/gitcode")
def gitcode_oauth_login(request: Request):
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
def gitcode_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
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
        import requests as _req
        _user_resp = _req.get(
            "https://gitcode.com/api/v5/user",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        gitcode_user = _user_resp.json()
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

    # 白名单校验：所有用户（含已存在）都必须在白名单内，踢出白名单后立即无法登录
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

    # JWT 双 token——OAuth 也签发 refresh_token
    from src.api.jwt_utils import create_access_token, create_refresh_token
    token_version = user_db.get_token_version(local_user["id"])
    jwt_access_token = create_access_token(local_user["id"], local_user["username"], token_version)
    jwt_refresh_token = create_refresh_token(local_user["id"], local_user["username"], token_version)
    # 仅真正首次登录（无任何托管仓库）才 first_login=1（前端清 currentRepo 进仓库选择页）；
    # 已有托管仓库则保留上次选择的仓库（first_login=0），恢复上次的 repo
    has_repos = bool(user_db.get_managed_repos(local_user["id"]))
    # 从 cookie 读语言偏好（i18n.js setLang 时双写 localStorage + cookie）
    lang = request.cookies.get("grapeLang", "zh")
    prefix = f"/{lang}" if lang != "zh" else ""
    # refresh_token 通过 cookie 传递（HttpOnly）——URL 只带 access_token
    resp = RedirectResponse(url=f"{prefix}/?token={jwt_access_token}&first_login={'1' if not has_repos else '0'}", status_code=302)
    _set_refresh_cookie(resp, jwt_refresh_token)
    return resp


# ====== GitHub OAuth ======

def _oauth_error_redirect(request: Request, message: str):
    """OAuth 回调失败 → 重定向到前端登录页并带错误信息（前端 toast 提示）。"""
    import urllib.parse
    scheme = request.url.scheme
    host = request.url.hostname
    port = request.url.port
    # 前端 SPA 路由：/login?oauth_error=xxx
    base = f"{scheme}://{host}"
    if port and port not in (80, 443):
        base += f":{port}"
    _encoded = urllib.parse.quote(message)
    return RedirectResponse(url=f"{base}/login?oauth_error={_encoded}", status_code=302)


@router.get("/login/github")
def github_oauth_login(request: Request):
    """GitHub OAuth 第一步：跳转 GitHub 授权页。"""
    from src.user.db import get_meta_value
    client_id = get_meta_value("oauth_github_client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitHub OAuth 未配置"})

    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI") or get_meta_value("oauth_github_redirect_uri")
    if not redirect_uri:
        scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
        host = request.headers.get("Host", "localhost:80").split(":")[0]
        redirect_uri = f"{scheme}://{host}:80/api/auth/callback/github"

    state = hashlib.sha256(os.urandom(32)).hexdigest()
    user_db.save_oauth_state(state, "github", {})

    import urllib.parse
    auth_url = (f"https://github.com/login/oauth/authorize"
                f"?client_id={urllib.parse.quote(client_id)}"
                f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
                f"&response_type=code"
                f"&scope=repo%20user%20admin:repo_hook%20offline_access"
                f"&state={urllib.parse.quote(state)}")
    return {"redirect_url": auth_url}


@router.get("/callback/github")
def github_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """GitHub OAuth 回调：用 code 换 access_token → 获取用户信息 → 创建/绑定本地用户 → 签发 JWT。"""
    if error:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"GitHub 授权失败: {error}"})

    state_data = user_db.validate_oauth_state(state, "github")
    if state_data is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "state 无效或已过期"})

    from src.user.db import get_meta_value
    client_id = get_meta_value("oauth_github_client_id")
    client_secret = get_meta_value("oauth_github_client_secret")

    import urllib.parse, json
    _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""

    def _github_post(url, data, headers, timeout=15):
        """向 github.com 发 POST 请求。有代理走代理，否则直连。"""
        import requests as _req
        if _proxy:
            return _req.post(url, data=data, headers=headers, timeout=timeout, proxies={"https": _proxy, "http": _proxy})
        return _req.post(url, data=data, headers=headers, timeout=timeout)

    token_data = None
    try:
        _token_body = urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "code": code,
        }).encode()
        _token_headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        _resp = _github_post("https://github.com/login/oauth/access_token", data=_token_body, headers=_token_headers)
        token_data = _resp.json()
    except Exception as e:
        _msg = str(e)
        if "timed out" in _msg or "ConnectTimeout" in _msg or "Max retries" in _msg:
            _user_msg = "GitHub 登录失败：服务器无法连接 github.com（网络超时）。请稍后重试，或联系管理员配置代理。"
        else:
            _user_msg = f"GitHub 登录失败：{_msg[:120]}"
        return _oauth_error_redirect(request, _user_msg)

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        return _oauth_error_redirect(request, "GitHub 登录失败：未获取到 access_token，请重试。")

    github_user = None
    try:
        import requests as _req
        _user_headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
        _proxies = {"https": _proxy, "http": _proxy} if _proxy else None
        _user_resp = _req.get("https://api.github.com/user", headers=_user_headers, timeout=10, proxies=_proxies)
        _user_resp.raise_for_status()
        github_user = _user_resp.json()
    except Exception as e:
        return _oauth_error_redirect(request, f"GitHub 登录失败：获取用户信息失败（{str(e)[:80]}）")

    github_id = str(github_user.get("id", ""))
    github_login = github_user.get("login", "")
    github_name = github_user.get("name") or github_login
    if not github_id:
        return _oauth_error_redirect(request, "GitHub 登录失败：无法获取用户信息。")

    local_user = user_db.get_user_by_github_id(github_id)
    if not local_user:
        local_user = user_db.get_user_by_username(github_login)

    # 白名单校验
    from src.config import is_github_user_allowed
    if not is_github_user_allowed(github_login):
        return _oauth_error_redirect(request, "您的 GitHub 账号不在白名单内，无法登录本系统。请联系统管理员。")

    # 新用户才创建
    if not local_user:
        result = user_db.create_user(github_login, hashlib.sha256(os.urandom(16)).hexdigest(), 0)
        if not result:
            raise HTTPException(status_code=500, detail={"code": 500, "message": "创建用户失败"})
        local_user = user_db.get_user(result["id"])

    user_db.link_github_user(local_user["id"], github_id, access_token, refresh_token)

    user_acc = user_db.get_user_account(local_user["username"]) or {}
    user_acc["github_api_token"] = access_token
    if github_name:
        user_acc["nickname"] = github_name
    user_db.save_user_account(local_user["username"], user_acc)
    # WAL 模式下确保写入持久化到主 DB 文件（防止进程退出后丢失）
    try:
        from src.session_manager import db as _db, db_lock as _db_lock
        with _db_lock:
            _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    log.info(f"GitHub OAuth: user={local_user['username']} github_id={github_id} token={access_token[:10]}...{access_token[-5:]} saved+checkpointed")

    # 签发 JWT
    from src.api.jwt_utils import create_access_token, create_refresh_token
    token_version = user_db.get_token_version(local_user["id"])
    jwt_access_token = create_access_token(local_user["id"], local_user["username"], token_version)
    jwt_refresh_token = create_refresh_token(local_user["id"], local_user["username"], token_version)

    # OAuth App 不需要 installation 检查（scope 在授权 URL 里已指定）
    _gh_perm_warning = ""

    try:
        from src.session_manager import update_unified_user_info
        update_unified_user_info(github_login, open_id="", github_login=github_login)
    except Exception as e:
        log.warning(f"GitHub OAuth login: write user mapping failed: {e}")

    has_repos = bool(user_db.get_managed_repos(local_user["id"]))
    lang = request.cookies.get("grapeLang", "zh")
    prefix = f"/{lang}" if lang != "zh" else ""
    _warn_param = f"&gh_perm_warning={urllib.parse.quote(_gh_perm_warning)}" if _gh_perm_warning else ""
    resp = RedirectResponse(url=f"{prefix}/?token={jwt_access_token}&first_login={'1' if not has_repos else '0'}{_warn_param}", status_code=302)
    _set_refresh_cookie(resp, jwt_refresh_token)
    return resp


@router.get("/profile")
def get_profile(user: dict = Depends(require_no_session_token)):
    """用户信息——支持 JWT 会话 / grape_ API Key / tk_ 会话 token（Issue #69）。"""
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
        "github_bound": bool(user_db.get_credential(user["id"], "github_oauth")),
    }


@router.post("/logout")
def logout(user: dict = Depends(require_no_session_token), response: Response = None):
    """登出——token_version+1 撤销所有 token + 清 cookie。"""
    user_db.increment_token_version(user["id"])
    if response:
        _clear_refresh_cookie(response)
    return {"message": "已登出"}
