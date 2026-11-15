"""共享依赖：Token 验证、Token 三层体系、用户信息获取"""

import time

import threading

from fastapi import Header, HTTPException, Request, Depends

from src.domains.mgmt.user import db as user_db

from src.kernel.config import log

def _parse_utc_timestamp(s: str) -> int:
    """FR-13: 将 TEXT 格式的时间戳按 UTC 解释为 epoch int。

    grape_account_db 写入 gmtime UTC 字符串，读侧必须按 UTC 解释。
    解析失败返回 0（触发 30 分钟 fallback 刷新逻辑）。
    """
    from datetime import datetime as _dt, timezone as _tz
    try:
        return int(_dt.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc).timestamp())
    except (ValueError, OSError):
        # Issue #512 同族：expires 写入方存在 UTC ISO 口径（T 分隔+微秒+offset）
        try:
            return int(_dt.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
        except (ValueError, OSError, TypeError):
            return 0

TOKEN_TYPE_DEVOPS = "devops_bot"

TOKEN_TYPE_USER = "user"

TOKEN_TYPE_WEBHOOK = "webhook"

TOKEN_TYPE_ADMIN = "admin"

# 用户活跃状态追踪——内存缓冲，定时批量 flush
_pending_active: set = set()

# 用户级 GitCode OAuth 刷新锁——同一用户并发 401 时只刷新一次（Issue #52）
_gitcode_refresh_locks: dict[int, threading.Lock] = {}

_gitcode_refresh_locks_guard = threading.Lock()

def mark_user_active(uid: str):
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
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "NOT_LOGGED_IN", "message": "Not logged in"})
    token = authorization.replace("Bearer ", "").strip()
    # JWT access token 验证——签名+过期
    from src.kernel.auth_sdk.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_EXPIRED", "message": "Session expired"})
    uid = str(payload.get("sub") or "").strip()     # D105 第二批：sub 统一字符串化（老 int sub → "24"，新 UUID 原样）
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "USER_NOT_FOUND", "message": "User not found"})

    # token_version + 禁用状态校验（内存缓存优化，TTL 5 秒）
    db_ver, status, db_username = get_user_status_cached(uid)

    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_REVOKED", "message": "Session revoked"})
    if status != 1:
        # 清除缓存，下次查库
        _user_status_cache.pop(uid, None)
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DISABLED", "message": "账号已被禁用"})

    # Issue #343: username/id 一致性校验——防止旧 JWT sub=旧id 撞上新表有效用户
    # JWT payload 里的 username 必须和库里 sub 对应的 username 一致
    if db_username and username and db_username != username:
        _user_status_cache.pop(uid, None)
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_INVALID", "message": "Session invalid"})

    # 记录活跃状态——O(1)，不阻塞
    mark_user_active(uid)
    login_provider = payload.get("login_provider", "")
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_USER,
            "login_provider": login_provider}

# 用户状态内存缓存：uid → (token_version, status, username, expire_ts)
_user_status_cache: dict[int, tuple[int, int, str, float]] = {}

def get_user_status_cached(uid: str) -> tuple[int, int, str]:
    """获取用户 token_version + status + username，优先读缓存（TTL 5 秒）。

    D3 修复：提取为公共函数，供 get_current_user 和 refresh 路由共享缓存。
    Issue #343: 返回 username 用于 JWT 一致性校验。
    """
    cached = _user_status_cache.get(uid)
    now = time.time()
    if cached and cached[3] > now:
        return cached[0], cached[1], cached[2]
    # 缓存未命中，查库
    from src.domains.mgmt.auth.grape_account_db import get_status_and_token_version
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

