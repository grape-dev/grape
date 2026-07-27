"""共享依赖：Token 验证、Token 三层体系、用户信息获取"""
from fastapi import Header, HTTPException, Depends
from src.user import db as user_db

TOKEN_TYPE_DEVOPS = "devops_bot"
TOKEN_TYPE_USER = "user"
TOKEN_TYPE_WEBHOOK = "webhook"


def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization: Bearer <token> 提取并验证用户"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录"})
    token = authorization.replace("Bearer ", "").strip()
    uid = user_db.validate_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话过期"})
    user = user_db.get_user(uid)
    if not user:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    return {"id": uid, "username": user["username"], "token": token, "token_type": TOKEN_TYPE_USER}


def get_bot_token(username: str) -> str:
    """获取 DevOps Bot Token：优先使用用户配置的 Bot Token，回退到系统默认（default 账户）"""
    acc = user_db.get_user_account(username) or {}
    bot_token = acc.get("gitcode_api_token", "")
    if bot_token:
        return bot_token
    default_acc = user_db.get_user_account("default") or {}
    return default_acc.get("gitcode_api_token", "")


def get_webhook_token(owner: str, repo: str) -> str:
    """获取仓库的 Webhook Token，回退到 default"""
    acc = user_db.get_user_account(f"{owner}/{repo}") or {}
    wh_token = acc.get("gitcode_webhook_token", "")
    if wh_token:
        return wh_token
    default_acc = user_db.get_user_account("default") or {}
    return default_acc.get("gitcode_webhook_token", "")


def require_devops_token(user: dict = Depends(get_current_user)):
    """确保该端点只能由拥有 Bot Token 配置的管理员访问"""
    bot_token = get_bot_token("default")
    if not bot_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "DevOps Bot Token 未配置，请管理员在 GitCode Repo 页面中配置"})
    return user
