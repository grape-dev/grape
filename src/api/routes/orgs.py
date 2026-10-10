"""组织管理路由（/api/orgs）。

提供组织 CRUD、成员管理等 API。
认证：require_no_session_token（JWT/API Key，tk_ → 403）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import require_no_session_token, require_org_admin
from src.user.org_db import (
    create_org, get_org, list_user_orgs, update_org, delete_org,
    get_org_members, add_org_member, update_member_role, remove_org_member,
    get_member_role, is_org_member,
)
from src.user import db as user_db

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class CreateOrgBody(BaseModel):
    name: str
    slug: str = ""
    description: str = ""


class UpdateOrgBody(BaseModel):
    name: str = ""
    description: str = ""
    avatar_url: str = ""
    max_members: int = 0
    max_workers: int = 0


class AddMemberBody(BaseModel):
    username: str = ""
    user_id: int = 0
    role: str = "member"


class UpdateMemberBody(BaseModel):
    role: str


def _ensure_slug(slug: str, name: str, user_id: int) -> str:
    """生成唯一 slug。"""
    if not slug:
        slug = name.lower().replace(" ", "-").replace("/", "-")[:32]
    if not slug:
        slug = f"org-{user_id}"
    return slug


@router.get("", operation_id="orgs_list", summary="列出当前用户加入的组织")
def list_orgs(user: dict = Depends(require_no_session_token)):
    orgs = list_user_orgs(user["id"])
    return [
        {
            "id": o["id"],
            "name": o["name"],
            "slug": o["slug"],
            "description": o.get("description", ""),
            "avatar_url": o.get("avatar_url", ""),
            "is_personal": bool(o.get("is_personal")),
            "role": o.get("my_role", "member"),
            "member_count": o.get("member_count", 0),
            "created_at": o.get("created_at", ""),
        }
        for o in orgs
    ]


@router.post("", operation_id="orgs_create", summary="创建组织")
def create_organization(body: CreateOrgBody, user: dict = Depends(require_no_session_token)):
    slug = _ensure_slug(body.slug, body.name, user["id"])
    org = create_org(name=body.name, slug=slug, owner_id=user["id"],
                     description=body.description)
    if not org:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "slug 已存在或创建失败"})
    return {
        "id": org["id"],
        "name": org["name"],
        "slug": org["slug"],
        "description": org.get("description", ""),
        "is_personal": bool(org.get("is_personal")),
        "role": "owner",
        "member_count": 1,
        "created_at": org.get("created_at", ""),
    }


@router.get("/{org_id}", operation_id="orgs_detail", summary="获取组织详情")
def get_organization(org_id: int, user: dict = Depends(require_no_session_token)):
    if not is_org_member(user["id"], org_id):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})
    org = get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "组织不存在"})
    role = get_member_role(user["id"], org_id)
    return {
        "id": org["id"],
        "name": org["name"],
        "slug": org["slug"],
        "description": org.get("description", ""),
        "avatar_url": org.get("avatar_url", ""),
        "is_personal": bool(org.get("is_personal")),
        "owner_id": org.get("owner_id"),
        "role": role,
        "settings": org.get("settings", "{}"),
        "max_members": org.get("max_members", 50),
        "max_workers": org.get("max_workers", 10),
        "created_at": org.get("created_at", ""),
    }


@router.put("/{org_id}", operation_id="orgs_update", summary="更新组织信息")
def update_organization(org_id: int, body: UpdateOrgBody,
                        user: dict = Depends(require_org_admin)):
    update_org(org_id, name=body.name, description=body.description, avatar_url=body.avatar_url,
               max_members=body.max_members, max_workers=body.max_workers)
    return {"ok": True}


@router.delete("/{org_id}", operation_id="orgs_delete", summary="删除组织（个人组织不可删）")
def delete_organization(org_id: int, user: dict = Depends(require_org_admin)):
    if user.get("org_role") != "owner":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅 owner 可删除组织"})
    ok = delete_org(org_id)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "组织不存在或为个人组织不可删除"})
    return {"ok": True}


@router.get("/search/users", operation_id="orgs_search_users", summary="搜索用户（添加成员用）")
def search_users(q: str = "", user: dict = Depends(require_no_session_token)):
    """按用户名模糊搜索用户（用于添加成员时的实时搜索）。"""
    from src.session_manager import db as _db, db_lock as _dbl
    if not q or len(q) < 1:
        return []
    with _dbl:
        _c = _db.cursor()
        _c.execute(
            "SELECT id, username, nickname FROM user_accounts "
            "WHERE username LIKE ? OR nickname LIKE ? "
            "ORDER BY username LIMIT 20",
            (f"%{q}%", f"%{q}%")
        )
        rows = _c.fetchall()
    return [{"id": r[0], "username": r[1], "nickname": r[2] or ""} for r in rows]


@router.get("/{org_id}/members", operation_id="orgs_members_list", summary="列出组织成员")
def list_members(org_id: int, user: dict = Depends(require_no_session_token)):
    if not is_org_member(user["id"], org_id):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不是该组织的成员"})
    members = get_org_members(org_id)
    return [
        {
            "user_id": m["user_id"],
            "username": m.get("username", ""),
            "nickname": m.get("nickname", ""),
            "role": m["role"],
            "joined_at": m.get("joined_at", ""),
        }
        for m in members
    ]


@router.post("/{org_id}/members", operation_id="orgs_members_add", summary="添加成员")
def add_member(org_id: int, body: AddMemberBody, user: dict = Depends(require_org_admin)):
    # 支持通过 username 或 user_id 添加
    target_uid = body.user_id
    if not target_uid and body.username:
        from src.user.db import get_user_by_username
        target = get_user_by_username(body.username.strip())
        if not target:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"用户 '{body.username}' 不存在"})
        target_uid = target["id"]
    if not target_uid:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请提供 username 或 user_id"})
    # 检查目标用户是否存在
    from src.session_manager import db as _db, db_lock as _dbl
    with _dbl:
        _c = _db.cursor()
        _c.execute("SELECT id, username FROM user_accounts WHERE id=?", (target_uid,))
        _u = _c.fetchone()
    if not _u:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "用户不存在"})
    ok = add_org_member(org_id, target_uid, body.role, invited_by=user["id"])
    if not ok:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "用户已是组织成员"})
    return {"ok": True}


@router.patch("/{org_id}/members/{user_id}", operation_id="orgs_members_update", summary="修改成员角色")
def update_member(org_id: int, user_id: int, body: UpdateMemberBody,
                  user: dict = Depends(require_org_admin)):
    if user.get("org_role") != "owner":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅 owner 可修改成员角色"})
    ok = update_member_role(org_id, user_id, body.role)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "成员不存在"})
    return {"ok": True}


@router.delete("/{org_id}/members/{user_id}", operation_id="orgs_members_remove", summary="移除成员")
def remove_member(org_id: int, user_id: int, user: dict = Depends(require_org_admin)):
    ok = remove_org_member(org_id, user_id)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "成员不存在或为 owner 不可移除"})
    return {"ok": True}


@router.post("/{org_id}/leave", operation_id="orgs_leave", summary="离开组织")
def leave_org(org_id: int, user: dict = Depends(require_no_session_token)):
    role = get_member_role(user["id"], org_id)
    if not role:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "不是该组织的成员"})
    if role == "owner":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "owner 不可离开组织，请先转让所有权"})
    ok = remove_org_member(org_id, user["id"])
    return {"ok": ok}
