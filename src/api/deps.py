"""共享依赖：Token 验证、Token 三层体系、用户信息获取"""
import time
import threading
from fastapi import Header, HTTPException, Request, Depends
from src.user import db as user_db
from src.config import log

TOKEN_TYPE_DEVOPS = "devops_bot"
TOKEN_TYPE_USER = "user"
TOKEN_TYPE_WEBHOOK = "webhook"
TOKEN_TYPE_ADMIN = "admin"

# 用户活跃状态追踪——内存缓冲，定时批量 flush
_pending_active: set = set()

# 用户级 GitCode OAuth 刷新锁——同一用户并发 401 时只刷新一次（Issue #52）
_gitcode_refresh_locks: dict[int, threading.Lock] = {}
_gitcode_refresh_locks_guard = threading.Lock()

def mark_user_active(uid: int):
    """将用户 id 加入待更新集合——O(1)，不阻塞 API"""
    _pending_active.add(uid)

def get_pending_active() -> set:
    """获取并清空待更新集合（供定时任务调用）"""
    uids = set(_pending_active)
    _pending_active.clear()
    return uids


def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization: Bearer *** 提取并验证 JWT access token。

    JWT 无状态验证——签名+过期在 jwt_utils 验证。
    token_version 对比数据库——改密码/logout 后旧 token 失效。
    用户禁用状态校验——通过内存缓存优化（TTL 5 秒）。
    任何实例都能验证（集群会话共享）。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Not logged in"})
    token = authorization.replace("Bearer ", "").strip()
    # JWT access token 验证——签名+过期
    from src.api.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Session expired"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "User not found"})

    # token_version + 禁用状态校验（内存缓存优化，TTL 5 秒）
    db_ver, status, db_username = get_user_status_cached(uid)

    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Session revoked"})
    if status != 1:
        # 清除缓存，下次查库
        _user_status_cache.pop(uid, None)
        raise HTTPException(status_code=401, detail={"code": 401, "message": "账号已被禁用"})

    # Issue #343: username/id 一致性校验——防止旧 JWT sub=旧id 撞上新表有效用户
    # JWT payload 里的 username 必须和库里 sub 对应的 username 一致
    if db_username and username and db_username != username:
        _user_status_cache.pop(uid, None)
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Session invalid"})

    # 记录活跃状态——O(1)，不阻塞
    mark_user_active(uid)
    login_provider = payload.get("login_provider", "")
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_USER,
            "login_provider": login_provider}


# 用户状态内存缓存：uid → (token_version, status, username, expire_ts)
_user_status_cache: dict[int, tuple[int, int, str, float]] = {}


def get_user_status_cached(uid: int) -> tuple[int, int, str]:
    """获取用户 token_version + status + username，优先读缓存（TTL 5 秒）。

    D3 修复：提取为公共函数，供 get_current_user 和 refresh 路由共享缓存。
    Issue #343: 返回 username 用于 JWT 一致性校验。
    """
    cached = _user_status_cache.get(uid)
    now = time.time()
    if cached and cached[3] > now:
        return cached[0], cached[1], cached[2]
    # 缓存未命中，查库
    from src.auth.grape_account_db import get_status_and_token_version
    info = get_status_and_token_version(uid)
    if info is not None:
        status, db_ver, username = info
    else:
        # D32: grape_accounts 无此用户 → 不 fallback 旧表，视为无效
        db_ver = 0
        status = 0  # 无账号 = 禁用
        username = ""
    _user_status_cache[uid] = (db_ver, status, username, now + 5)
    return db_ver, status, username


def invalidate_user_cache(uid: int) -> None:
    """主动失效用户状态缓存（密码重置/禁用/登出时调用）。"""
    _user_status_cache.pop(uid, None)


