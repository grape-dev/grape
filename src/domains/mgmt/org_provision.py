"""Organization repository and default scene provisioning."""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from src.api.pipeline_config import create_org_scene
from src.domains.mgmt.auth.audit import log_audit
from src.domains.mgmt.org_dispatch import DISPATCH_IN_FLIGHT
from src.domains.mgmt.routes.repos import (
    CreateRepoBody,
    RepoAction,
    _get_user_provider_and_token,
    check_repo_name,
    create_new_repo,
    host_repo,
)
from src.domains.mgmt.user.org_db import (
    bind_managed_repo_to_org,
    bind_repo_as_org_repo,
    clear_org_repo_flag,
    fail_org_provision_attempt,
    get_managed_repo_binding,
    get_org,
    has_running_org_dispatch,
    is_org_personal,
    update_org_provision_fields,
)
from src.kernel.config import log
from src.kernel.lock import acquire_lock, release_lock, renew_lock
from src.kernel.repo_key import from_repo_key, to_repo_key


_ORG_LOCKS: dict[int, threading.Lock] = {}
_ORG_LOCKS_GUARD = threading.Lock()
# rationale（数值登记 docs/org-pipeline/org-runtime-budget.md §2）：组织供给一次包含
# 建仓 + 建场景 + 首次绑定，需求 Issue #2403 的目标 P95 供给时长 < 10min；租约取 600s
# 覆盖“建仓失败重试 + 平台限流等待”的最长单轮路径。供给循环每轮 renew_lock，
# 正常长任务不会因 TTL 到期丢锁，只有进程静默死亡才由 TTL 释放给重试者。
# rationale: docs/org-pipeline/org-runtime-budget.md §2（Issue #2403：供给 P95 < 10min）
_ORG_LEASE_TTL = 600


class _LeaseLost(RuntimeError):
    pass


def _org_lock(org_id: str) -> threading.Lock:
    with _ORG_LOCKS_GUARD:
        lock = _ORG_LOCKS.get(org_id)
        if lock is None:
            lock = threading.Lock()
            _ORG_LOCKS[org_id] = lock
        return lock


def _org_lease_key(org_id: str) -> str:
    return f"org-provision:{org_id}"


def _renew_org_lease(org_id: str) -> None:
    if not renew_lock(_org_lease_key(org_id), ttl=_ORG_LEASE_TTL):
        raise _LeaseLost("ORG_PROVISION_LEASE_LOST")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("error_code") or detail)
        return str(detail)
    return str(exc)


def _error(status: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": status, "error_code": error_code, "message": message},
    )


def _serialize_org(org: dict | None, *, skipped: bool = False) -> dict:
    org = org or {}
    status = org.get("provision_status") or "none"
    error = org.get("provision_error") or ""
    return {
        "org_id": org.get("id", 0),
        "org_repo_key": org.get("org_repo_key") or "",
        "org_scene_id": org.get("org_scene_id") or "",
        "provision_status": status,
        "provision_error": error,
        "error": error,
        "provisioned_at": org.get("provisioned_at") or "",
        "skipped": skipped,
    }


def _set_provision_fields(org_id: str, **fields) -> None:
    update_org_provision_fields(org_id, **fields)


def _owner_user(org: dict) -> dict:
    # D105 第三批：owner_id 为 TEXT（uuid/保值 int 字符串），不可 int() 强转
    owner_id = str(org.get("owner_id") or "").strip()
    username = f"user-{owner_id}" if owner_id else ""
    login_provider = ""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id

        account = get_grape_account_by_id(owner_id) or {}
        username = account.get("username") or username
        login_provider = account.get("login_provider") or ""
    except Exception as exc:
        log.warning(f"org_provision: load owner account failed org={org.get('id')}: {exc}")
    return {
        "id": owner_id,
        "user_id": owner_id,
        "username": username,
        "login_provider": login_provider,
        "auth_type": "jwt",
    }


def _repo_name(org: dict) -> str:
    raw = str(org.get("slug") or f"org-{org.get('id') or 0}").strip().lower()
    slug = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-.") or f"org-{org.get('id') or 0}"
    name = f"org-{slug}"
    if len(name) > 80:
        name = f"{name[:64].rstrip('-')}-{org.get('id') or 0}"
    return name


