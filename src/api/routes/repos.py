"""仓库路由：列表、加入、托管、移除、同步"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook
from src.api.deps import get_current_user, get_bot_token, get_user_token
from src.user.db import update_managed_repo_bot_token, get_bot_token_by_repo

router = APIRouter(prefix="/api/repos", tags=["repos"])


class RepoAction(BaseModel):
    full_name: str


def _fetch_and_cache(user_id: int, api_token: str) -> list[dict]:
    """从 GitCode 拉取仓库列表，写入 user_repos 缓存，返回过滤后的结果"""
    repos = fetch_repos_with_role(api_token)
    user_db.save_user_repos(user_id, repos)
    return repos


def _filter_repos(user_id: int, repos: list[dict]) -> list[dict]:
    """过滤仓库列表：只返回可托管（Admin/Maintainer）或已托管的仓库"""
    managed = {r["full_name"]: r["is_admin"] for r in user_db.get_managed_repos(user_id)}
    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        is_admin = r.get("is_admin", False)
        in_managed = full_name in managed

        if not is_admin and not in_managed:
            continue

        result.append({
            "full_name": full_name,
            "owner": r.get("owner", ""),
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "role": "Admin" if is_admin else "Maintainer" if in_managed else "Member",
            "is_joined": in_managed,
            "is_hosted": in_managed and managed.get(full_name, False),
        })
    return result


@router.get("/member")
async def list_member_repos(user: dict = Depends(get_current_user)):
    """获取用户可操作仓库列表：优先从 user_repos 缓存读取，缓存为空时自动从 GitCode 拉取"""
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    # 优先读取 DB 缓存
    cached = user_db.get_user_repos(user["id"])
    if cached:
        return {"repos": _filter_repos(user["id"], cached), "_cached": True}

    # 缓存为空 → 从 GitCode 拉取
    try:
        repos = _fetch_and_cache(user["id"], api_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"加载仓库列表失败: {e}"})

    return {"repos": _filter_repos(user["id"], repos), "_cached": False}


@router.post("/sync")
async def sync_repos(user: dict = Depends(get_current_user)):
    """手动从 GitCode 同步仓库列表，刷新 user_repos 缓存"""
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    try:
        repos = _fetch_and_cache(user["id"], api_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"同步失败: {e}"})

    return {"repos": _filter_repos(user["id"], repos), "message": "同步成功"}


@router.post("/join")
async def join_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=0, is_admin=False)
    return {"message": "已加入仓库"}


@router.post("/host")
async def host_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    api_token = get_user_token(user["username"])
    acc = user_db.get_user_account(user["username"]) or {}
    webhook_url = acc.get("webhook_url", "")

    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    # 首次托管使用用户个人 Token 创建 webhook，Bot Token 可在 Repo 设置中后续配置
    bot_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
    webhook_token = bot_token or api_token
    if not webhook_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置，无法创建 webhook"})

    from src.user.repo_manager import _check_collaborator_admin, _req
    try:
        user_data = _req("GET", f"https://api.gitcode.com/api/v5/user", api_token)
        current_user = user_data.get("login", "")
        if not current_user:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})

        if current_user == owner:
            is_admin = True
        else:
            is_admin = _check_collaborator_admin(api_token, owner, repo_name, current_user)

        if not is_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "托管权限不够：仅仓库的维护员或管理员可以托管"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"权限验证失败: {e}"})

    try:
        existing_id = find_existing_webhook(webhook_token, owner, repo_name, webhook_url)
        if existing_id:
            user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=existing_id, is_admin=True)
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id}

        hook_id = setup_webhook(webhook_token, owner, repo_name, webhook_url)
        user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=hook_id, is_admin=True)
        return {"message": "仓库托管成功", "webhook_id": hook_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: {e}"})


@router.get("/managed")
async def list_managed_repos(user: dict = Depends(get_current_user)):
    repos = user_db.get_managed_repos(user["id"])
    return {"repos": [{
        "full_name": r["full_name"],
        "role": "Admin" if r["is_admin"] else "Member",
        "webhook_ok": bool(r["webhook_id"]),
        "branch": "main",
        "status": "Active" if r["is_admin"] else "Joined",
        "has_bot_token": bool(r.get("bot_gitcode_token", "")),
    } for r in repos]}


class BotTokenBody(BaseModel):
    full_name: str
    bot_token: str = ""


@router.get("/bot-token/{owner}/{repo_name}")
async def get_repo_bot_token(owner: str, repo_name: str, user: dict = Depends(get_current_user)):
    """获取仓库绑定的 DevOps Bot Token 是否存在"""
    token = get_bot_token_by_repo(owner, repo_name)
    return {"has_bot_token": bool(token)}


@router.put("/bot-token")
async def set_repo_bot_token(body: BotTokenBody, user: dict = Depends(get_current_user)):
    """设置仓库绑定的 DevOps Bot Token"""
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    ok = update_managed_repo_bot_token(user["id"], owner, repo_name, body.bot_token)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    return {"message": "Bot Token 已更新"}


@router.post("/remove")
async def remove_managed_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
    if not api_token:
        api_token = get_user_token(user["username"])

    if api_token:
        try:
            from src.user.repo_manager import _get_managed_webhook, remove_webhook
            wh_id = _get_managed_webhook(user["id"], owner, repo_name)
            if wh_id:
                remove_webhook(api_token, owner, repo_name, wh_id)
        except Exception:
            pass

    removed = user_db.remove_managed_repo(user["id"], owner, repo_name)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    return {"message": "已移除仓库"}