def _extract_api_token(request) -> str:
    """三通道取值：Authorization: Bearer / PRIVATE-TOKEN header / ?access_token= query。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    private = request.headers.get("PRIVATE-TOKEN") or request.headers.get("private-token")
    if private:
        return private.strip()
    return (request.query_params.get("access_token") or "").strip()


def get_current_user_or_api_key(request: Request) -> dict:
    """对外业务 API 统一认证入口：JWT 会话 / 会话级临时 token（A）/ 用户级 API Key（B）三选一。

    三通道取值（Bearer / PRIVATE-TOKEN / access_token）后按 token 前缀分流：
    - `tk_`    → 会话级临时 token（session_api_tokens 校验，返回 repo/agent 上下文）→ auth_type=session_token
    - `grape_` → 用户级长期 API Key（api_tokens 校验，绑定单用户）→ auth_type=api_key
    - 其他     → JWT access token（复用 get_current_user）→ auth_type=jwt

    返回 dict 与 get_current_user 兼容（含 id/username/token），并附加认证类型与作用域字段，
    供内部接口按需读取（如 repo_key/agent_type 做仓库级鉴权）。
    """
    from src.api.auth_token import SESSION_TOKEN_PREFIX, API_KEY_PREFIX, verify_session_token, verify_api_token
    token = _extract_api_token(request)
    if not token:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Not logged in"})

    if token.startswith(SESSION_TOKEN_PREFIX):
        info = verify_session_token(token)
        if not info:
            raise HTTPException(status_code=401, detail={"code": 401, "message": "Invalid or expired session token"})
        return {
            "id": info["user_id"],
            "username": "",
            "token": token,
            "token_type": "session_token",
            "auth_type": "session_token",
            "session_key": info["session_key"],
            "repo_key": info["repo_key"],
            "agent_type": info["agent_type"],
        }

    if token.startswith(API_KEY_PREFIX):
        info = verify_api_token(token)
        if not info:
            raise HTTPException(status_code=401, detail={"code": 401, "message": "Invalid or revoked API Key"})
        uid = info["user_id"]
        # SEC-3: API Key 路径也需校验用户 status
        db_ver, status, _ = get_user_status_cached(uid)
        if status != 1:
            _user_status_cache.pop(uid, None)
            raise HTTPException(status_code=401, detail={"code": 401, "message": "账号已被禁用"})
        # D32: 从 grape_accounts 读 username
        from src.auth.grape_account_db import get_grape_account_by_id
        acc = get_grape_account_by_id(uid) or {}
        mark_user_active(uid)
        return {
            "id": uid,
            "username": acc.get("username", ""),
            "token": token,
            "token_type": API_KEY_PREFIX,
            "auth_type": "api_key",
            "api_token_id": info["api_token_id"],
            "token_name": info["name"],
        }

    # JWT 会话（仅 Bearer 通道承载）；原流程不变
    user = get_current_user(authorization=request.headers.get("authorization"))
    user["auth_type"] = "jwt"
    return user


def get_user_or_none(request: Request) -> dict | None:
    """A2A 软认证依赖：JWT / 会话 token / API Key 任一通过返回 user，否则 None。

    与 `get_current_user_or_api_key` 区别：不因 token 无效抛 401，而是返回 None，
    供 A2A 端点先把用户级身份放行、再把 bot-token 等内部鉴权路径交给 `_authorize_a2a`。
    """
    try:
        return get_current_user_or_api_key(request)
    except HTTPException:
        return None


def require_admin(authorization: str = Header(None)) -> dict:
    """管理后台鉴权：JWT access token 且 role=admin，token_version 与 admin_accounts 一致。

    - 普通用户 JWT（role 为空或非 admin）→ 403
    - 校验失败 → 401
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Not logged in"})
    token = authorization.replace("Bearer ", "").strip()
    from src.api.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Session expired"})
    if payload.get("role") != TOKEN_TYPE_ADMIN:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Admin only"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "User not found"})
    from src.admin.db import get_admin_token_version
    db_ver = get_admin_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Session revoked"})
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_ADMIN}


def require_no_session_token(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """账户/平台管理端点鉴权：session_token（tk_）→ 403；API Key / JWT 维持现状放行。

    Phase token-scope（docs/weekly-03/token-scope-classification-design.md）：
    防止 agent 会话凭据（tk_）用于账户/平台管理操作；`grape_` API Key 与 Web JWT 不受影响。
    经 Depends 复用 get_current_user_or_api_key（override 可注入，测试友好）。
    """
    if user.get("auth_type") == "session_token":
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "Session token not allowed on this endpoint (account/platform management)"})
    return user


def require_jwt_only(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """高敏感账号管理端点鉴权：仅允许 JWT，禁止 session_token 和 api_key。

    P1-6: API key 不过期、不加密存储、泄露后可永久操控账号。
    修改密码 / 设置密码 / 变更邮箱 / 注销账号 / 绑定解绑 等高敏感操作
    不应通过长期 API key 执行，仅允许 Web JWT（有过期时间 + refresh 轮换）。
    """
    auth_type = user.get("auth_type", "jwt")
    if auth_type != "jwt":
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "This endpoint requires interactive login (JWT). API keys and session tokens are not allowed."})
    return user


