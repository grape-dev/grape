"""仓库角色校验（Issue #393）。

判定「当前用户在指定仓库是否为 admin」，以**平台真实角色**为准（参考 #377：
本地 user_repos 缓存的 is_admin 可能过期/不准），实时查 VCS collaborator permission：

- GitCode: GET /repos/{owner}/{repo}/collaborators/{username}/permission
  admin 判定：access_level >= 50，或单数 permission == "admin"，或 username == owner。
  （与 repo_manager._check_collaborator_admin 的 >=40 不同：那是「可托管」口径
  （Maintainer 也能建 webhook）；本模块是「admin 终审」口径，仅 Owner/Admin 可确认。）
- GitHub: GET /repos/{owner}/{repo}/collaborators/{username}/permission
  admin 判定：role_name == "admin" 或 permission == "admin" 或 username == owner。

Fail-closed 原则：任何查询失败（网络/401/403/404/限流）一律返回 None（非 admin），
宁可拒绝合法 admin（其可稍后重试），绝不放行无权限用户。
"""
from src.config import log


def _split_repo_key(repo_key: str) -> tuple[str, str, str]:
    """拆 repo_key → (provider, owner, name)。无前缀默认 gitcode。"""
    from src.repo_key import parse_repo_key
    try:
        provider, owner, name = parse_repo_key(repo_key or "")
        if owner and name:
            return provider or "gitcode", owner, name
    except Exception:
        pass
    # 兜底：直接拆 owner/name
    raw = (repo_key or "").split(":", 1)[-1]
    parts = raw.split("/", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return "gitcode", parts[0], parts[1]
    return "", "", ""


def _get_gitcode_login(user: dict) -> str:
    """取用户绑定的 GitCode 用户名（external_identities.external_login，status=1）。"""
    try:
        from src.auth.grape_account_db import get_user_identities_with_tokens
        for ident in get_user_identities_with_tokens(user.get("id", 0) or 0):
            if ident.get("provider") == "gitcode":
                return ident.get("external_login", "") or ""
    except Exception as e:
        log.warning(f"repo_roles: get gitcode login failed (user={user.get('id')}): {e}")
    return ""


def _get_github_login(user: dict) -> str:
    """取用户绑定的 GitHub 用户名。"""
    try:
        from src.auth.grape_account_db import get_user_identities_with_tokens
        for ident in get_user_identities_with_tokens(user.get("id", 0) or 0):
            if ident.get("provider") == "github":
                return ident.get("external_login", "") or ""
    except Exception as e:
        log.warning(f"repo_roles: get github login failed (user={user.get('id')}): {e}")
    return ""


def _check_gitcode_admin(token: str, owner: str, repo: str, username: str) -> bool:
    """GitCode admin 判定（单次查询，异常向上抛由调用方 fail-closed）。"""
    import urllib.parse
    from src.user.repo_manager import _req
    url = (f"https://gitcode.com/api/v5/repos/{owner}/"
           f"{urllib.parse.quote(repo, safe='')}/collaborators/"
           f"{urllib.parse.quote(username, safe='')}/permission")
    data = _req("GET", url, token)
    try:
        access_level = int(data.get("access_level", 0) or 0)
    except (TypeError, ValueError):
        access_level = 0
    if access_level >= 50:  # 50=Admin（40=Maintainer 不满足终审口径）
        return True
    if str(data.get("permission", "") or "").lower() == "admin":
        return True
    perm = data.get("permissions") or {}
    return bool(perm.get("admin")) if isinstance(perm, dict) else False


def _check_github_admin(token: str, owner: str, repo: str, username: str) -> bool:
    """GitHub admin 判定。"""
    from src.user.repo_manager import get_github_collaborator_permission
    role = get_github_collaborator_permission(token, owner, repo, username)
    return str(role or "").lower() == "admin"


def is_repo_admin(user: dict, repo_key: str) -> bool:
    """判定用户是否为 repo_key 仓库的 admin（Owner/Admin）。

    fail-closed：未绑定平台、无 token、API 失败 → False。
    """
    return get_repo_admin_identity(user, repo_key) is not None


def get_repo_admin_identity(user: dict, repo_key: str) -> "dict | None":
    """返回 {username, source}（admin 时），非 admin / 判定失败返回 None。

    source: "owner"（仓库所有者本人） | "permission"（VCS 权限接口判定）。
    """
    provider, owner, repo = _split_repo_key(repo_key)
    if not owner or not repo or not user:
        return None

    if provider == "github":
        username = _get_github_login(user)
    else:
        username = _get_gitcode_login(user)
    if not username:
        return None

    # 仓库所有者本人 → admin（与 repos.host 的 owner 快速路径一致）
    if username == owner:
        return {"username": username, "source": "owner"}

    # 实时查 VCS permission（with_oauth_token：预刷新 + 401 重试一次）
    try:
        from src.api.oauth_token import with_oauth_token
        provider_name = "github" if provider == "github" else "gitcode"

        def _check(token, _owner=owner, _repo=repo, _u=username, _p=provider):
            if _p == "github":
                return _check_github_admin(token, _owner, _repo, _u)
            return _check_gitcode_admin(token, _owner, _repo, _u)

        if with_oauth_token(user, provider_name, _check):
            return {"username": username, "source": "permission"}
        return None
    except Exception as e:
        # fail-closed：401 重授权/403 业务无权限/网络异常 → 一律视为非 admin
        log.info(f"repo_roles: admin check failed for user={user.get('id')} "
                 f"repo={repo_key} (fail-closed): {e}")
        return None
