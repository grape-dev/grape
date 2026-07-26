"""仓库路由：列表、加入、托管、同步"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.v2.api.deps import get_current_user

router = APIRouter(prefix="/api/v2/repos", tags=["repos"])


class RepoAction(BaseModel):
    full_name: str  # "owner/repo"


@router.get("/member")
async def list_member_repos(user: dict = Depends(get_current_user)):
    """获取用户有权限的 GitCode 仓库列表（含角色、状态）"""
    acc = user_db.get_user_account(user["username"]) or {}
    api_token = acc.get("gitcode_api_token", "")
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    managed = {r["full_name"]: r["is_admin"] for r in user_db.get_managed_repos(user["id"])}

    from src.user.repo_manager import fetch_repos_with_role
    try:
        repos = fetch_repos_with_role(api_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"加载仓库列表失败: {e}"})

    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        result.append({
            "full_name": full_name,
            "owner": r.get("owner", ""),
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "role": "Admin" if r.get("is_admin") else "Maintainer" if full_name in managed else "Member",
            "is_joined": full_name in managed,
            "is_hosted": full_name in managed and managed.get(full_name, False),
        })
    return {"repos": result}


@router.post("/join")
async def join_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    """加入仓库（成员角色，不做托管）"""
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})

    owner, repo_name = parts
    user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=0, is_admin=False)
    return {"message": "已加入仓库"}


@router.post("/host")
async def host_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    """托管仓库（创建 webhook）"""
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})

    owner, repo_name = parts
    acc = user_db.get_user_account(user["username"]) or {}
    api_token = acc.get("gitcode_api_token", "")
    webhook_url = acc.get("webhook_url", "")

    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    from src.user.repo_manager import setup_webhook, find_existing_webhook
    from src.config import GITCODE_WEBHOOK_TOKEN
    try:
        existing_id = find_existing_webhook(api_token, owner, repo_name, webhook_url)
        if existing_id:
            user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=existing_id, is_admin=True)
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id}

        hook_id = setup_webhook(api_token, owner, repo_name, webhook_url)
        user_db.add_managed_repo(user["id"], owner, repo_name, body.full_name, webhook_id=hook_id, is_admin=True)
        return {"message": "仓库托管成功", "webhook_id": hook_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: {e}"})


@router.get("/managed")
async def list_managed_repos(user: dict = Depends(get_current_user)):
    """获取已托管/已加入的仓库列表"""
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
    removed = user_db.remove_managed_repo(user["id"], parts[0], parts[1])
    if not removed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    return {"message": "已移除仓库"}