def require_org_member(request: Request, user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """组织成员校验依赖。返回 {**user, "org_id": int, "org_role": str}。

    解析 X-Org-ID/X-Org-Slug/ query 参数获取组织上下文；
    未指定时回退到用户的个人组织（兼容现有 API 行为）。
    """
    from src.api.org_context import resolve_org_context
    from src.user.org_db import get_member_role
    org_id = resolve_org_context(request, user.get("id") or 0)
    role = get_member_role(user.get("id") or 0, org_id)
    return {**user, "org_id": org_id, "org_role": role}


def require_org_admin(user: dict = Depends(require_org_member)) -> dict:
    """组织管理员校验（owner/admin）。"""
    if user.get("org_role") not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "需要组织管理员权限"})
    return user


def _get_oauth_token_from_identity(username: str, provider: str) -> str:
    """从 external_identities 表读取用户的 OAuth access_token。

    username → grape_accounts.id → external_identities.access_token（解密）
    返回 token 或空字符串。
    """
    try:
        from src.auth.grape_account_db import get_grape_account_by_username
        acc = get_grape_account_by_username(username)
        if not acc:
            return ""
        from src.auth.grape_account_db import get_external_identity
        # external_identities 按 grape_user_id + provider 查
        # get_external_identity 是按 provider + external_id 查的，不能直接用
        from src.session_manager import db, db_lock
        from src.auth.credential_crypto import decrypt
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT access_token FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (acc["id"], provider),
            )
            row = c.fetchone()
        if row and row[0]:
            return decrypt(row[0])
    except Exception as e:
        log.warning(f"_get_oauth_token_from_identity({username}, {provider}): {e}")
    return ""


def get_user_token(username: str) -> str:
    """获取用户的 GitCode API Token（D32: 只从 external_identities 读，不 fallback 老表）。"""
    return _get_oauth_token_from_identity(username, "gitcode")


def get_user_github_token(username: str) -> str:
    """获取用户的 GitHub API Token（D32: 只从 external_identities 读，不 fallback 老表）。"""
    return _get_oauth_token_from_identity(username, "github")


def get_user_github_token_with_refresh(username: str, user_id: int = 0) -> str:
    """获取用户 GitHub token，过期时主动预刷新（Issue #343）。

    和 get_user_token_with_refresh（GitCode 版）对称：
    优先从 external_identities 读，检查 user_credentials 中的 token_expires_at，
    临近过期时调用 refresh_oauth_token 预刷新。
    """
    import time as _time
    token = _get_oauth_token_from_identity(username, "github")

    if token and user_id > 0:
        cred = user_db.get_credential(user_id, "github_oauth")
        if cred:
            now = int(_time.time())
            need_refresh = False
            token_expires_at = cred.get("token_expires_at", 0)
            # 兼容 int 和 TEXT 两种格式
            if isinstance(token_expires_at, str):
                from datetime import datetime as _dt
                try:
                    token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").timestamp())
                except (ValueError, OSError):
                    token_expires_at = 0
            if token_expires_at:
                need_refresh = (token_expires_at - now) < 600
            else:
                fetched_at = cred.get("fetched_at", 0)
                age = now - fetched_at if fetched_at else 0
                need_refresh = age > 1800
            if need_refresh:
                new_token = user_db.refresh_oauth_token(user_id, "github")
                return new_token or token
        return token

    if user_id > 0 and not token:
        return user_db.refresh_oauth_token(user_id, "github") or ""

    return token


def _get_user_refresh_lock(user_id: int) -> threading.Lock:
    """获取用户级别的 GitCode OAuth 刷新锁（懒创建，Issue #52）。"""
    with _gitcode_refresh_locks_guard:
        if user_id not in _gitcode_refresh_locks:
            _gitcode_refresh_locks[user_id] = threading.Lock()
        return _gitcode_refresh_locks[user_id]


def try_refresh_gitcode_token(user_id: int) -> str | None:
    """尝试刷新 GitCode OAuth token（加锁，防并发重复刷新，Issue #52）。

    返回新 token 或 None（刷新失败 / 无 refresh_token）。
    """
    lock = _get_user_refresh_lock(user_id)
    with lock:
        return user_db.refresh_gitcode_oauth_token(user_id)


