"""共享依赖：Token 验证、Token 三层体系、用户信息获取"""
import threading
from fastapi import Header, HTTPException, Depends
from src.user import db as user_db

TOKEN_TYPE_DEVOPS = "devops_bot"
TOKEN_TYPE_USER = "user"
TOKEN_TYPE_WEBHOOK = "webhook"

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
    任何实例都能验证（集群会话共享）。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录"})
    token = authorization.replace("Bearer ", "").strip()
    # JWT access token 验证——签名+过期
    from src.api.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话过期"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    # token_version 校验——改密码/logout 后 +1，旧 token 失效
    db_ver = user_db.get_token_version(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话已失效"})
    # 记录活跃状态——O(1)，不阻塞
    mark_user_active(uid)
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_USER}


def get_user_token(username: str) -> str:
    """获取用户的个人 GitCode API Token（Chat Agent 使用）"""
    acc = user_db.get_user_account(username) or {}
    return acc.get("gitcode_api_token", "")


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
    """获取用户 GitCode token，过期时主动预刷新（Issue #52）。

    与 get_user_token 的区别：当 user_id > 0 时，检查 token 年龄，
    超过 100 分钟（GitCode token 默认 2 小时，留 20 分钟余量）则预刷新。
    401 时的被动刷新由 _gitcode_api_with_refresh 包装器兜底。
    """
    import time as _time
    acc = user_db.get_user_account(username) or {}
    token = acc.get("gitcode_api_token", "")

    if token and user_id > 0:
        # 检查 token 年龄——临近过期则预刷新
        cred = user_db.get_credential(user_id, "gitcode_oauth")
        if cred:
            fetched_at = cred.get("fetched_at", 0)
            age = int(_time.time()) - fetched_at if fetched_at else 0
            # 超过 100 分钟（6000 秒）预刷新
            if age > 6000:
                new_token = try_refresh_gitcode_token(user_id)
                return new_token or token
        return token

    if user_id > 0 and not token:
        return try_refresh_gitcode_token(user_id) or ""

    return token


def get_bot_token(repo_owner: str = "", repo_name: str = "") -> str:
    """获取仓库绑定的 DevOps Bot Token（DevOps Agents 使用）"""
    if repo_owner and repo_name:
        from src.user.db import get_bot_token_by_repo
        return get_bot_token_by_repo(repo_owner, repo_name)
    return ""


def require_devops_token(user: dict = Depends(get_current_user)):
    """确保该端点只能由拥有 Bot Token 配置的管理员访问"""
    # 检查用户是否有任一托管仓库配置了 Bot Token
    repos = user_db.get_managed_repos(user["id"])
    has_bot = any(r.get("bot_gitcode_token", "") for r in repos)
    if not has_bot:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "DevOps Bot Token 未配置，请在 GitCode Repo 页面中配置"})
    return user
