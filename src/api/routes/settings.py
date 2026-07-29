"""Settings 路由：凭据、通知偏好、GitCode Repo、Backend Agents"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user, get_webhook_token, get_user_token

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ====== Credentials ======

@router.get("/credentials")
async def get_credentials(user: dict = Depends(get_current_user)):
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
        "codearts": {
            "ak_exists": bool(acc.get("CODEARTS_CLI_AK")),
            "sk_exists": bool(acc.get("CODEARTS_CLI_SK")),
        },
    }


class UpdateCredentials(BaseModel):
    huawei_cloud_ak: str = ""
    huawei_cloud_sk: str = ""
    codearts_ak: str = ""
    codearts_sk: str = ""


@router.put("/credentials")
async def update_credentials(body: UpdateCredentials, user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account(user["username"]) or {}
    if body.huawei_cloud_ak:
        acc["huawei_cloud_ak"] = body.huawei_cloud_ak
    if body.huawei_cloud_sk:
        acc["huawei_cloud_sk"] = body.huawei_cloud_sk
    if body.codearts_ak:
        acc["CODEARTS_CLI_AK"] = body.codearts_ak
    if body.codearts_sk:
        acc["CODEARTS_CLI_SK"] = body.codearts_sk
    user_db.save_user_account(user["username"], acc)
    return {"message": "凭据已更新"}


# ====== Notifications ======

@router.get("/notifications")
async def get_notifications(user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT auto_impl_enabled, notify_enabled FROM feishu_bindings LIMIT 1")
            row = c.fetchone()
            auto_impl = bool(row[0]) if row else True
            notify = bool(row[1]) if row else True
        except Exception:
            auto_impl, notify = True, True
    return {
        "issues": True,
        "pr": True,
        "build": False,
        "release": True,
        "vod": auto_impl,
        "notifications_enabled": notify,
    }


class NotificationPrefs(BaseModel):
    issues: bool = True
    pr: bool = True
    build: bool = False
    release: bool = True
    vod: bool = False
    notifications_enabled: bool = True


@router.put("/notifications")
async def update_notifications(body: NotificationPrefs, user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("UPDATE feishu_bindings SET auto_impl_enabled=?, notify_enabled=?",
                      (1 if body.vod else 0, 1 if body.notifications_enabled else 0))
            db.commit()
        except Exception:
            pass
    return {"message": "通知偏好已更新", "prefs": body.model_dump()}


# ====== GitCode Repo ======

@router.get("/gitcode-repo/{owner}/{repo}")
async def get_gitcode_repo_config(owner: str, repo: str, user: dict = Depends(get_current_user)):
    repo_key = f"{owner}/{repo}"
    acc = user_db.get_user_account(repo_key) or {}
    # 读取 repo 绑定的 Bot Token
    from src.user.db import get_bot_token_by_repo
    bot_token = get_bot_token_by_repo(owner, repo)
    wh_token = acc.get("gitcode_webhook_token", "")

    return {
        "devops_bot_token_exists": bool(bot_token),
        "webhook_token_exists": bool(wh_token),
        "webhook_url": acc.get("webhook_url", "http://localhost:8080/webhook/gitcode"),
        "bot_username": acc.get("bot_usernames", "devops-bot"),
    }


class GitCodeRepoConfig(BaseModel):
    bot_token: str = ""
    webhook_token: str = ""
    bot_username: str = "devops-bot"


@router.put("/gitcode-repo/{owner}/{repo}")
async def update_gitcode_repo_config(owner: str, repo: str, body: GitCodeRepoConfig,
                                     user: dict = Depends(get_current_user)):
    repo_key = f"{owner}/{repo}"
    acc = user_db.get_user_account(repo_key) or {}
    if body.webhook_token:
        acc["gitcode_webhook_token"] = body.webhook_token
    if body.bot_username:
        acc["bot_usernames"] = body.bot_username
    user_db.save_user_account(repo_key, acc)
    # 将 Bot Token 存储到 managed_repos
    if body.bot_token:
        from src.user.db import update_managed_repo_bot_token
        # 找到该 repo 在 managed_repos 中的记录
        repos = user_db.get_managed_repos(user["id"])
        for r in repos:
            if r["repo_owner"] == owner and r["repo_name"] == repo:
                update_managed_repo_bot_token(user["id"], owner, repo, body.bot_token)
                break
    return {"message": "GitCode Repo 配置已更新"}


# ====== OAuth 凭据（管理员配置）======

@router.get("/oauth")
async def get_oauth_config(user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account("default") or {}
    return {
        "gitcode_oauth_client_id": bool(acc.get("gitcode_oauth_client_id", "")),
        "gitcode_oauth_redirect_uri": acc.get("gitcode_oauth_redirect_uri", ""),
    }


class OAuthConfig(BaseModel):
    gitcode_oauth_client_id: str = ""
    gitcode_oauth_client_secret: str = ""
    gitcode_oauth_redirect_uri: str = ""


@router.put("/oauth")
async def update_oauth_config(body: OAuthConfig, user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account("default") or {}
    if body.gitcode_oauth_client_id:
        acc["gitcode_oauth_client_id"] = body.gitcode_oauth_client_id
    if body.gitcode_oauth_client_secret:
        acc["gitcode_oauth_client_secret"] = body.gitcode_oauth_client_secret
    if body.gitcode_oauth_redirect_uri:
        acc["gitcode_oauth_redirect_uri"] = body.gitcode_oauth_redirect_uri
    user_db.save_user_account("default", acc)
    # Re-initialize config accounts
    from src.config import _load_accounts
    _load_accounts()
    return {"message": "OAuth 配置已更新"}


# ====== Backend Agents ======

@router.get("/backend-agents")
async def get_backend_agents(user: dict = Depends(get_current_user)):
    """从 DB 读取后端引擎列表，空列表则需手动刷新"""
    from src.user import db as user_db
    cached = user_db.get_backend_agents()
    return {"backends": cached}

@router.post("/backend-agents/refresh")
async def refresh_backend_agents(user: dict = Depends(get_current_user)):
    """手动刷新：发现本地引擎 + 子进程检测 + 写入 DB"""
    from src.agent.executor import discover_backends
    from src.user import db as user_db
    import subprocess

    discovered = discover_backends()
    results = []
    for eng in discovered:
        version = None
        healthy = None
        cli_path = eng["cli_path"]

        if cli_path:
            try:
                r = subprocess.run([cli_path, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    version = r.stdout.strip().split("\n")[0][:60]
                    healthy = True
                else:
                    healthy = False
            except Exception as e:
                version = str(e)[:40]
                healthy = False
        else:
            healthy = False

        results.append({
            "name": eng["name"],
            "display_name": eng["name"].capitalize(),
            "installed": eng["installed"],
            "cli_path": cli_path or "",
            "healthy": healthy,
            "version": version or "",
            "pip_package": eng.get("pip_package", ""),
        })

    # Only persist backends that are actually installed
    results = [r for r in results if r["installed"]]
    user_db.save_backend_agents(results)
    return {"backends": results}
