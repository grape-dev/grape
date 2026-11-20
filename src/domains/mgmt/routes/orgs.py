"""组织管理路由（/api/orgs）。

提供组织 CRUD、成员管理等 API。
认证：require_no_session_token（JWT/API Key，tk_ → 403）。
"""
import copy
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field  # Field：#2423 M2b 移植

from src.api.deps import get_user_token_for, require_no_session_token, require_org_admin
from src.domains.mgmt.user.org_db import (
    create_org, get_org, list_user_orgs, update_org, delete_org,
    get_org_members, add_org_member, update_member_role, remove_org_member,
    claim_org_provision_retry, get_member_role, is_org_member,
    update_org_provision_fields,
)
from src.domains.mgmt.org_provision import (
    manual_bind_org_repo as manual_bind_org_repo_service,
    start_provision,
)
from src.domains.mgmt.user import db as user_db

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


def _error(status: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": status, "error_code": error_code, "message": message},
    )


class CreateOrgBody(BaseModel):
    name: str
    slug: str = ""
    description: str = ""
    # 组织仓库创建选项（团队组织创建后自动建仓；空值保持历史自动选择行为）
    repo_provider: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    repo_path: str = ""
    repo_private: bool = True


class UpdateOrgBody(BaseModel):
    name: str = ""
    description: str = ""
    avatar_url: str = ""
    max_members: int = 0
    max_workers: int = 0
    max_repos: int = 0


class AddMemberBody(BaseModel):
    username: str = ""
    user_id: str = ""
    role: str = "member"


class UpdateMemberBody(BaseModel):
    role: str


class ManualBindRepoBody(BaseModel):
    repo_key: str = ""
    confirm: bool = False


def _ensure_slug(slug: str, name: str, user_id: int) -> str:
    """生成唯一 slug。"""
    if not slug:
        slug = name.lower().replace(" ", "-").replace("/", "-")[:32]
    if not slug:
        slug = f"org-{user_id}"
    return slug


def _provision_payload(org: dict) -> dict:
    error = org.get("provision_error") or ""
    return {
        "org_id": org.get("id", 0),
        "org_repo_key": org.get("org_repo_key") or "",
        "org_scene_id": org.get("org_scene_id") or "",
        "provision_status": org.get("provision_status") or "none",
        "provision_error": error,
        "error": error,
        "provisioned_at": org.get("provisioned_at") or "",
        **_repo_create_payload(org),
    }


def _repo_create_payload(org: dict) -> dict:
    """组织仓库创建选项（平台/空间/名称/路径/可见性）对外字段。"""
    try:
        private = bool(int(org.get("org_repo_private") if org.get("org_repo_private") is not None else 1))
    except (TypeError, ValueError):
        private = True
    return {
        "org_repo_provider": org.get("org_repo_provider") or "",
        "org_repo_owner": org.get("org_repo_owner") or "",
        "org_repo_name": org.get("org_repo_name") or "",
        "org_repo_path": org.get("org_repo_path") or "",
        "org_repo_private": private,
    }


_REPO_SLUG_RE = re.compile(r"^[\w\-.]+$")


