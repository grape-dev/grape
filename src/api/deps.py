"""共享依赖：Token 验证、Token 三层体系、用户信息获取"""
from fastapi import Header, HTTPException, Depends
from src.user import db as user_db

TOKEN_TYPE_DEVOPS = "devops_bot"
TOKEN_TYPE_USER = "user"
TOKEN_TYPE_WEBHOOK = "webhook"


def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization: Bearer *** 提取并验证 JWT access token。

    JWT 无状态验证——不查 _user_tokens、不查数据库。
    任何实例都能验证（集群会话共享）。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录"})
    token = authorization.replace("Bearer ", "").strip()
    # JWT access token 验证——无状态，任何实例都能验证
    from src.api.jwt_utils import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话过期"})
    uid = int(payload.get("sub", 0))
    username = payload.get("username", "")
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    return {"id": uid, "username": username, "token": token, "token_type": TOKEN_TYPE_USER}


def get_user_token(username: str) -> str:
    """获取用户的个人 GitCode API Token（Chat Agent 使用）"""
    acc = user_db.get_user_account(username) or {}
    return acc.get("gitcode_api_token", "")


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