def _repo_description(org: dict) -> str:
    return f"Managed repository for organization {org.get('name') or org.get('id')}"


def _repo_create_options(org: dict) -> dict:
    """组织仓库创建选项（组织创建时用户选择，空值回落到历史默认行为）。"""
    provider = str(org.get("org_repo_provider") or "").strip().lower()
    owner = str(org.get("org_repo_owner") or "").strip()
    path = str(org.get("org_repo_path") or "").strip()
    name = str(org.get("org_repo_name") or "").strip() or _repo_name(org)
    try:
        private = bool(int(org.get("org_repo_private")
                           if org.get("org_repo_private") is not None else 1))
    except (TypeError, ValueError):
        private = True
    return {
        "provider": provider,
        "owner": owner,
        "name": name,
        "path": path,
        "private": private,
    }


def _resolve_repo_provider(org: dict, owner_user: dict) -> tuple[str, str]:
    """返回 (provider, token)。组织指定了平台时只认该平台，否则沿用自动选择。"""
    provider = str(org.get("org_repo_provider") or "").strip().lower()
    if not provider:
        return _get_user_provider_and_token(
            owner_user.get("username", ""),
            owner_user.get("login_provider", ""),
            owner_user.get("id", 0),
        )
    from src.api.deps import get_user_token_for

    token = get_user_token_for(
        owner_user.get("username", ""),
        user_id=str(owner_user.get("id") or ""),
        provider=provider,
    )
    return provider, token or ""


def _claim_existing_org_repo(org: dict, name: str) -> str:
    """Claim an org repo that exists after a pre-persist crash/retry.

    Only claims when the existing repo carries the provisioning ownership
    evidence (private + the exact managed description), so a user-created repo
    that happens to share the deterministic name is never hijacked.
    """
    try:
        owner_user = _owner_user(org)
        provider, token = _resolve_repo_provider(org, owner_user)
        if not token:
            return ""
        expected_description = _repo_description(org)
        configured_owner = str(org.get("org_repo_owner") or "").strip()
        if not configured_owner:
            # 个人空间：复用 check_repo_name（当前 token 用户名下探活，口径与旧行为一致）
            existing = check_repo_name(name, owner_user) or {}
            full_name = str(existing.get("full_name") or "").strip()
            if not existing.get("exists") or not full_name:
                return ""
            claimed_description = str(existing.get("description") or "").strip()
            if not existing.get("private") or claimed_description != expected_description:
                log.warning(
                    f"org_provision claim skipped ownership evidence mismatch "
                    f"org={org.get('id')} repo={full_name} private={existing.get('private')} "
                    f"description={claimed_description!r}"
                )
                return ""
            repo_key = to_repo_key(full_name, provider=provider)
            log.info(f"org_provision claimed existing repo org={org.get('id')} repo={repo_key}")
            return repo_key

        # 组织空间：按选定 owner 探活（check_repo_name 只查当前用户名下）
        from src.domains.runtime import public as runtime_public
        VcsApiClient = runtime_public.VcsApiClient

        existing = VcsApiClient(provider, token, configured_owner, name).get_repo_info() or {}
        if not isinstance(existing, dict) or existing.get("error"):
            return ""
        full_name = str(existing.get("full_name") or "").strip() or f"{configured_owner}/{name}"
        claimed_description = str(existing.get("description") or "").strip()
        is_private = bool(existing.get("private")) or \
            str(existing.get("visibility") or "").strip().lower() == "private"
        if not is_private or claimed_description != expected_description:
            log.warning(
                f"org_provision claim skipped ownership evidence mismatch "
                f"org={org.get('id')} repo={full_name} private={is_private} "
                f"description={claimed_description!r}"
            )
            return ""
        repo_key = to_repo_key(full_name, provider=provider)
        log.info(f"org_provision claimed existing repo org={org.get('id')} repo={repo_key}")
        return repo_key
    except Exception as exc:
        log.warning(
            f"org_provision claim existing repo failed org={org.get('id')} name={name}: {exc}"
        )
        return ""