def _normalize_repo_create_options(body: CreateOrgBody, user: dict) -> dict:
    """校验并归一化组织仓库创建选项。

    空 provider = 沿用历史自动选择（登录平台/已绑定 token 优先），此时不允许
    单独指定空间/路径，避免出现「选了组织空间却不知道去哪个平台建」的歧义。
    """
    provider = (body.repo_provider or "").strip().lower()
    owner = (body.repo_owner or "").strip()
    name = (body.repo_name or "").strip()
    path = (body.repo_path or "").strip()

    from src.domains.mgmt.user.repo_manager import org_namespace_providers

    allowed = set(org_namespace_providers())
    if provider and provider not in allowed:
        raise _error(400, "INVALID_REPO_PROVIDER", "Organization repository provider must be GitCode or GitHub")
    if not provider and (owner or name or path):
        raise _error(400, "INVALID_REPO_PROVIDER", "Select the organization repository provider")
    if owner and not _REPO_SLUG_RE.match(owner):
        raise _error(400, "INVALID_REPO_OWNER", "Namespace may contain letters, digits, underscore, hyphen and dot only")
    for value, label in ((name, "仓库名称"), (path, "仓库路径")):
        if value and not _REPO_SLUG_RE.match(value):
            raise _error(400, "INVALID_REPO_NAME", f"{label} may contain letters, digits, underscore, hyphen and dot only")
    if name and len(name) > 100:
        raise _error(400, "INVALID_REPO_NAME", "Repository name is too long")
    if path and len(path) > 100:
        raise _error(400, "INVALID_REPO_PATH", "Repository path is too long")

    if provider:
        token = get_user_token_for(
            user.get("username", ""), user_id=str(user.get("id") or ""), provider=provider)
        if not token:
            raise _error(
                400, "VCS_TOKEN_MISSING",
                f"当前账号未绑定 {provider} 平台凭据，无法在该平台创建组织仓库",
            )
        if provider == "github":
            # 建组织前先探测建仓权限：没权限直接报错，不创建组织再异步失败
            # （2026-11-19 现场：细粒度 PAT 无建仓权限，组织建出来了但仓库没了）。
            from src.domains.mgmt.user.repo_manager import probe_github_repo_create

            can_create, reason = probe_github_repo_create(
                token, owner=owner, private=bool(body.repo_private))
            if can_create is False:
                raise _error(
                    400, "VCS_REPO_CREATE_FORBIDDEN",
                    f"无法在 GitHub 创建组织仓库：{reason}",
                )

    return {
        "org_repo_provider": provider,
        "org_repo_owner": owner,
        "org_repo_name": name,
        "org_repo_path": path,
        "org_repo_private": 1 if body.repo_private else 0,
    }


@router.get("", operation_id="orgs_list", summary="List organizations")
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
            "max_repos": o.get("max_repos", 20),
            "provision_status": o.get("provision_status") or "none",
            "provision_error": o.get("provision_error") or "",
            **_repo_create_payload(o),
            "created_at": o.get("created_at", ""),
        }
        for o in orgs
    ]


# ===== 创建组织弹窗的数据源：账号 + 可建仓空间 =====
# 每个 provider 需要两次外部 VCS 调用（verify_token 取登录名、组织空间列表），
# 串行时 gitcode+github 约 2.2~2.6s，全部变成进入创建弹窗的白屏等待。
# 优化：① provider 之间并行、同 provider 的两次调用也并行；② 60s 进程内缓存
# （key 含 token 指纹，换 token 自动失效），组织页/下拉打开时预热后再次打开是毫秒级。
_NS_CACHE_TTL = 60.0
_NS_CACHE_MAX = 256
_NS_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}
_NS_CACHE_LOCK = threading.Lock()


def _ns_cache_key(user: dict, provider: str, token: str) -> tuple[str, str, str]:
    return (
        str(user.get("id") or ""),
        provider,
        hashlib.sha1(token.encode("utf-8")).hexdigest()[:12],
    )


def _ns_cache_get(key: tuple[str, str, str]) -> dict | None:
    with _NS_CACHE_LOCK:
        hit = _NS_CACHE.get(key)
        if not hit:
            return None
        ts, entry = hit
        if time.time() - ts > _NS_CACHE_TTL:
            _NS_CACHE.pop(key, None)
            return None
        return copy.deepcopy(entry)


def _ns_cache_put(key: tuple[str, str, str], entry: dict) -> None:
    with _NS_CACHE_LOCK:
        if len(_NS_CACHE) >= _NS_CACHE_MAX:
            oldest = min(_NS_CACHE, key=lambda k: _NS_CACHE[k][0])
            _NS_CACHE.pop(oldest, None)
        _NS_CACHE[key] = (time.time(), copy.deepcopy(entry))