def invalidate_user_cache(uid: str) -> None:
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
    from src.kernel.auth_sdk.auth_token import (
        SESSION_TOKEN_PREFIX, API_KEY_PREFIX, SESSION_TOKEN_RECOVERY_HINT,
        TAKEOVER_TOKEN_PREFIX, verify_session_token, verify_api_token, verify_takeover_token,
    )
    token = _extract_api_token(request)
    if not token:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "NOT_LOGGED_IN", "message": "Not logged in"})

    # 接管会话（tko_）：与 tk_ 同类（repo 作用域），但只用于"用户接手任务"场景。
    # 能力矩阵见 src/api/auth_caps.py：repo_session=True，agent_self/user_scope=False。
    if token.startswith(TAKEOVER_TOKEN_PREFIX):
        info = verify_takeover_token(token)
        if not info:
            raise HTTPException(status_code=401, detail={"code": 401,
                                                         "error_code": "TAKEOVER_TOKEN_INVALID",
                                                         "message": "Invalid or expired takeover token"})
        uid = info["user_id"]
        try:
            from src.domains.mgmt.user.grape_account_db import get_grape_account_by_id
            _acc = get_grape_account_by_id(uid) or {}
        except Exception:
            _acc = {}
        return {
            "id": uid,
            "username": _acc.get("username", ""),
            "token": token,
            "token_type": "takeover",
            "auth_type": "takeover",
            "session_key": info["session_key"],
            "repo_key": info["repo_key"],
            "agent_type": info["agent_type"],
            "kind": "takeover",
        }

    if token.startswith(SESSION_TOKEN_PREFIX):
        info = verify_session_token(token)
        if not info:
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_TOKEN_INVALID", "message": SESSION_TOKEN_RECOVERY_HINT})
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
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "API_KEY_INVALID", "message": "Invalid or revoked API Key"})
        uid = info["user_id"]
        # SEC-3: API Key 路径也需校验用户 status
        db_ver, status, _ = get_user_status_cached(uid)
        if status != 1:
            _user_status_cache.pop(uid, None)
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DISABLED", "message": "账号已被禁用"})
        # D32: 从 grape_accounts 读 username
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
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
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "NOT_LOGGED_IN", "message": "Not logged in"})
    token = authorization.replace("Bearer ", "").strip()
    from src.kernel.auth_sdk.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_EXPIRED", "message": "Session expired"})
    if payload.get("role") != TOKEN_TYPE_ADMIN:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Admin only"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "USER_NOT_FOUND", "message": "User not found"})
    from src.domains.mgmt.admin.db import get_admin_token_version
    db_ver = get_admin_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_REVOKED", "message": "Session revoked"})
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_ADMIN}

def require_user_or_takeover(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """允许"用户身份"或"接管 token"（tko_）。

    唯一用途：接管会话自己上报进展（`POST /api/takeover/sessions/{id}/progress`）。
    接管 token 没有 user_scope（设计使然），但它对**自己那条会话**写进展是安全的，
    路由里再用 session_key 校验归属。
    """
    if str(user.get("auth_type") or "") == "takeover":
        return user
    from src.api.auth_caps import USER_SCOPE, has_capability
    if not has_capability(user, USER_SCOPE):
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "CAPABILITY_REQUIRED",
            "message": f"需要 {USER_SCOPE} 能力（当前：{user.get('auth_type') or 'unknown'}）",
        })
    return user


def require_no_session_token(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """账户/平台管理端点鉴权 = 需要 `user_scope` 能力。

    Phase token-scope（docs/weekly-03/...）+ ADR-2：能力判定收敛到 `src/api/auth_caps.py`，
    session_token（tk_）与 takeover（tko_）都没有 user_scope → 403；API Key / JWT / internal 放行。
    保留函数名是为了不动 30+ 处既有 import 与路由签名。
    """
    from src.api.auth_caps import USER_SCOPE, has_capability
    if not has_capability(user, USER_SCOPE):
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "Session token not allowed on this endpoint (account/platform management)"})
    return user

def require_jwt_only(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """高敏感账号管理端点鉴权 = 需要 `interactive_only` 能力（仅 JWT）。

    P1-6: API key 不过期、不加密存储、泄露后可永久操控账号。
    修改密码 / 设置密码 / 变更邮箱 / 注销账号 / 绑定解绑 等高敏感操作
    不应通过长期 API key 执行，仅允许 Web JWT（有过期时间 + refresh 轮换）。
    """
    from src.api.auth_caps import INTERACTIVE_ONLY, has_capability
    if not has_capability(user, INTERACTIVE_ONLY):
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "This endpoint requires interactive login (JWT). API keys and session tokens are not allowed."})
    return user

