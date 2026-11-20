"""Organization repository binding, unbinding and transfer APIs."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import require_no_session_token, require_org_api_user
from src.domains.mgmt.auth.audit import log_audit
from src.domains.mgmt.user.org_db import (
    bind_managed_repo_to_org,
    get_managed_repo_binding,
    get_member_role,
    get_org,
    is_org_personal,
    list_bindable_repos,
    list_org_repos,
    transfer_managed_repo_to_org,
    unbind_managed_repo_from_org,
)
from src.kernel.repo_key import parse_repo_key, to_repo_key

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


class BindRepoBody(BaseModel):
    repo_key: str = ""
    full_name: str = ""


class ConfirmBody(BaseModel):
    confirm: bool = False


class TransferRepoBody(BaseModel):
    target_org_id: str = ""
    target_slug: str = ""
    target_owner: str = ""
    target_org: str = ""


def _schedule_repo_map_analysis(org_id: str, repo_key: str, actor_id: str | int = 0) -> None:
    """仓库绑定后触发组织级 AI 分析（后台去抖，不阻断绑定请求）。

    不再按单仓拆任务：10s 去抖窗口把一次批量绑定的多个仓库合并成一次
    mode=all 分析，AI 一次拿到全部关联仓库，逐个分析并回传。
    """
    def _run():
        try:
            from src.domains.mgmt import org_analysis

            org_analysis.ensure_analysis_automation(org_id, actor_id=actor_id)
            org_analysis.schedule_all_analysis(
                org_id, trigger="link", delay=10.0,
                fallback_repo_key=repo_key, actor_id=actor_id)
        except Exception as exc:  # noqa: BLE001 - best-effort
            from src.kernel.config import log

            log.warning(f"org repo map auto-analyze failed org={org_id} repo={repo_key}: {exc}")

    threading.Thread(
        target=_run, name=f"org-repo-map-{org_id}-{repo_key}", daemon=True,
    ).start()


def _schedule_context_sync(org_id: str) -> None:
    try:
        from src.domains.mgmt.org_context_repo import schedule_sync

        schedule_sync(org_id)
    except Exception:
        pass


def _error(status: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": status, "error_code": error_code, "message": message},
    )


def _permission_error() -> HTTPException:
    return _error(403, "PERMISSION_DENIED", "Organization admin or repository maintainer/admin permission required")


def _require_org(org_id: str) -> dict:
    org = get_org(org_id)
    if not org:
        raise _error(404, "ORG_NOT_FOUND", "Organization not found")
    return org


def _require_team_org(org_id: str) -> dict:
    org = _require_org(org_id)
    if is_org_personal(org_id):
        raise _error(400, "INVALID_ORG_CONTEXT", "Personal organizations do not support organization repository management")
    return org


def _require_member(user_id: int, org_id: str) -> str:
    _require_org(org_id)
    role = get_member_role(user_id, org_id)
    if not role:
        raise _error(403, "PERMISSION_DENIED", "Not a member of this organization")
    return role


def _require_admin(user_id: int, org_id: str) -> str:
    role = _require_member(user_id, org_id)
    if role not in ("owner", "admin"):
        raise _permission_error()
    return role


def _require_repo_maintainer(user: dict, repo_key: str) -> None:
    """Require VCS maintainer/admin permission for a managed repository."""
    try:
        from src.domains.mgmt.auth.repo_roles import is_repo_admin

        if is_repo_admin(user, repo_key):
            return
    except Exception:
        pass
    provider, owner, name = parse_repo_key(repo_key)
    try:
        from src.domains.mgmt.routes.repos import _check_migrate_vcs_permission

        _check_migrate_vcs_permission(user, owner, name, provider or "gitcode")
    except HTTPException as exc:
        if exc.status_code == 403:
            raise _permission_error()
        raise
    except Exception:
        raise _permission_error()


def _normalize_repo_key(body: BindRepoBody) -> str:
    raw = (body.repo_key or body.full_name or "").strip()
    if not raw:
        raise _error(400, "INVALID_REPO", "repo_key is required")
    provider = "gitcode"
    if ":" in raw:
        maybe_provider, rest = raw.split(":", 1)
        if maybe_provider in ("gitcode", "github"):
            provider = maybe_provider
            raw = rest
    parts = raw.split("/", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise _error(400, "INVALID_REPO", "repo_key must be provider:owner/repo or owner/repo")
    return to_repo_key(raw, provider=provider)


def _repo_payload(row: dict) -> dict:
    return {
        "repo_key": row.get("repo_key", ""),
        "repo_owner": row.get("repo_owner", ""),
        "repo_name": row.get("repo_name", ""),
        "full_name": row.get("full_name", ""),
        "org_id": str(row.get("org_id") or ""),
        "is_org_repo": bool(row.get("is_org_repo")),
        "linked_by": str(row.get("linked_by") or ""),
        "linked_at": row.get("linked_at") or "",
        "webhook_id": int(row.get("webhook_id") or 0),
    }


def _binding_or_404(repo_key: str, org_id: str) -> dict:
    row = get_managed_repo_binding(repo_key)
    if not row or str(row.get("org_id") or "") != str(org_id):
        raise _error(404, "REPO_NOT_HOSTED", "Repository is not hosted or does not belong to this organization")
    return row


@router.get("/{org_id}/repos", operation_id="org_repos_list", summary="List organization repositories")
def list_repos(org_id: str, user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    return {"repos": [_repo_payload(row) for row in list_org_repos(org_id)]}


@router.get("/{org_id}/repos/available", operation_id="org_repos_available",
            summary="List repositories available to link")
def list_available_repos(org_id: str, user: dict = Depends(require_no_session_token)):
    """返回当前用户名下可关联的托管仓库（Issue #2263 多选列表）。"""
    _require_member(user["id"], org_id)
    org = _require_team_org(org_id)
    rows = list_bindable_repos(org_id, str(user["id"]))
    org_repo_key = str(org.get("org_repo_key") or "")
    out = []
    for row in rows:
        item = _repo_payload(row)
        _oid = str(item.get("org_id") or "")
        item["bound_here"] = _oid == str(org_id)
        item["from_personal"] = _oid.isdigit() or is_org_personal(_oid)
        item["is_org_repo"] = str(item.get("repo_key") or "") == org_repo_key or item["is_org_repo"]
        out.append(item)
    return {"repos": out, "org_repo_key": org_repo_key}