def _namespace_provider_entry(user: dict, provider: str) -> dict:
    """单个 provider 的账号/空间视图（并行外部调用 + 60s 缓存）。"""
    from src.domains.mgmt.user.repo_manager import fetch_org_namespaces

    token = get_user_token_for(
        user.get("username", ""), user_id=str(user.get("id") or ""), provider=provider)
    name = provider
    try:
        from src.domains.runtime import public as runtime_public

        card = runtime_public.get_platform_card(provider)
        if card is not None and getattr(card, "name", ""):
            name = card.name
    except Exception:
        pass

    entry = {
        "provider": provider,
        "name": name,
        "connected": bool(token),
        "account": {},
        "namespaces": [],
        "error": "",
    }
    if not token:
        return entry

    key = _ns_cache_key(user, provider, token)
    cached = _ns_cache_get(key)
    if cached is not None:
        return cached

    def _verify() -> dict:
        from src.kernel.token_resolver import create_provider

        return create_provider(provider).verify_token(token) or {}

    def _orgs() -> list:
        return fetch_org_namespaces(provider, token) or []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_info = pool.submit(_verify)
        f_orgs = pool.submit(_orgs)
        try:
            info = f_info.result() or {}
        except Exception as exc:
            entry["connected"] = False
            entry["error"] = str(exc)[:300]
            return entry
        try:
            orgs = f_orgs.result() or []
        except Exception as org_exc:
            # 组织列表失败不应禁用个人空间建仓
            orgs = []
            entry["org_error"] = str(org_exc)[:300]

    try:
        login = str(info.get("login") or info.get("username") or "")
        if not login:
            raise RuntimeError("平台未返回登录名")
        display = str(info.get("name") or info.get("nickname") or login)
        avatar = str(info.get("avatar_url") or info.get("avatar") or "")
        entry["account"] = {"login": login, "name": display, "avatar_url": avatar}
        entry["namespaces"].append({
            "login": login,
            "name": display,
            "avatar_url": avatar,
            "type": "user",
        })
        for org in orgs:
            org_login = str((org or {}).get("login") or "").strip()
            if not org_login or org_login == login:
                continue
            entry["namespaces"].append({
                "login": org_login,
                "name": str((org or {}).get("name") or org_login),
                "avatar_url": str((org or {}).get("avatar_url") or ""),
                "type": "org",
            })
    except Exception as exc:
        entry["connected"] = False
        entry["error"] = str(exc)[:300]
        return entry

    _ns_cache_put(key, entry)
    return entry


@router.get("/repo-namespaces", operation_id="orgs_repo_namespaces",
            summary="List repository namespaces")
def list_org_repo_namespaces(user: dict = Depends(require_no_session_token)):
    """创建组织弹窗的数据源：已绑定的 GitCode/GitHub 账号 + 可建仓的组织空间。

    每个 provider 返回 connected（是否已绑定 token）和 account/namespaces；
    token 失效仅影响该 provider（connected=false + error），不阻断另一平台。
    """
    from src.domains.mgmt.user.repo_manager import org_namespace_providers

    providers = org_namespace_providers()
    if not providers:
        return {"providers": []}
    with ThreadPoolExecutor(max_workers=min(4, len(providers))) as pool:
        entries = list(pool.map(lambda p: _namespace_provider_entry(user, p), providers))
    return {"providers": entries}


@router.post("", operation_id="orgs_create", summary="Create organization")
def create_organization(body: CreateOrgBody, user: dict = Depends(require_no_session_token)):
    repo_options = _normalize_repo_create_options(body, user)
    slug = _ensure_slug(body.slug, body.name, user["id"])
    org = create_org(name=body.name, slug=slug, owner_id=user["id"],
                     description=body.description)
    if not org:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "Slug already exists or creation failed"})
    update_org_provision_fields(
        org["id"], provision_status="pending", provision_error="", **repo_options)
    start_provision(org["id"])
    org = get_org(org["id"]) or org
    return {
        "id": org["id"],
        "name": org["name"],
        "slug": org["slug"],
        "description": org.get("description", ""),
        "is_personal": bool(org.get("is_personal")),
        "role": "owner",
        "member_count": 1,
        "max_repos": org.get("max_repos", 20),
        **_provision_payload(org),
        "created_at": org.get("created_at", ""),
    }