def require_org_member(request: Request, user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """组织成员校验依赖。返回 {**user, "org_id": str, "org_role": str}。

    解析 X-Org-ID/X-Org-Slug/ query 参数获取组织上下文；
    未指定时回退到用户的个人组织（兼容现有 API 行为）。
    D105 第三批：org_id 为 str（''=未归属哨兵，原 0）。
    """
    from src.api.org_context import resolve_org_context
    from src.domains.mgmt.user.org_db import get_member_role
    org_id = resolve_org_context(request, str(user.get("id") or ""))
    role = get_member_role(str(user.get("id") or ""), org_id)
    return {**user, "org_id": org_id, "org_role": role}

def require_path_org_member(user_id: str, org_id: str) -> str:
    """路径型组织接口鉴权（Issue #381 / #395）：鉴权对象必须是路径里的 org_id。

    与 require_org_member（上下文型）解耦：
    - 组织不存在 → 404
    - 非成员 → 403
    返回成员角色（member/admin/owner）。
    D105 第三批：org_id 为 str（uuid/保值 int 字符串形态）。

    修复前缺陷：PUT/DELETE /api/orgs/{org_id} 用 resolve_org_context 回退个人组织
    鉴权（读 403、写 200 语义矛盾，且可越权写任意组织）。
    """
    from src.domains.mgmt.user.org_db import get_org, get_member_role
    if get_org(org_id) is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "组织不存在"})
    role = get_member_role(user_id, org_id)
    if not role:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})
    return role

def require_path_org_admin(user_id: str, org_id: str) -> str:
    """路径型组织接口管理员鉴权（Issue #381 / #395）：先成员校验，再校验 owner/admin。"""
    role = require_path_org_member(user_id, org_id)
    if role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "需要组织管理员权限"})
    return role

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
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        acc = get_grape_account_by_username(username)
        if not acc:
            return ""
        from src.domains.mgmt.auth.grape_account_db import get_external_identity
        # external_identities 按 grape_user_id + provider 查
        # get_external_identity 是按 provider + external_id 查的，不能直接用
        # 归位：表归 mgmt，走任意拓扑原语（api 进程库无该表，视图 0）
        from src.domains.mgmt.public import oauth_token_status_any
        from src.domains.mgmt.auth.credential_crypto import decrypt
        row = oauth_token_status_any(acc["id"], provider)
        if row and row[0]:
            _token = decrypt(row[0])
            # F3 修复（R9.6）：status IN (1, 4) 统一——status=4（token 过期）时仍返回 token
            # （R10 issue 回帖需要用过期 token 发评论，因为重新获取也是同一个过期 token——
            # 评论发不出去是预期行为，用户需重新绑定）
            # 本查询与 get_external_identity 的 status 口径统一（status IN (1, 4)）。
            return _token
    except Exception as e:
        log.warning(f"_get_oauth_token_from_identity({username}, {provider}): {e}")
    return ""

def get_user_token(username: str) -> str:
    """获取用户的 GitCode API Token（D32: 只从 external_identities 读，不 fallback 老表）。

    D105k 解耦收口：委托 get_user_token_for（①凭据域 <p>_pat ②OAuth ei），
    不再直读 ei——专用字段 PAT 路径已改写凭据域，本函数保留签名仅为存量调用方。
    """
    try:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        acc = get_grape_account_by_username(username)
        if not acc:
            return ""
        return get_user_token_for(username, user_id=str(acc.get("id") or ""), provider="gitcode")
    except Exception as e:
        log.warning(f"get_user_token({username}): {e}")
        return ""

def get_user_github_token(username: str) -> str:
    """获取用户的 GitHub API Token（D32: 只从 external_identities 读，不 fallback 老表）。"""
    return _get_oauth_token_from_identity(username, "github")

def _get_user_refresh_lock(user_id: str) -> threading.Lock:
    """获取用户级别的 GitCode OAuth 刷新锁（懒创建，Issue #52）。"""
    with _gitcode_refresh_locks_guard:
        if user_id not in _gitcode_refresh_locks:
            _gitcode_refresh_locks[user_id] = threading.Lock()
        return _gitcode_refresh_locks[user_id]