def _create_org_repo(org: dict) -> str:
    _renew_org_lease(org["id"])
    owner_user = _owner_user(org)
    options = _repo_create_options(org)
    name = options["name"]
    private = bool(options["private"])
    try:
        result = create_new_repo(
            CreateRepoBody(
                name=name,
                description=_repo_description(org),
                private=private,
                visibility="private" if private else "public",
                auto_init=False,
                default_branch="grape-repo-knowledge",
                host_after_create=False,
                provider=options["provider"],
                owner=options["owner"],
                path=options["path"],
            ),
            owner_user,
        )
    except Exception:
        existing_key = _claim_existing_org_repo(org, name)
        if existing_key:
            _set_provision_fields(org["id"], org_repo_key=existing_key)
            _renew_org_lease(org["id"])
            return existing_key
        raise
    _renew_org_lease(org["id"])
    result = result or {}
    full_name = str(result.get("full_name") or "").strip()
    if not full_name:
        repo = result.get("repo") or {}
        full_name = str(repo.get("full_name") or "").strip()
    if not full_name:
        raise RuntimeError("VCS repository creation returned no full_name")
    provider = str(result.get("provider") or options["provider"] or "").strip().lower()
    if not provider:
        provider, _token = _get_user_provider_and_token(
            owner_user.get("username", ""),
            owner_user.get("login_provider", ""),
            owner_user.get("id", 0),
        )
        provider = provider or "gitcode"
    repo_key = to_repo_key(full_name, provider=provider)
    _set_provision_fields(org["id"], org_repo_key=repo_key)
    return repo_key


def _bind_org_repo(org: dict, repo_key: str) -> None:
    result = bind_managed_repo_to_org(
        repo_key,
        str(org["id"] or ""),
        str(org.get("owner_id") or ""),
        is_org_repo=1,
        max_repos=int(org.get("max_repos") or 0),
    )
    if result in ("bound", "already_bound"):
        if result == "bound":
            log_audit(
                "org_repo_bind",
                user_id=str(org.get("owner_id") or ""),
                operator_id=str(org.get("owner_id") or ""),
                detail={
                    "org_id": str(org["id"] or ""),
                    "repo_key": repo_key,
                    "is_org_repo": True,
                    "via": "org_provision",
                },
            )
        try:
            from src.domains.mgmt import org_analysis

            org_analysis.ensure_analysis_automation(str(org["id"] or ""),
                                                     actor_id=str(org.get("owner_id") or ""))
        except Exception as _ae:
            log.warning(f"org_provision analysis automation failed org={org.get('id')}: {_ae}")
        return
    code = {
        "protected": "ORG_REPO_PROTECTED",
        "quota": "ORG_REPO_QUOTA_EXCEEDED",
        "busy": "ORG_REPO_BUSY",
        "conflict": "REPO_ALREADY_BOUND",
        "not_hosted": "REPO_NOT_HOSTED",
    }.get(result, "ORG_REPO_BIND_FAILED")
    raise RuntimeError(code)


def _host_org_repo(org: dict, repo_key: str) -> None:
    _renew_org_lease(org["id"])
    binding = get_managed_repo_binding(repo_key)
    bound_org_id = str((binding or {}).get("org_id") or "")
    if binding:
        if bound_org_id not in ("", str(org["id"] or "")):
            raise RuntimeError("REPO_ALREADY_BOUND")
        if int(binding.get("webhook_id") or 0) > 0:
            _bind_org_repo(org, repo_key)
            _renew_org_lease(org["id"])
            return

    owner_user = _owner_user(org)
    # host_repo 的历史接口会顺手写 org_id；新仓库按未归属托管，已有目标
    # 归属则保留，再由统一绑定 DAL 完成配额校验与保护标记写入。
    host_user = {**owner_user, "org_id": bound_org_id}
    result = host_repo(
        RepoAction(full_name=from_repo_key(repo_key)),
        host_user,
        request=None,
        provider=repo_key.split(":", 1)[0],
    )
    if isinstance(result, dict) and result.get("hosted") is False:
        raise RuntimeError(result.get("message") or "repository hosting failed")
    _bind_org_repo(org, repo_key)
    _renew_org_lease(org["id"])