@router.get("/{org_id}", operation_id="orgs_detail", summary="Get organization detail")
def get_organization(org_id: str, user: dict = Depends(require_no_session_token)):
    if not is_org_member(user["id"], org_id):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Not a member of this organization"})
    org = get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Organization not found"})
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
        "max_repos": org.get("max_repos", 20),
        **_provision_payload(org),
        "created_at": org.get("created_at", ""),
    }


@router.get("/{org_id}/provision", operation_id="orgs_provision_get",
            summary="Get organization provisioning status")
def get_org_provision(org_id: str, user: dict = Depends(require_no_session_token)):
    org = get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail={
            "code": 404, "error_code": "ORG_NOT_FOUND", "message": "Organization not found",
        })
    if not get_member_role(user["id"], org_id):
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "PERMISSION_DENIED", "message": "Not a member of this organization",
        })
    return _provision_payload(org)


@router.post("/{org_id}/provision/retry", operation_id="orgs_provision_retry",
             summary="Retry organization provisioning")
def retry_org_provision(org_id: str, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.user.org_db import is_org_personal

    org = get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail={
            "code": 404, "error_code": "ORG_NOT_FOUND", "message": "Organization not found",
        })
    role = get_member_role(user["id"], org_id)
    if not role:
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "PERMISSION_DENIED", "message": "Not a member of this organization",
        })
    if role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "PERMISSION_DENIED", "message": "Organization admin permission required",
        })
    if is_org_personal(org_id):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "INVALID_ORG_CONTEXT",
            "message": "Personal organizations do not support provisioning",
        })
    status = org.get("provision_status") or "none"
    if status == "ready":
        return _provision_payload(org)
    if status in ("none", "failed"):
        claim_org_provision_retry(org_id)
    start_provision(org_id)
    org = get_org(org_id) or org
    return _provision_payload(org)


@router.put("/{org_id}/provision/repo", operation_id="orgs_provision_manual_bind",
            summary="Manually bind organization repository")
def manual_bind_org_repo(org_id: str, body: ManualBindRepoBody,
                         user: dict = Depends(require_no_session_token)):
    """Issue #2258：自动 provisioning 失败时，owner/admin 可将已托管仓库指定为组织仓库。"""
    if not get_org(org_id):
        raise _error(404, "ORG_NOT_FOUND", "Organization not found")
    role = get_member_role(user["id"], org_id)
    if role not in ("owner", "admin"):
        raise _error(403, "PERMISSION_DENIED", "Organization admin permission required")
    return manual_bind_org_repo_service(
        org_id, body.repo_key, actor_id=user["id"], confirm=bool(body.confirm),
    )


@router.put("/{org_id}", operation_id="orgs_update", summary="Update organization")
def update_organization(org_id: str, body: UpdateOrgBody,
                        user: dict = Depends(require_no_session_token)):
    # Issue #381/#395: 写接口鉴权绑定路径 org_id（Organization not found→404；非成员→403；非管理员→403），
    # 不再用 require_org_admin（resolve_org_context 回退个人组织的上下文型鉴权，与路径脱钩可越权）
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    # 个人组织（默认工作空间）不支持编辑成员上限/Worker 上限，忽略这两个字段
    from src.domains.mgmt.user.org_db import is_org_personal as _is_p
    if _is_p(org_id):
        body.max_members = 0
        body.max_workers = 0
        body.max_repos = 0
    ok = update_org(org_id, name=body.name, description=body.description, avatar_url=body.avatar_url,
                    max_members=body.max_members, max_workers=body.max_workers,
                    max_repos=body.max_repos)
    if not ok:
        # update_org 影响行数为 0（Organization not found）→ 不得报成功
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Organization not found"})
    return {"ok": True}