def try_refresh_gitcode_token(user_id: str) -> str | None:
    """尝试刷新 GitCode OAuth token（加锁，防并发重复刷新，Issue #52）。

    R20.27.1: 加分布式锁 oauth_refresh:{user_id}:gitcode，防止双主集群跨实例竞态。
    R20.27.2: 进入分布式锁后先读 token_expires_at，已刷新则跳过。
    R20.27.3: 刷新失败不清除旧 token。
    R20.27.5: 分布式锁超时 30s。

    返回新 token 或 None（刷新失败 / 无 refresh_token）。
    """
    # R20.27.1: 外层分布式锁
    try:
        from src.kernel.lock import acquire_lock, release_lock
        _lock_name = f"oauth_refresh:{user_id}:gitcode"
        if acquire_lock(_lock_name, ttl=30):
            try:
                # R20.27.2: 进入锁后先检查是否已被其他实例刷新
                # 进程内锁仍保留作为内层锁
                lock = _get_user_refresh_lock(user_id)
                with lock:
                    return user_db.refresh_gitcode_oauth_token(user_id)
            finally:
                release_lock(_lock_name)
        else:
            # 未抢到锁 = 其他实例在刷新，等待后读最新 token
            import time as _t
            _t.sleep(1)
            # R20.27.2: 读最新 access_token（其他实例可能已刷新）
            # 归位：external_identities 归 mgmt，走任意拓扑原语（api 进程库无该表）
            try:
                from src.domains.mgmt.public import stored_access_token_any
                from src.domains.mgmt.auth.credential_crypto import decrypt
                _raw = stored_access_token_any(user_id, "gitcode")
                if _raw:
                    return decrypt(_raw)
            except Exception as e:  # noqa: BLE001 - 等锁降级路径：读不到等价于「未被刷新」
                log.debug(f"try_refresh_gitcode_token: token re-read failed: {e}")
            return None
    except Exception as e:
        log.warning(f"try_refresh_gitcode_token: distributed lock failed: {e}")
        # 降级到进程内锁
        lock = _get_user_refresh_lock(user_id)
        with lock:
            return user_db.refresh_gitcode_oauth_token(user_id)

def _try_refresh_github_token(user_id: str) -> str | None:
    """R20.27.4: 尝试刷新 GitHub OAuth token（加分布式锁）。

    与 try_refresh_gitcode_token 对称，锁名 oauth_refresh:{user_id}:github。
    """
    try:
        from src.kernel.lock import acquire_lock, release_lock
        _lock_name = f"oauth_refresh:{user_id}:github"
        if acquire_lock(_lock_name, ttl=30):
            try:
                return user_db.refresh_oauth_token(user_id, "github")
            finally:
                release_lock(_lock_name)
        else:
            # 未抢到锁 = 其他实例在刷新，等待后读最新 token
            import time as _t
            _t.sleep(1)
            # R20.27.2: 读最新 access_token（其他实例可能已刷新）
            # 归位：external_identities 归 mgmt，走任意拓扑原语（api 进程库无该表）
            try:
                from src.domains.mgmt.public import stored_access_token_any
                from src.domains.mgmt.auth.credential_crypto import decrypt
                _raw = stored_access_token_any(user_id, "github")
                if _raw:
                    return decrypt(_raw)
            except Exception as e:  # noqa: BLE001 - 等锁降级路径：读不到等价于「未被刷新」
                log.debug(f"_try_refresh_github_token: token re-read failed: {e}")
            return None
    except Exception as e:
        log.warning(f"_try_refresh_github_token: distributed lock failed: {e}")
        return user_db.refresh_oauth_token(user_id, "github")



def get_bot_token(repo_owner: str = "", repo_name: str = "") -> str:
    """获取仓库绑定的 DevOps Bot Token（DevOps Agents 使用）。

    R12.13: 改用 get_repo_token_with_refresh（路径 B 仓库返回用户 token）。
    """
    if repo_owner and repo_name:
        from src.domains.mgmt.user.db import get_repo_token_with_refresh
        from src.kernel.repo_key import to_repo_key
        return get_repo_token_with_refresh(to_repo_key(f"{repo_owner}/{repo_name}")) or ""
    return ""

def require_devops_token(user: dict = Depends(get_current_user)) -> dict:
    """确保该端点只能由拥有 Bot Token 配置的管理员访问"""
    # 检查用户是否有任一托管仓库配置了 Bot Token
    repos = user_db.get_managed_repos(user["id"])
    # S82/D58：凭据唯一权威=grape_t_repo_credentials（列已冻结）——rc 感知判定
    has_bot = any(r.get("bot_gitcode_token", "") for r in repos) or \
        user_db.any_repo_credential_for_repos([r.get("repo_key") for r in repos])
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
    # ADR-2 注：takeover（tko_）**故意不在此列** → 走下面的真实 repo 校验。
    # 它带真实 user_id，managed_repos 可查；这样"接管 token 只能碰自己的仓库"由归属校验兜底，
    # 不需要额外的 admin 豁免（见设计文档 §5.6 / §13-4）。
    uid = str(user.get("id") or "")
    if not uid or not repo_key:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "No permission to access this repository"})
    from src.kernel.repo_key import to_repo_key, repo_key_candidates
    rk = to_repo_key(repo_key)
    # D16/S25：候选集由平台卡清单派生（新平台自动加入，修复双前缀功能 bug）
    candidates = set(repo_key_candidates(rk))
    repos = user_db.get_managed_repos(uid)
    for r in repos:
        if r["repo_key"] in candidates:
            return  # 用户有权访问
    raise HTTPException(status_code=403, detail={"code": 403, "message": "No permission to access this repository"})

