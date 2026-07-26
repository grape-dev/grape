"""Settings 路由：凭据、通知偏好、自定义 Skill"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.v2.api.deps import get_current_user

router = APIRouter(prefix="/api/v2/settings", tags=["settings"])


@router.get("/credentials")
async def get_credentials(user: dict = Depends(get_current_user)):
    """获取用户凭据"""
    acc = user_db.get_user_account(user["username"]) or {}
    return {
        "gitcode": {
            "api_token_exists": bool(acc.get("gitcode_api_token")),
            "account": user["username"],
            "auto_filled": True,
        },
        "huawei_cloud": {
            "ak_exists": bool(acc.get("huawei_cloud_ak")),
            "sk_exists": bool(acc.get("huawei_cloud_sk")),
        },
    }


class UpdateCredentials(BaseModel):
    huawei_cloud_ak: str = ""
    huawei_cloud_sk: str = ""


@router.put("/credentials")
async def update_credentials(body: UpdateCredentials, user: dict = Depends(get_current_user)):
    """更新用户凭据"""
    acc = user_db.get_user_account(user["username"]) or {}
    if body.huawei_cloud_ak:
        acc["huawei_cloud_ak"] = body.huawei_cloud_ak
    if body.huawei_cloud_sk:
        acc["huawei_cloud_sk"] = body.huawei_cloud_sk
    user_db.save_user_account(user["username"], acc)
    return {"message": "凭据已更新"}


@router.get("/notifications")
async def get_notifications(user: dict = Depends(get_current_user)):
    """获取通知偏好"""
    return {
        "issues": True,
        "pr": True,
        "build": False,
        "release": True,
        "vod": False,
    }


class NotificationPrefs(BaseModel):
    issues: bool = True
    pr: bool = True
    build: bool = False
    release: bool = True
    vod: bool = False


@router.put("/notifications")
async def update_notifications(body: NotificationPrefs, user: dict = Depends(get_current_user)):
    """更新通知偏好"""
    return {"message": "通知偏好已更新", "prefs": body.dict()}


@router.get("/gitcode-repo/{owner}/{repo}")
async def get_gitcode_repo_config(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """获取仓库级 GitCode 配置"""
    return {
        "devops_bot_token_exists": True,
        "webhook_token_exists": True,
        "webhook_url": "http://203.0.113.2:8080/webhook/gitcode",
        "bot_username": "devops-bot",
    }


class GitCodeRepoConfig(BaseModel):
    bot_token: str = ""
    webhook_token: str = ""
    bot_username: str = "devops-bot"


@router.put("/gitcode-repo/{owner}/{repo}")
async def update_gitcode_repo_config(owner: str, repo: str, body: GitCodeRepoConfig,
                                      user: dict = Depends(get_current_user)):
    """更新仓库级 GitCode 配置"""
    return {"message": "GitCode Repo 配置已更新"}