@router.delete("/{org_id}", operation_id="orgs_delete", summary="Delete organization")
def delete_organization(org_id: str, user: dict = Depends(require_no_session_token)):
    # Issue #381/#395: 删除接口鉴权同样绑定路径 org_id（404/403 语义与 PUT 一致）
    from src.api.deps import require_path_org_admin
    role = require_path_org_admin(user["id"], org_id)
    if role != "owner":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Only the owner can delete the organization"})
    # Issue #2261：删组织同时回收组织自动仓库（远端+本地），手工关联仓库只退回未归属，
    # 避免组织删掉后仓库/场景/托管记录变成孤儿（此前会残留在个人仓库下拉里）。
    org = get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Organization not found"})
    if org.get("is_personal"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Personal organizations cannot be deleted"})
    from src.domains.mgmt.org_provision import delete_org_with_cleanup

    try:
        cleanup = delete_org_with_cleanup(org_id, delete_remote_repo=True)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"Failed to delete organization: {e}"}) from e
    return {"ok": True, **cleanup}


@router.get("/search/users", operation_id="orgs_search_users", summary="Search users")
def search_users(q: str = "", user: dict = Depends(require_no_session_token)):
    """按用户名模糊搜索用户（用于添加成员时的实时搜索）。"""
    # D32-补丁：查 grape_accounts，不查 user_accounts
    from src.kernel.session_manager import db as _db, db_lock as _dbl
    if not q or len(q) < 1:
        return []
    with _dbl:
        _c = _db.cursor()
        _c.execute(
            "SELECT id, username, nickname FROM grape_accounts "
            "WHERE username LIKE ? OR nickname LIKE ? "
            "ORDER BY username LIMIT 20",
            (f"%{q}%", f"%{q}%")
        )
        rows = _c.fetchall()
    return [{"id": r[0], "username": r[1], "nickname": r[2] or ""} for r in rows]


@router.get("/{org_id}/members", operation_id="orgs_members_list", summary="List organization members")
def list_members(org_id: str, user: dict = Depends(require_no_session_token)):
    if not is_org_member(user["id"], org_id):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Not a member of this organization"})
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


@router.post("/{org_id}/members", operation_id="orgs_members_add", summary="Add organization member")
def add_member(org_id: str, body: AddMemberBody, user: dict = Depends(require_no_session_token)):
    # Issue #381/#395: 同因越权修复——路径 org_id 绑定鉴权（非成员/非管理员无法对他人组织加人）
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    # 个人组织（默认工作空间）暂不支持添加成员——成员即用户本人，多人协作请创建团队组织
    from src.domains.mgmt.user.org_db import is_org_personal as _is_p
    if _is_p(org_id):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Personal workspaces do not support adding members; create a team organization for collaboration"})
    # 支持通过 username 或 user_id 添加
    target_uid = body.user_id
    if not target_uid and body.username:
        # D32-补丁：查 grape_accounts，不查 user_accounts
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        target = get_grape_account_by_username(body.username.strip())
        if not target:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"User '{body.username}' not found"})
        target_uid = target["id"]
    if not target_uid:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Provide username or user_id"})
    # 检查目标用户是否存在（D32-补丁：查 grape_accounts）
    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
    if not get_grape_account_by_id(target_uid):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "User not found"})
    ok = add_org_member(org_id, target_uid, body.role, invited_by=user["id"])
    if not ok:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "User is already a member"})
    return {"ok": True}


@router.patch("/{org_id}/members/{user_id}", operation_id="orgs_members_update", summary="Update member role")
def update_member(org_id: str, user_id: str, body: UpdateMemberBody,
                  user: dict = Depends(require_no_session_token)):
    # Issue #381/#395: 同因越权修复——路径 org_id 绑定鉴权
    from src.api.deps import require_path_org_admin
    role = require_path_org_admin(user["id"], org_id)
    if role != "owner":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Only the owner can change member roles"})
    ok = update_member_role(org_id, user_id, body.role)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Member not found"})
    return {"ok": True}