# D40/S60：刷新实现注册（渠道能力声明集中一处，替代调用点字符串相等；
# 置于两个实现定义之后，模块导入时完成注册）
from src.kernel.auth_sdk.provider_link import register_refresh_implementation as _register_refresh_impl

def get_user_token_for(username: str, user_id: str = "", provider: str = "gitcode") -> str:
    """统一取用户平台 token（D3/K1 消除）：provider 无关，预刷新语义一致。

    D105k 解耦收口：解析序 = ①凭据域 <p>_pat（用户显式配置=明确意图，命中即返，
    PAT 无刷新语义）→ ② external_identities OAuth（带预刷新）。
    - token 只从 external_identities 读（登录凭据域），不按登录身份全局匹配
      旧凭据（K4 兜底已删，S22 检测工具负责存量错位数据的报告与人工修复）
    - 无 refresh_token 的凭据（PAT/导入）不刷新（R2 语义，S3 用例锁）
    - 临近过期预刷新（提前 10 分钟；无 token_expires_at 降级 fetched_at+30min）
    - 刷新失败标记 identity expired（FR-7.1），返回旧 token
    - 无 token 时尝试刷新一次（D32：不再 fallback 老表）
    """
    import time as _time
    # D105：user_id 统一字符串化（UUID 主键），空串即"未提供"，替代旧 int <= 0 判定
    user_id = str(user_id or "").strip()
    # D105k ①：凭据域 PAT（显式配置优先于 OAuth 供给——与 token_resolver
    # fetch_user_token 单序口径一致）。无 user_id 无法定位凭据归属 → 跳过。
    if user_id:
        try:
            from src.domains.mgmt.user.db import get_credential
            _pat = str((get_credential(user_id, provider, kind="pat") or {}).get("token", "") or "")
            if _pat:
                return _pat
        except Exception as e:
            log.warning(f"get_user_token_for: credential-domain PAT lookup failed "
                        f"(user={user_id}, provider={provider}): {e}")
    token = _get_oauth_token_from_identity(username, provider)
    if not token and user_id == "":
        return ""
    if token and user_id != "":
        from src.domains.mgmt.auth.grape_account_db import _has_refresh_token
        if not _has_refresh_token(user_id, provider):
            return token
        # D100（2026-11-08）R1：预刷新判定改读 external_identities.token_expires_at
        # （权威存储；原读 user_credentials <p>_oauth 快照的 fetched_at/
        # token_expires_at——快照已清退 W1 双写消灭，此处同步改道）。
        # expires 格式为 gmtime UTC TEXT（update_external_identity_tokens 写入
        # 口径），_parse_utc_timestamp 按 UTC 解释；解析失败=0 → 走 30 分钟
        # fetched_at fallback 不可用（ei 无 fetched_at），退化为不预刷新
        # （token 真实过期由 401 兜底链 oauth_401_retry 接管，语义安全）。
        # 归位：external_identities 归 mgmt，走任意拓扑原语（api 进程库无该表，
        # 视图 0 → 1146，被下方 except 吞成「不预刷新」）
        from src.domains.mgmt.public import token_expires_at_any
        try:
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username as _gbau
            _acc = _gbau(username)
            if not _acc:
                return token
            _expires_raw = token_expires_at_any(_acc["id"], provider)
            token_expires_at = _parse_utc_timestamp(_expires_raw) if _expires_raw else 0
            now = int(_time.time())
            if token_expires_at and (token_expires_at - now) < 600:
                new_token = _try_refresh_user_token(user_id, provider)
                if not new_token:
                    from src.domains.mgmt.auth.grape_account_db import mark_identity_expired
                    try:
                        mark_identity_expired(user_id, provider)
                    except Exception:
                        pass
                return new_token or token
        except Exception as e:
            log.warning(f"pre-refresh expiry check failed (user={user_id}, "
                        f"provider={provider}): {e}")
        return token
    if user_id != "" and not token:
        return _try_refresh_user_token(user_id, provider) or ""
    return token

def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    """兼容入口：GitCode token + 临近过期预刷新。

    多平台解耦（b439764fd）后 provider 语义统一到 ``get_user_token_for``，
    repos/routes 等存量调用方仍按旧签名调用，这里做一跳转发，避免
    「函数被删、调用方还在」的导入期崩溃（2026-11-12 合并回归）。
    """
    return get_user_token_for(username, user_id=str(user_id or ""), provider="gitcode")


