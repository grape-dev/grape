"""组织仓库默认分支解析（只读）。

组织场景 / 每日分析任务的工作分支必须与组织仓库实际分支一致：
- 新建组织仓库保留 ``main`` 作为默认分支，且保持空仓，留给组织自己开发；
- Grape 托管内容全部在 ``grape-org`` 分支；
- 存量组织仓库可能仍是 ``main`` 或其他默认分支，不能按新默认硬编码。

本模块通过 VCS 仓库信息读取默认分支；读取失败时由调用方给出安全兜底。
"""
from __future__ import annotations

from src.kernel.config import log
from src.kernel.repo_key import parse_repo_key


def resolve_org_repo_default_branch(org: dict, repo_key: str, *,
                                    fallback: str = "") -> str:
    """读取组织仓库的默认分支；失败/空值返回 ``fallback``（缺省为统一常量）。"""
    repo_key = str(repo_key or "").strip()
    if not fallback:
        from src.domains.mgmt.org_context_repo import ORG_REPO_DEFAULT_BRANCH

        fallback = ORG_REPO_DEFAULT_BRANCH
    if not repo_key:
        return fallback
    try:
        provider, owner, name = parse_repo_key(repo_key)
        if not (provider and owner and name):
            return fallback
        owner_id = str((org or {}).get("owner_id") or "").strip()
        username = ""
        try:
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id

            account = get_grape_account_by_id(owner_id) or {}
            username = str(account.get("username") or "")
        except Exception as exc:  # noqa: BLE001 - 账号读取失败按无 token 处理
            log.warning(f"org_repo_branch: load owner account failed org={org.get('id')}: {exc}")
        if not username:
            return fallback
        from src.api.deps import get_user_token_for

        token = get_user_token_for(username, user_id=owner_id, provider=provider) or ""
        if not token:
            return fallback
        from src.domains.runtime import public as runtime_public

        info = runtime_public.VcsApiClient(provider, token, owner, name).get_repo_info() or {}
        branch = str(info.get("default_branch") or "").strip()
        if branch:
            return branch
        log.warning(
            f"org_repo_branch: default_branch empty repo={repo_key} "
            f"error={str(info.get('error') or '')[:160]}")
    except Exception as exc:  # noqa: BLE001 - 网络/平台异常不阻断 provisioning
        log.warning(f"org_repo_branch: resolve failed repo={repo_key}: {exc}")
    return fallback