def _ensure_org_scene(org: dict, repo_key: str) -> str:
    _renew_org_lease(org["id"])
    scene_id = org.get("org_scene_id") or ""
    if not scene_id:
        scene_id = f"custom-org-{uuid.uuid4()}"
        _set_provision_fields(org["id"], org_scene_id=scene_id)
    owner_user = _owner_user(org)
    create_org_scene(
        scene_id=scene_id,
        org_name=org.get("name") or "",
        repo_key=repo_key,
        created_by=owner_user.get("username") or "",
        org_id=str(org["id"] or ""),
    )
    # 组织仓库默认场景 = 组织场景：保证直接在组织仓库开 Issue 时 issue_opened 能进入
    # org_intake，而不是回落到 vod/智能推荐。
    try:
        from src.domains.mgmt.user.db import set_repo_scene

        set_repo_scene(repo_key, scene_id)
    except Exception as exc:
        log.warning(f"org_provision set default scene failed org={org['id']}: {exc}")
    _renew_org_lease(org["id"])
    return scene_id


def _normalize_repo_key(raw: str) -> str:
    """归一化手动绑定的 repo_key（provider:owner/repo 或 owner/repo）。"""
    text = (raw or "").strip()
    if not text:
        raise _error(400, "INVALID_REPO", "repo_key 不能为空")
    provider = "gitcode"
    if ":" in text:
        provider, text = text.split(":", 1)
        provider = provider.strip().lower()
        if provider not in ("gitcode", "github"):
            raise _error(400, "INVALID_REPO", "repo_key provider 仅支持 gitcode/github")
        text = text.strip()
    parts = text.split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise _error(400, "INVALID_REPO", "repo_key 必须为 provider:owner/repo 或 owner/repo")
    return to_repo_key(text, provider=provider)


def _delete_vcs_repo(repo_key: str, token: str) -> None:
    """删除 VCS 远端仓库（仅组织自动仓库，best-effort 由调用方兜住）。"""
    from src.domains.mgmt.user import repo_manager
    from src.kernel.repo_key import parse_repo_key

    provider, owner, name = parse_repo_key(repo_key or "")
    if provider == "github":
        repo_manager.delete_github_repo(token, owner, name)
    elif provider == "gitcode":
        repo_manager.delete_gitcode_repo(token, owner, name)
    else:
        raise RuntimeError(f"provider {provider!r} 暂不支持删除仓库（仅 gitcode/github）")


def delete_org_with_cleanup(org_id: str, *, delete_remote_repo: bool = True) -> dict:
    """删除组织并回收其仓库关联（Issue #2261）。

    - 组织自动仓库（is_org_repo=1）：默认同时删除 VCS 远端仓库 + 本地托管记录；
      远端删除失败不阻断组织删除（记入 errors，本地记录仍清理，避免孤儿残留）。
    - 手工关联仓库（is_org_repo=0）：只退回「未归属」，绝不删除远端仓库。
    - 组织场景行：删除，避免删除组织后场景仍挂在不存在的 org 上。
    """
    from src.domains.mgmt.user.org_db import (
        delete_org, delete_org_repo_records, delete_org_scenes,
        get_org, list_org_repos, unbind_org_repo_local,
    )

    org = get_org(org_id)
    if not org:
        raise _error(404, "ORG_NOT_FOUND", "组织不存在")
    if org.get("is_personal"):
        raise _error(400, "PERSONAL_ORG_NOT_DELETABLE", "个人组织不可删除")

    result = {"deleted_remote": [], "removed_local": [], "unbound": [], "errors": []}
    repos = list_org_repos(org_id)
    default_token = ""
    owner = {}
    if delete_remote_repo:
        try:
            from src.api.deps import get_user_token_with_refresh

            owner = _owner_user(org)
            default_token = get_user_token_with_refresh(
                owner.get("username") or "", owner.get("id") or "")
        except Exception as exc:
            log.warning(f"org delete: load owner token failed org={org_id}: {exc}")

    _provider_tokens: dict[str, str] = {}

    def _token_for_provider(provider: str) -> str:
        """按仓库 provider 取 owner token（GitHub 组织仓库不能用 GitCode token 删）。"""
        if provider in _provider_tokens:
            return _provider_tokens[provider]
        token = ""
        if provider and owner:
            try:
                from src.api.deps import get_user_token_for

                token = get_user_token_for(
                    owner.get("username") or "",
                    user_id=str(owner.get("id") or ""),
                    provider=provider,
                )
            except Exception as exc:
                log.warning(
                    f"org delete: load {provider} token failed org={org_id}: {exc}")
        token = token or default_token
        _provider_tokens[provider] = token
        return token

    for row in repos:
        repo_key = (row or {}).get("repo_key") or ""
        if not repo_key:
            continue
        if row.get("is_org_repo"):
            if delete_remote_repo:
                from src.kernel.repo_key import parse_repo_key

                provider = parse_repo_key(repo_key)[0]
                token = _token_for_provider(provider)
                if token:
                    try:
                        _delete_vcs_repo(repo_key, token)
                        result["deleted_remote"].append(repo_key)
                    except Exception as exc:
                        log.warning(f"org delete: remote repo delete failed repo={repo_key}: {exc}")
                        result["errors"].append(f"{repo_key}: {str(exc)[:160]}")
                else:
                    result["errors"].append(f"{repo_key}: 无可用 token，未删除远端仓库")
            delete_org_repo_records(repo_key)
            result["removed_local"].append(repo_key)
        else:
            if unbind_org_repo_local(repo_key, org_id):
                result["unbound"].append(repo_key)

    try:
        delete_org_scenes(org_id)
    except Exception as exc:
        log.warning(f"org delete: scene cleanup failed org={org_id}: {exc}")

    delete_org(org_id)
    log.info(
        f"org delete: org={org_id} remote={len(result['deleted_remote'])} "
        f"local={len(result['removed_local'])} unbound={len(result['unbound'])} "
        f"errors={len(result['errors'])}"
    )
    return result