def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    """获取用户 GitCode token，过期时主动预刷新（Issue #52 + Issue #343）。

    Issue #343: 改用 token_expires_at 实际过期时间预刷新（提前 10 分钟），
    无 token_expires_at 时降级用 fetched_at + 30 分钟（匹配实测最短寿命）。

    Issue #343 修复: token_expires_at 在 user_credentials(config_json) 中存为 epoch int，
    在 external_identities 列中存为 TEXT 格式（如 "2026-10-15 12:00:00"）。
    本函数从 user_credentials 读，值为 int，直接做数值比较。
    如从 external_identities 读 TEXT 格式，用 _parse_token_expires_at 统一转 epoch int。
    """
    import time as _time
    # 1. 优先从 external_identities 读
    token = _get_oauth_token_from_identity(username, "gitcode")

    if token and user_id > 0:
        # 检查 token 是否临近过期
        cred = user_db.get_credential(user_id, "gitcode_oauth")
        if cred:
            now = int(_time.time())
            need_refresh = False
            token_expires_at = cred.get("token_expires_at", 0)
            # 统一转 epoch int（兼容 int 和 TEXT 两种格式）
            if isinstance(token_expires_at, str):
                from datetime import datetime as _dt
                try:
                    token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").timestamp())
                except (ValueError, OSError):
                    token_expires_at = 0
            if token_expires_at:
                # 有过期时间：距过期不足 10 分钟（600 秒）则预刷新
                need_refresh = (token_expires_at - now) < 600
            else:
                # 降级：无过期时间（历史数据），用 fetched_at + 30 分钟
                # Issue #343: 实测最短 15 分钟，30 分钟降级比 60 分钟更安全
                fetched_at = cred.get("fetched_at", 0)
                age = now - fetched_at if fetched_at else 0
                need_refresh = age > 1800
            if need_refresh:
                new_token = try_refresh_gitcode_token(user_id)
                return new_token or token
        return token

    # D32: 删除老表 fallback，直接尝试刷新
    if user_id > 0 and not token:
        return try_refresh_gitcode_token(user_id) or ""

    return token


def get_bot_token(repo_owner: str = "", repo_name: str = "") -> str:
    """获取仓库绑定的 DevOps Bot Token（DevOps Agents 使用）"""
    if repo_owner and repo_name:
        from src.user.db import get_bot_token_by_repo
        return get_bot_token_by_repo(repo_owner, repo_name)
    return ""


def require_devops_token(user: dict = Depends(get_current_user)) -> dict:
    """确保该端点只能由拥有 Bot Token 配置的管理员访问"""
    # 检查用户是否有任一托管仓库配置了 Bot Token
    repos = user_db.get_managed_repos(user["id"])
    has_bot = any(r.get("bot_gitcode_token", "") for r in repos)
    if not has_bot:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "DevOps Bot Token not configured, please configure it in GitCode Repo page"})
    return user


def check_repo_access(user: dict, repo_key: str) -> None:
    """校验当前用户是否有权访问指定仓库（managed_repos 中存在该仓库）。
    
    用于 dashboard/events/pipeline/agents 等按 repo_key 查询的接口，防止用户访问他人仓库的数据。
    session_token / api_key 认证方式跳过校验（已由 token 作用域限定）。
    同时检查 gitcode: 和 github: 前缀——前端 URL 不含平台前缀，to_repo_key 默认加 gitcode:，
    但用户可能通过 GitHub OAuth 登录，仓库实际是 github: 前缀。
    """
    auth_type = user.get("auth_type", "jwt")
    if auth_type in ("session_token", "api_key"):
        return  # 作用域已限定
    uid = user.get("id", 0)
    if not uid or not repo_key:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "No permission to access this repository"})
    from src.repo_key import to_repo_key
    rk = to_repo_key(repo_key)
    # 构建候选 repo_key 列表：原始 + 跨平台前缀（gitcode:↔github:）
    base = rk.split(":", 1)[1] if ":" in rk else rk
    candidates = {f"gitcode:{base}", f"github:{base}"}
    repos = user_db.get_managed_repos(uid)
    for r in repos:
        if r["repo_key"] in candidates:
            return  # 用户有权访问
    raise HTTPException(status_code=403, detail={"code": 403, "message": "No permission to access this repository"})