def get_user_github_token_with_refresh(username: str, user_id: int = 0) -> str:
    """兼容入口：GitHub token + 临近过期预刷新（同上，provider='github'）。"""
    return get_user_token_for(username, user_id=str(user_id or ""), provider="github")


def _try_refresh_user_token(user_id: str, provider: str) -> "str | None":
    """按 provider 派发刷新（D40/S60：显式注册表派发，消平台名字面量）。

    注册表在 provider_link（身份域）——gitcode/github 刷新实现由本模块
    底部注册；config-only/未注册渠道返回 None（PAT-only 平台无
    refresh_token、无从刷新的语义显式化，非 else 兜底）。刷新责任全部
    留在登录域（仓库域只消费短期 access token 结果）。
    """
    from src.kernel.auth_sdk.provider_link import refresh_token_for
    return refresh_token_for(user_id, provider)

def get_user_token_for_repo(repo_key: str, username: str, user_id: str = "") -> str:
    """D37/S57：按 repo_key 前缀派生 provider 取用户 token（调用方零平台分支）。

    gitee/gitlab 等新平台不再折叠为 gitcode——脱敏/chat 直连此前落
    get_user_token_with_refresh（默认 provider=gitcode）导致 gitee 仓取空。
    裸 key = 存量默认 gitcode（与 get_user_token_with_refresh 语义一致）。
    """
    from src.kernel.repo_key import parse_repo_key
    _provider = parse_repo_key(repo_key or "")[0]
    return get_user_token_for(username, user_id=user_id, provider=_provider)

# 用户级 GitCode OAuth 刷新锁——同一用户并发 401 时只刷新一次（Issue #52）
_gitcode_refresh_locks: dict[str, threading.Lock] = {}  # D105 第二批：key=user_id(str)

# 用户状态内存缓存：uid → (token_version, status, username, expire_ts)
_user_status_cache: dict[str, tuple[int, int, str, float]] = {}  # D105 第二批：key=user_id(str)

_register_refresh_impl("gitcode", try_refresh_gitcode_token)

_register_refresh_impl("github", _try_refresh_github_token)


# ---------------------------------------------------------------------------
# K1 通用化（S57/D37）：arch 基线遗留的平台专用取 token 符号不写死在代码里，
# 由平台卡的 `legacy_token_symbol` 字段声明、此处按卡动态解析（PEP 562）。
# 语义与基线一致：external_identities 已有 token 且无 refresh_token（PAT/导入）
# 直接返回不预刷新（#343 R2 / T25-01）；其余委托 get_user_token_for。
# 新增平台若要保留自己的遗留符号，只需在卡里加 `legacy_token_symbol`，零代码改动。
# ---------------------------------------------------------------------------
def _legacy_token_resolver(symbol: str, provider: str):
    def _resolve(username: str, user_id: int = 0) -> str:
        token = _get_oauth_token_from_identity(username, provider)
        _uid = str(user_id or "").strip()
        if token and _uid not in ("", "0"):
            from src.domains.mgmt.auth.grape_account_db import _has_refresh_token
            if not _has_refresh_token(user_id, provider):
                return token
        return get_user_token_for(username, user_id=user_id, provider=provider)
    _resolve.__name__ = symbol
    _resolve.__qualname__ = symbol
    _resolve.__doc__ = (f"兼容别名（平台卡 legacy_token_symbol 声明）：委托 "
                        f"get_user_token_for(provider={provider!r})，保留基线 R2 早退语义。")
    return _resolve


def _legacy_token_symbols() -> "dict[str, str]":
    """符号名 → provider（由平台卡派生；卡不可用时返回空表，不静默造假符号）。"""
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms, get_platform_card
        out = {}
        for _p in list_vcs_platforms():
            _card = get_platform_card(_p)
            _sym = getattr(_card, "legacy_token_symbol", "") if _card else ""
            if _sym:
                out[str(_sym)] = _p
        return out
    except Exception as e:  # noqa: BLE001 - 卡不可用是配置态，不影响其余取 token 入口
        log.warning(f"legacy token symbols unavailable: {e}")
        return {}


def __getattr__(name: str):  # PEP 562：模块级动态属性
    _sym = _legacy_token_symbols().get(name)
    if _sym:
        _fn = _legacy_token_resolver(name, _sym)
        globals()[name] = _fn
        return _fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
