"""仓库路由：列表、加入、托管、移除"""
import time
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook
from src.api.deps import get_current_user, get_bot_token
from src.config import GITCODE_WEBHOOK_TOKEN

router = APIRouter(prefix="/api/repos", tags=["repos"])

# 仓库列表缓存（userId → {data, timestamp}），TTL 30s
_repo_cache: dict[int, dict] = {}
_REPO_CACHE_TTL = 30


def _get_cached_repos(user_id: int):
    entry = _repo_cache.get(user_id)
    if entry and time.time() - entry["time"] < _REPO_CACHE_TTL:
        return entry["data"]
    return None


def _set_cached_repos(user_id: int, data: list):
    _repo_cache[user_id] = {"data": data, "time": time.time()}


def _clear_repo_cache(user_id: int):
    _repo_cache.pop(user_id, None)


class RepoAction(BaseModel):
    full_name: str


@router.get("/member")
async def list_member_repos(user: dict = Depends(get_current_user)):
    # 检查缓存
    cached = _get_cached_repos(user["id"])
    if cached is not None:
        return {"repos": cached}

    acc = user_db.get_user_account(user["username"]) or {}
    api_token = acc.get("gitcode_api_token", "")
    if not api_token:
        api_token = get_bot_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    managed = {r["full_name"]: r["is_admin"] for r in user_db.get_managed_repos(user["id"])}

    try:
        repos = fetch_repos_with_role(api_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"加载仓库列表失败: {e}"})

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

    _set_cached_repos(user["id"], result)
    return {"repos": result}


@router.post("/join")
async def join_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=0, is_admin=False)
    _clear_repo_cache(user["id"])
    return {"message": "已加入仓库"}


@router.post("/host")
async def host_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    acc = user_db.get_user_account(user["username"]) or {}
    api_token = acc.get("gitcode_api_token", "")
    if not api_token:
        api_token = get_bot_token(user["username"])
    webhook_url = acc.get("webhook_url", "")

    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    # Webhook 操作使用 DevOps Bot Token（有 manage_webhook 权限），而非用户个人 token
    bot_token = get_bot_token("default")
    if not bot_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "DevOps Bot Token 未配置，请在 GitCode Repo 设置中配置系统默认 Token"})

    # 权限检查：必须是仓库的维护员或管理员才能托管
    from src.user.repo_manager import _check_collaborator_admin, _req
    try:
        user_data = _req("GET", f"https://api.gitcode.com/api/v5/user", api_token)
        current_user = user_data.get("login", "")
        if not current_user:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})

        # 项目所有者有完全权限，直接通过
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
        existing_id = find_existing_webhook(bot_token, owner, repo_name, webhook_url)
        if existing_id:
            user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=existing_id, is_admin=True)
            _clear_repo_cache(user["id"])
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id}

        hook_id = setup_webhook(bot_token, owner, repo_name, webhook_url)
        user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=hook_id, is_admin=True)
        _clear_repo_cache(user["id"])
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
    } for r in repos]}


@router.post("/remove")
async def remove_managed_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # 如果存在 webhook，先从 GitCode 删除
    acc = user_db.get_user_account(user["username"]) or {}
    api_token = acc.get("gitcode_api_token", "")
    if not api_token:
        api_token = get_bot_token(user["username"])

    if api_token:
        try:
            from src.user.repo_manager import _get_managed_webhook, remove_webhook
            wh_id = _get_managed_webhook(user["id"], owner, repo_name)
            if wh_id:
                remove_webhook(api_token, owner, repo_name, wh_id)
        except Exception as e:
            pass  # Webhook 删除失败不影响解除托管

    removed = user_db.remove_managed_repo(user["id"], owner, repo_name)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    _clear_repo_cache(user["id"])
    return {"message": "已移除仓库"}