@router.delete("/{org_id}/members/{user_id}", operation_id="orgs_members_remove", summary="Remove organization member")
def remove_member(org_id: str, user_id: str, user: dict = Depends(require_no_session_token)):
    # Issue #381/#395: 同因越权修复——路径 org_id 绑定鉴权
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    ok = remove_org_member(org_id, user_id)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Member not found or owner cannot be removed"})
    return {"ok": True}


@router.post("/{org_id}/leave", operation_id="orgs_leave", summary="Leave organization")
def leave_org(org_id: str, user: dict = Depends(require_no_session_token)):
    role = get_member_role(user["id"], org_id)
    if not role:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Not a member of this organization"})
    if role == "owner":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Owner cannot leave the organization; transfer ownership first"})
    ok = remove_org_member(org_id, user["id"])
    return {"ok": ok}


# ===== Issue #2423 M2b（#2423 移植）：管理端 OV 凭据格子三端点 =====

class PutOVCredentialBody(BaseModel):
    """Issue #2423 M2b：管理端 OV 凭据格子配置体（明文仅入 vault，永不回显）。"""
    ov_api_key: str = Field(min_length=1)
    base_url: str = ""


@router.put("/{org_id}/ov-credentials/{user_id}",
            operation_id="orgs_ov_credential_put",
            summary="Set a member's OV credential (credential cell)",
            description="组织管理员为成员（含本人）配置/重置 OpenViking 凭据（凭据格子 user_id×org）。"
                        "明文经统一凭据库（vault）入库——全系统唯一持明文处，不落明文文件；"
                        "格子行只存 vault 句柄。重置同格时 vault ref 稳定（句柄引用不失效）。")
def put_ov_credential(org_id: str, user_id: str, body: PutOVCredentialBody,  # arch11 D105：org/user id 均为 UUID 串
                      user: dict = Depends(require_no_session_token)):
    # Issue #381/#395 同款：写接口鉴权绑定路径 org_id（组织不存在→404；非成员→403；非管理员→403）
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    if not is_org_member(user_id, org_id):
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "目标用户不是该组织的成员"})
    from src.domains.mgmt.auth.credential_cell import put_cell
    try:
        put_cell(user["id"], user_id, org_id, body.ov_api_key, body.base_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": 422, "message": str(e)})
    return {"ok": True, "user_id": user_id, "org_id": org_id}


@router.get("/{org_id}/ov-credentials",
            operation_id="orgs_ov_credentials_list",
            summary="List organization OV credential cells (masked)",
            description="列出本组织全部 OV 凭据格子（user_id×org）。只回存在性/base_url/时间戳等"
                        "元数据，凭据明文与 vault 句柄永不返回。仅组织管理员可见。")
def list_ov_credentials(org_id: str, user: dict = Depends(require_no_session_token)):  # arch11 D105
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    from src.domains.mgmt.auth.credential_cell import list_cells
    return list_cells(org_id)


@router.delete("/{org_id}/ov-credentials/{user_id}",
               operation_id="orgs_ov_credential_delete",
               summary="Revoke a member's OV credential (revocation is permanent)",
               description="吊销一格（user_id×org）：格子行与 vault 明文行同事务删除；执行期兑换"
                           "（exec 端点）每次实时解析、无缓存副本——吊销即刻生效、不可复活。")
def delete_ov_credential(org_id: str, user_id: str,  # arch11 D105：org/user id 均为 UUID 串
                         user: dict = Depends(require_no_session_token)):
    from src.api.deps import require_path_org_admin
    require_path_org_admin(user["id"], org_id)
    from src.domains.mgmt.auth.credential_cell import revoke_cell
    if not revoke_cell(user_id, org_id):
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "该成员未配置 OV 凭据（或已吊销）"})
    return {"ok": True}