def _bind_one(org: dict, org_id: str, user: dict, repo_key: str) -> dict:
    """单个仓库关联核心（单条/批量路由共用；鉴权由调用方完成）。"""
    _require_repo_maintainer(user, repo_key)

    row = get_managed_repo_binding(repo_key)
    if not row:
        raise _error(404, "REPO_NOT_HOSTED", "Repository is not hosted")
    current_org = str(row.get("org_id") or "")
    if current_org == org_id:
        return _repo_payload(row)
    allowed_source_org_id = ""
    if current_org:
        if is_org_personal(current_org):
            allowed_source_org_id = current_org
        else:
            raise _error(409, "REPO_ALREADY_BOUND", "Repository is already bound to another organization")
    bind_result = bind_managed_repo_to_org(
        repo_key,
        org_id,
        user["id"],
        is_org_repo=0,
        max_repos=int(org.get("max_repos") or 0),
        allowed_source_org_id=allowed_source_org_id,
    )
    if bind_result == "already_bound":
        return _repo_payload(get_managed_repo_binding(repo_key) or row)
    if bind_result == "quota":
        raise _error(409, "ORG_REPO_QUOTA_EXCEEDED", "Organization repository quota exceeded")
    if bind_result == "conflict":
        raise _error(409, "REPO_ALREADY_BOUND", "Repository is already bound to another organization")
    if bind_result == "not_hosted":
        raise _error(404, "REPO_NOT_HOSTED", "Repository is not hosted")
    if bind_result == "busy":
        raise _error(409, "ORG_REPO_BUSY", "Organization repository binding is busy; try again later")
    if bind_result == "protected":
        raise _error(409, "ORG_REPO_PROTECTED", "The organization repository ownership cannot be changed")
    row = get_managed_repo_binding(repo_key) or row
    log_audit(
        "org_repo_bind",
        user_id=user["id"],
        operator_id=user["id"],
        detail={"org_id": org_id, "repo_key": repo_key},
    )
    _schedule_context_sync(org_id)
    _schedule_repo_map_analysis(org_id, repo_key, user["id"])
    return _repo_payload(row)


@router.post("/{org_id}/repos", operation_id="org_repos_bind", summary="Link managed repository to organization")
def bind_repo(org_id: str, body: BindRepoBody, user: dict = Depends(require_no_session_token)):
    org = _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    repo_key = _normalize_repo_key(body)
    return _bind_one(org, org_id, user, repo_key)


class BindReposBatchBody(BaseModel):
    repo_keys: list[str] = []


@router.post("/{org_id}/repos/batch", operation_id="org_repos_bind_batch",
             summary="Batch link managed repositories")