def manual_bind_org_repo(org_id: str, repo_key: str, *, actor_id: str = "",
                         confirm: bool = False) -> dict:
    """手动将已托管仓库指定为组织仓库（Issue #2258 自动 provisioning 兜底）。

    校验单归属与替换保护，补齐组织场景后置 ready 并写审计。不新建仓库、不迁移场景。
    """
    key = _normalize_repo_key(repo_key)
    lock = _org_lock(str(org_id))
    with lock:
        org = get_org(str(org_id))
        if not org:
            raise _error(404, "ORG_NOT_FOUND", "组织不存在")
        if is_org_personal(str(org_id)):
            raise _error(400, "INVALID_ORG_CONTEXT", "个人组织不支持手动绑定组织仓库")

        binding = get_managed_repo_binding(key)
        if not binding:
            raise _error(404, "REPO_NOT_HOSTED", "仓库尚未托管")
        bound_org_id = str(binding.get("org_id") or "")
        if bound_org_id not in ("", str(org_id)):
            raise _error(409, "REPO_ALREADY_BOUND", "仓库已归属其它组织")

        previous_key = org.get("org_repo_key") or ""
        replaced = bool(previous_key and previous_key != key)
        if replaced:
            if not confirm:
                raise _error(409, "CONFIRM_REQUIRED", "组织已绑定其它仓库，替换需 confirm=true")
            if has_running_org_dispatch(str(org_id), previous_key,
                                        statuses=DISPATCH_IN_FLIGHT):
                raise _error(409, "ORG_REPO_EXISTS", "旧组织仓库仍被运行中的下派引用，暂不可替换")

        scene_id = org.get("org_scene_id") or ""
        if not scene_id:
            _lease_key = _org_lease_key(str(org_id))
            if not acquire_lock(_lease_key, ttl=_ORG_LEASE_TTL):
                raise _error(409, "ORG_PROVISION_BUSY", "组织 provisioning 进行中，请稍后重试")
            try:
                scene_id = _ensure_org_scene(org, key)
            except Exception as exc:
                # _ensure_org_scene 先落占位 scene_id 再建场景；失败需回滚，避免重试时跳过创建
                _set_provision_fields(org_id, org_scene_id="")
                log.warning(f"org_provision manual bind scene failed org={org_id}: {exc}")
                raise _error(500, "SCENE_CREATE_FAILED", "组织场景创建失败") from exc
            finally:
                release_lock(_lease_key)

        if not bind_repo_as_org_repo(key, str(org_id), str(actor_id or "")):
            latest = get_managed_repo_binding(key)
            if not latest or str(latest.get("org_id") or "") != str(org_id):
                raise _error(409, "REPO_ALREADY_BOUND", "仓库归属已变化，请刷新后重试")
        if replaced:
            clear_org_repo_flag(previous_key, str(org_id))

        _set_provision_fields(
            org_id,
            org_repo_key=key,
            provision_status="ready",
            provision_error="",
            provisioned_at=_utc_now(),
        )
        log_audit(
            "org_provision_manual_bind",
            user_id=str(actor_id or ""),
            operator_id=str(actor_id or ""),
            detail={
                "org_id": str(org_id),
                "repo_key": key,
                "previous_repo_key": previous_key,
                "replaced": replaced,
                "scene_id": scene_id,
            },
        )

        try:
            from src.domains.mgmt import org_analysis

            org_analysis.ensure_analysis_automation(org_id, actor_id=actor_id)
        except Exception as _ae:
            log.warning(f"org_provision manual bind analysis automation failed org={org_id}: {_ae}")

        return _serialize_org(get_org(org_id))


def _provision_org_impl(org_id: str) -> dict:
    """Provision an organization idempotently, filling only missing steps."""
    lock = _org_lock(str(org_id))
    with lock:
        org = get_org(org_id)
        if not org:
            return {
                "org_id": org_id,
                "org_repo_key": "",
                "org_scene_id": "",
                "provision_status": "failed",
                "provision_error": "ORG_NOT_FOUND",
                "error": "ORG_NOT_FOUND",
                "provisioned_at": "",
                "skipped": False,
            }
        if is_org_personal(org_id):
            return _serialize_org(org, skipped=True)
        if (org.get("provision_status") or "none") == "ready":
            return _serialize_org(org)

        lease_key = _org_lease_key(org_id)
        if not acquire_lock(lease_key, ttl=_ORG_LEASE_TTL):
            return _serialize_org(org)
        attempt = uuid.uuid4().hex
        try:
            org = get_org(org_id) or org
            if is_org_personal(org_id):
                return _serialize_org(org, skipped=True)
            if (org.get("provision_status") or "none") == "ready":
                return _serialize_org(org)

            _renew_org_lease(org_id)
            _set_provision_fields(
                org_id,
                provision_status="pending",
                provision_error="",
                provision_attempt=attempt,
            )
            org = get_org(org_id) or org
            repo_key = org.get("org_repo_key") or ""
            if not repo_key:
                _set_provision_fields(org_id, provision_status="repo_creating")
                org = get_org(org_id) or org
                repo_key = _create_org_repo(org)

            org = get_org(org_id) or org
            repo_key = org.get("org_repo_key") or repo_key
            _set_provision_fields(org_id, provision_status="hosting")
            _host_org_repo(org, repo_key)

            org = get_org(org_id) or org
            _set_provision_fields(org_id, provision_status="scene_creating")
            _ensure_org_scene(org, repo_key)

            _renew_org_lease(org_id)
            _set_provision_fields(
                org_id,
                provision_status="ready",
                provision_error="",
                provisioned_at=_utc_now(),
            )
            _refresh_org_overview(org_id)
            # 组织仓库建好后把 org-context/（README/规则/画像/架构/知识/仓库地图）
            # 推到默认分支：否则新仓是空仓，GitHub 页面只有一个空仓库没有任何目录。
            try:
                from src.domains.mgmt.org_context_repo import schedule_sync

                schedule_sync(org_id)
            except Exception as _sync_e:
                log.warning(f"org_provision schedule context sync failed org={org_id}: {_sync_e}")
        except _LeaseLost:
            log.warning(f"org_provision lease lost org={org_id}")
            try:
                fail_org_provision_attempt(
                    org_id,
                    attempt,
                    "ORG_PROVISION_LEASE_LOST",
                )
            except Exception as mark_exc:
                log.warning(
                    f"org_provision lease-lost state update failed org={org_id}: {mark_exc}"
                )
        except Exception as exc:
            message = _error_text(exc)[:1000]
            log.warning(f"org_provision failed org={org_id}: {message}")
            _set_provision_fields(
                org_id,
                provision_status="failed",
                provision_error=message,
            )
        finally:
            release_lock(lease_key)
        return _serialize_org(get_org(org_id))