def bind_repos_batch(org_id: str, body: BindReposBatchBody,
                     user: dict = Depends(require_no_session_token)):
    """批量关联（Issue #2263）：前端多选后一次提交，逐个返回结果。"""
    org = _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    seen: set[str] = set()
    results: list[dict] = []
    for raw in body.repo_keys or []:
        try:
            repo_key = _normalize_repo_key(BindRepoBody(repo_key=raw))
            if repo_key in seen:
                continue
            seen.add(repo_key)
            payload = _bind_one(org, org_id, user, repo_key)
            results.append({"repo_key": repo_key, "ok": True, "repo": payload})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.append({
                "repo_key": str(raw or ""),
                "ok": False,
                "error_code": detail.get("error_code") or f"HTTP_{exc.status_code}",
                "message": detail.get("message") or str(exc.detail),
            })
    bound = sum(1 for r in results if r["ok"])
    return {"ok": bound == len(results), "results": results,
            "bound": bound, "failed": len(results) - bound}


@router.post("/{org_id}/repos/{repo_key:path}/transfer",
             operation_id="org_repos_transfer", summary="Transfer organization repository")
def transfer_repo(org_id: str, repo_key: str, body: TransferRepoBody,
                  user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    source_key = _normalize_repo_key(BindRepoBody(repo_key=repo_key))

    target_org_id = str(body.target_org_id or "")
    target_key = (body.target_slug or body.target_owner or body.target_org or "").strip()
    if not target_org_id and target_key:
        from src.domains.mgmt.user.org_db import get_org_by_slug

        target = get_org_by_slug(target_key)
        target_org_id = str((target or {}).get("id") or "")
    if not target_org_id:
        raise _error(400, "INVALID_ORG_CONTEXT", "Target organization is required")
    target_org = _require_team_org(target_org_id)
    _require_admin(user["id"], target_org_id)
    if target_org_id == org_id:
        raise _error(400, "INVALID_ORG_CONTEXT", "Target organization must differ from the source organization")

    row = _binding_or_404(source_key, org_id)
    if bool(row.get("is_org_repo")):
        raise _error(409, "ORG_REPO_PROTECTED", "The organization repository cannot be transferred")
    _require_repo_maintainer(user, source_key)
    transfer_result = transfer_managed_repo_to_org(
        source_key,
        org_id,
        target_org_id,
        user["id"],
        max_repos=int(target_org.get("max_repos") or 0),
    )
    if transfer_result == "quota":
        raise _error(409, "ORG_REPO_QUOTA_EXCEEDED", "Organization repository quota exceeded")
    if transfer_result == "busy":
        raise _error(409, "ORG_REPO_BUSY", "Organization repository binding is busy; try again later")
    if transfer_result == "protected":
        raise _error(409, "ORG_REPO_PROTECTED", "The organization repository cannot be transferred")
    if transfer_result != "transferred":
        raise _error(409, "REPO_ALREADY_BOUND", "Repository transfer preconditions changed; refresh and retry")
    log_audit(
        "org_repo_transfer",
        user_id=user["id"],
        operator_id=user["id"],
        detail={
            "repo_key": source_key,
            "source_org_id": org_id,
            "target_org_id": target_org_id,
        },
    )
    _schedule_context_sync(org_id)
    _schedule_context_sync(target_org_id)
    try:
        from src.domains.mgmt import org_analysis

        org_analysis.ensure_analysis_automation(target_org_id, actor_id=user["id"])
    except Exception as ensure_exc:  # noqa: BLE001
        from src.kernel.config import log as _log

        _log.warning(f"org analysis automation ensure failed org={target_org_id}: {ensure_exc}")
    return _repo_payload(get_managed_repo_binding(source_key) or row)


@router.delete("/{org_id}/repos/{repo_key:path}",
               operation_id="org_repos_unbind", summary="Unlink organization repository")
def unbind_repo(org_id: str, repo_key: str,
                body: ConfirmBody | None = Body(default=None),
                confirm: bool = False,
                user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    if not (confirm or (body and body.confirm)):
        raise _error(400, "INVALID_REPO", "Unbinding requires confirm=true")
    normalized = _normalize_repo_key(BindRepoBody(repo_key=repo_key))
    row = _binding_or_404(normalized, org_id)
    if bool(row.get("is_org_repo")):
        raise _error(409, "ORG_REPO_PROTECTED", "The organization repository cannot be unbound")
    _require_repo_maintainer(user, normalized)
    unbind_result = unbind_managed_repo_from_org(normalized, org_id)
    if unbind_result == "dispatch_running":
        raise _error(409, "ORG_REPO_DISPATCH_RUNNING", "Repository has running dispatch tasks and cannot be unbound yet")
    if unbind_result == "protected":
        raise _error(409, "ORG_REPO_PROTECTED", "The organization repository cannot be unbound")
    if unbind_result == "busy":
        raise _error(409, "ORG_REPO_BUSY", "Organization repository binding is busy; try again later")
    if unbind_result != "unbound":
        raise _error(404, "REPO_NOT_HOSTED", "Repository is not hosted or does not belong to this organization")
    log_audit(
        "org_repo_unbind",
        user_id=user["id"],
        operator_id=user["id"],
        detail={"org_id": org_id, "repo_key": normalized},
    )
    _schedule_context_sync(org_id)
    return {"ok": True, "repo_key": normalized, "org_id": ""}


# ===== 组织仓库地图（MD-first） =====

@router.get("/{org_id}/repo-map", operation_id="org_repo_map_list",
            summary="List organization repository map")
def list_org_repo_map(org_id: str, user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    from src.domains.mgmt.org_repo_map import list_repo_map

    return list_repo_map(org_id)


@router.post("/{org_id}/repo-map/analyze", operation_id="org_repo_map_analyze_all",
             summary="Analyze all organization repositories")
def analyze_org_repo_map(org_id: str, user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    from src.domains.mgmt.org_repo_map import analyze_all

    result = analyze_all(org_id, actor_id=str(user["id"]))
    log_audit(
        "org_repo_map_analyze_all",
        user_id=str(user["id"]), operator_id=str(user["id"]),
        detail={"org_id": org_id, "queued": int(result.get("queued") or 0)},
    )
    return result


@router.get("/{org_id}/repo-map/recall", operation_id="org_repo_map_recall",
            summary="Recall target repositories by requirement")
def recall_org_repo_map(org_id: str, q: str = "", limit: int = 3,
                        user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    from src.domains.mgmt.org_repo_map import recall_targets

    return recall_targets(org_id, q, limit=limit)


@router.get("/{org_id}/repo-map/summary", operation_id="org_repo_map_summary",
            summary="Get the aggregate repository map summary")
def get_org_repo_map_summary(org_id: str, user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    from src.domains.mgmt.org_repo_map import repo_map_summary

    return repo_map_summary(org_id)


@router.post("/{org_id}/repo-map/sync", operation_id="org_repo_map_sync",
             summary="Ensure repository map documents exist in the org repository")
def sync_org_repo_map(org_id: str, user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    from src.domains.mgmt.org_context_repo import ensure_repo_map_summary_file
    from src.domains.mgmt.org_repo_map import repo_map_summary

    ok = ensure_repo_map_summary_file(org_id)
    log_audit(
        "org_repo_map_sync",
        user_id=str(user["id"]), operator_id=str(user["id"]),
        detail={"org_id": org_id, "ok": bool(ok)},
    )
    data = repo_map_summary(org_id)
    return {"ok": bool(ok), "branch": data.get("branch"),
            "path": data.get("path"), "url": data.get("url")}


@router.get("/{org_id}/repo-map/{repo_key:path}", operation_id="org_repo_map_detail",
            summary="Get repository map summary")
def get_org_repo_map(org_id: str, repo_key: str, user: dict = Depends(require_org_api_user)):
    _require_member(user["id"], org_id)
    _require_team_org(org_id)
    from src.domains.mgmt.org_repo_map import get_map

    data = get_map(org_id, repo_key)
    if not data:
        raise _error(404, "REPO_MAP_NOT_FOUND", "Repository map has not been generated yet")
    return data


@router.post("/{org_id}/repo-map/{repo_key:path}/analyze",
             operation_id="org_repo_map_analyze_one",
             summary="Analyze one organization repository")
def analyze_one_org_repo_map(org_id: str, repo_key: str,
                             user: dict = Depends(require_no_session_token)):
    _require_team_org(org_id)
    _require_admin(user["id"], org_id)
    from src.domains.mgmt.org_repo_map import analyze_repo

    try:
        data = analyze_repo(org_id, repo_key, actor_id=str(user["id"]))
    except ValueError:
        raise _error(404, "REPO_NOT_IN_ORG", "Repository is not linked to this organization")
    log_audit(
        "org_repo_map_analyze",
        user_id=str(user["id"]), operator_id=str(user["id"]),
        detail={"org_id": org_id, "repo_key": repo_key},
    )
    return data