def provision_org(org_id: str) -> dict:
    """组织供给入口：保留原幂等语义，并接入 Metrics + Tempo feature span（A13）。"""
    from src.kernel import org_metrics
    from src.kernel.otel import feature_span

    started = time.monotonic()
    result_label = "failed"
    log_audit("org_provision_started", detail={"org_id": str(org_id)})
    try:
        with feature_span("org.provision", {"org_id": str(org_id)}):
            result = _provision_org_impl(org_id)
        result_label = str(result.get("provision_status") or "") or "unknown"
        _event = ("org_provision_completed" if result_label == "ready"
                  else ("org_provision_failed" if result_label == "failed" else ""))
        if _event:
            log_audit(
                _event,
                detail={
                    "org_id": str(org_id),
                    "provision_status": result_label,
                    "provision_error": str(result.get("provision_error") or "")[:500],
                },
            )
        return result
    except Exception as exc:
        result_label = "failed"
        log_audit("org_provision_failed", detail={
            "org_id": str(org_id),
            "provision_error": _error_text(exc)[:500],
        })
        raise
    finally:
        org_metrics.observe_provision(result_label, time.monotonic() - started)


def _refresh_org_overview(org_id: str) -> None:
    """provisioning 完成后补全「组织概览」上下文（Issue #2269）。

    组织创建时先写基础概览；这里补组织仓库、组织场景、节点链路与已关联仓库。
    幂等（brief 单 key；内容未变化不写）。失败仅告警，不影响 provisioning。
    """
    try:
        from src.domains.mgmt import org_context_store as _ocs
        from src.domains.mgmt.user.org_db import get_org, list_org_repos

        org = get_org(org_id) or {}
        if not org or int(org.get("is_personal") or 0):
            return
        repos = list_org_repos(org_id) or []
        scene_id = org.get("org_scene_id") or ""
        nodes: list = []
        try:
            from src.api.pipeline_config import get_pipeline_config

            cfg = get_pipeline_config(scene_id, repo_key=org.get("org_repo_key") or "") or {}
            nodes = cfg.get("nodes") or []
        except Exception:
            nodes = []
        lines = [
            "# 组织概览", "",
            f"- 名称：{org.get('name') or ''}",
            f"- Slug：{org.get('slug') or ''}",
            f"- 描述：{org.get('description') or '（未填写）'}",
            "- 类型：团队组织",
            f"- 组织仓库：`{org.get('org_repo_key') or ''}`",
            f"- 组织场景：`{scene_id}`",
        ]
        if nodes:
            lines.append("- 流水线节点：")
            for n in nodes:
                lines.append(
                    f"  - {n.get('name') or n.get('id') or ''}"
                    f"（tag={n.get('tag') or ''}, skill={n.get('skill') or ''}）")
        if repos:
            lines.append(f"- 已关联目标仓库（{len(repos)}）：")
            for r in repos:
                lines.append(
                    f"  - `{r.get('repo_key') or ''}`"
                    + ("（组织自动仓库）" if r.get("is_org_repo") else ""))
        else:
            lines.append("- 已关联目标仓库：暂无")
        lines += ["", "> 系统自动生成；组织需求下派的结论会自动沉淀为 decision 上下文。"]
        _md = "\n".join(lines)
        _ocs.upsert_context(
            org_id, "brief", "overview", "组织概览", _md,
            owner_id=str(org.get("owner_id") or ""), user_id=str(org.get("owner_id") or ""))
        # 同时沉淀一条组织知识「组织概览」，让知识库也有可检索的总结
        try:
            from src.domains.mgmt.org_knowledge import upsert_knowledge
            upsert_knowledge(
                org_id, org.get("org_repo_key") or "", kind="summary", name="组织概览",
                description="组织仓库 / 场景 / 关联仓库自动汇总",
                content=_md, tags=["org", "overview"], applies_to="org",
                owner_id=str(org.get("owner_id") or ""),
                created_by=str(org.get("owner_id") or ""),
                source="system")
        except Exception as _ke:
            log.warning(f"refresh org overview knowledge failed org={org_id}: {_ke}")
    except Exception as e:
        log.warning(f"refresh org overview failed org={org_id}: {e}")


def _provision_worker(org_id: str) -> None:
    try:
        provision_org(org_id)
    except Exception as exc:
        log.warning(f"org_provision worker crashed org={org_id}: {exc}")
        try:
            _set_provision_fields(
                org_id,
                provision_status="failed",
                provision_error=_error_text(exc)[:1000],
            )
        except Exception:
            pass


def start_provision(org_id: str) -> None:
    """Run provisioning in a daemon thread so org creation is never blocked."""
    threading.Thread(
        target=_provision_worker,
        args=(str(org_id),),
        name=f"org-provision-{org_id}",
        daemon=True,
    ).start()
