"""GitCode 仓库托管管理：获取管理员仓库、创建/删除 webhook"""
import json, urllib.request, urllib.error, urllib.parse
from src.config import log

API_BASE = "https://api.gitcode.com/api/v5"


def _req(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            if raw.strip():
                return json.loads(raw)
            return {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"GitCode API {method} {url} failed ({e.code}): {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")


def _check_collaborator_admin(api_token: str, owner: str, repo: str, username: str) -> bool:
    """调 /collaborators/{username}/permission 接口精确判断管理员权限"""
    if not username:
        return False
    # 项目所有者直接有完全权限
    if username == owner:
        return True
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{username}/permission"
    try:
        data = _req("GET", url, api_token)
        perm = data.get("permissions", {})
        return bool(perm.get("admin")) if isinstance(perm, dict) else False
    except RuntimeError:
        return False


# GitCode 成员权限枚举（官方 API：PUT /collaborators/{username} body 用单数字段 permission）
# 参与者→pull，开发者→push，仓库维护者→admin，自定义角色→customized
_PERMISSION_TO_GITCODE = {
    "maintainer": "admin",
    "admin": "admin",
    "developer": "push",
    "push": "push",
    "member": "pull",
    "reporter": "pull",
}


def add_collaborator(api_token: str, owner: str, repo: str, username: str,
                     permission: str = "maintainer") -> bool:
    """将用户添加为仓库协作者（GitCode 邀请制：创建邀请，需对方接受）。

    注意：
      - api_token 必须是仓库所有者/管理员的 token（GitCode 拒绝用 bot 自己 token 邀请自己）。
      - GitCode PUT body 字段名是单数 `permission`，枚举值：pull/push/admin/customized。
      - 已存在成员 / 已被邀请（400"已是成员"）视为成功（幂等）。
      - 不再静默降级：maintainer 邀请失败直接返回 False，由调用方决定是否拒绝托管。
    """
    if not username or not api_token:
        return False
    gitcode_perm = _PERMISSION_TO_GITCODE.get((permission or "").lower(), permission)
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _req("PUT", url, api_token, {"permission": gitcode_perm})
        return True
    except RuntimeError as e:
        # 已是成员 / 已被邀请（幂等成功）：说明该用户已在仓库的成员或邀请列表中
        if any(k in str(e) for k in ("已经是项目中的成员", "已经是", "已存在", "已被邀请", "已邀请", "已经邀请")):
            return True
        log.warning(f"add_collaborator failed: {e}")
        return False


def remove_collaborator(api_token: str, owner: str, repo: str, username: str) -> bool:
    """将用户从仓库协作者移除。返回是否成功（用户不在时视为成功）。

    注意：api_token 必须是仓库所有者/管理员的 token。
    """
    if not username or not api_token:
        return False
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _req("DELETE", url, api_token)
        return True
    except RuntimeError as e:
        # 404（用户本就不是协作者）视为成功
        if "404" in str(e):
            return True
        return False


def _req_query(method: str, path: str, token: str, params: dict | None = None,
               body: dict | None = None) -> dict:
    """GitCode 定制化接口请求：凭据用 query 参数 access_token（非 Bearer header）。

    用于 /api/v5/user/invite 系列接口（仅全局 bot grape_bot 可调用）。
    """
    if params is None:
        params = {}
    params["access_token"] = token
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{query}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"GitCode API {method} {path} failed ({e.code}): {raw}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")


def list_user_invites(token: str, page: int = 1, per_page: int = 100) -> list[dict]:
    """查询当前 token 用户的项目邀请列表（GET /api/v5/user/invite）。

    返回 content 列表，每项含 source_name(owner/repo)、invitation_token、
    user_role、inviter_username、expires_at 等。
    """
    data = _req_query("GET", "/user/invite", token,
                      {"page": page, "per_page": min(per_page, 100)})
    content = data.get("content", []) if isinstance(data, dict) else []
    return content if isinstance(content, list) else []


def accept_project_invite(token: str, invitation_token: str) -> bool:
    """接受项目邀请（POST /api/v5/user/invite/accept）。"""
    if not token or not invitation_token:
        return False
    try:
        _req_query("POST", "/user/invite/accept", token,
                   {"invitation_token": invitation_token})
        return True
    except RuntimeError as e:
        log.warning(f"accept_project_invite failed: {e}")
        return False


def reject_project_invite(token: str, invitation_token: str) -> bool:
    """拒绝项目邀请（POST /api/v5/user/invite/reject）。"""
    if not token or not invitation_token:
        return False
    try:
        _req_query("POST", "/user/invite/reject", token,
                   {"invitation_token": invitation_token})
        return True
    except RuntimeError as e:
        log.warning(f"reject_project_invite failed: {e}")
        return False


def accept_invite_for_repo(token: str, owner: str, repo: str) -> bool:
    """查询邀请列表，找到匹配 owner/repo 的邀请并接受。返回是否成功。

    用于全局 bot（grape_bot）被邀请加入托管仓库后自动同意。
    """
    if not token or not owner or not repo:
        return False
    try:
        invites = list_user_invites(token, per_page=100)
    except RuntimeError as e:
        log.warning(f"accept_invite_for_repo: list invites failed: {e}")
        return False
    target = f"{owner}/{repo}"
    for inv in invites:
        source = inv.get("source_name", "") or ""
        if source.lower() == target.lower():
            inv_token = inv.get("invitation_token", "")
            if inv_token:
                return accept_project_invite(token, inv_token)
    log.info(f"accept_invite_for_repo: no matching invite for {target} (total={len(invites)})")
    return False


def get_member_permission(api_token: str, owner: str, repo: str, username: str) -> dict | None:
    """查看仓库成员权限详情（GET /collaborators/{username}/permission）。

    返回含 access_level（30=Developer，40=Maintainer）、role_name、permission 等，失败返回 None。
    """
    if not username or not api_token:
        return None
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
    try:
        return _req("GET", url, api_token)
    except RuntimeError as e:
        log.warning(f"get_member_permission failed: {e}")
        return None


def fetch_repos_with_role(api_token: str) -> list[dict]:
    """获取用户的项目成员仓库，标注 is_admin。
    仓库所有者 | 组织管理员 → is_admin=True
    维护者 → is_admin=False
    返回：[{"full_name": "owner/repo", "owner": "owner", "name": "repo", "is_admin": bool}, ...]
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")
    # 获取当前用户身份
    current_user = ""
    try:
        user_data = _req("GET", f"{API_BASE}/user", api_token)
        current_user = user_data.get("login", "")
    except RuntimeError:
        pass
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/user/repos?type=all&per_page=100&page={page}&sort=full_name"
        data = _req("GET", url, api_token)
        log.info(f"fetch_repos page {page}: type={type(data).__name__}, len={len(data) if isinstance(data, (list, dict)) else '?'}")
        if not data:
            break
        items = data if isinstance(data, list) else []
        if not items and isinstance(data, dict):
            for key in ("data", "list", "items", "content"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
        for r in items:
            full_name = r.get("full_name", "")
            if not full_name or "/" not in full_name:
                continue
            if not repos and items.index(r) == 0:
                log.info(f"First repo: {r.get('full_name')}, owner={r.get('owner')}")
            owner_obj = r.get("owner", {})
            owner_login = owner_obj.get("login", "") if isinstance(owner_obj, dict) else ""
            release_owner = full_name.split("/", 1)[0]
            if current_user and owner_login and current_user == owner_login:
                is_admin = True
            else:
                is_admin = _check_collaborator_admin(api_token, release_owner, r.get("name", ""), current_user)
            repos.append({"full_name": full_name, "owner": release_owner, "name": r.get("name", ""), "is_admin": is_admin})
        if len(items) < 100:
            break
        page += 1
    log.info(f"fetch_repos total: {len(repos)} repos (current_user={current_user})")
    return repos


def find_existing_webhook(api_token: str, owner: str, repo: str, webhook_url: str) -> int | None:
    """检查仓库是否已有指向 webhook_url 的 webhook，有则返回 webhook_id"""
    if not webhook_url:
        return None
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    try:
        hooks = _req("GET", url, api_token)
        if isinstance(hooks, list):
            for h in hooks:
                if h.get("url") == webhook_url:
                    return h.get("id")
    except RuntimeError:
        pass
    return None


def setup_webhook(api_token: str, owner: str, repo: str, webhook_url: str, webhook_password: str = "") -> int:
    """在指定仓库上创建 DevOps Bot webhook，返回 webhook_id。"""
    if not webhook_password:
        from src.user.db import get_bot_token_by_repo
        webhook_password = get_bot_token_by_repo(owner, repo) or api_token
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    body = {
        "url": webhook_url,
        "password": webhook_password,
        "push_events": True,
        "issues_events": True,
        "merge_requests_events": True,
        "note_events": True,
        "tag_push_events": True,
        "build_events": True,
        "project_events": True,
        "wiki_events": True,
        "release_events": True,
    }
    result = _req("POST", url, api_token, body)
    hook_id = result.get("id", 0)
    if not hook_id:
        raise RuntimeError(f"Created webhook but got no id: {json.dumps(result, ensure_ascii=False)[:200]}")
    return hook_id


def _get_managed_webhook(user_id: int, owner: str, repo: str) -> int | None:
    """从 managed_repos 表获取 webhook_id"""
    import sqlite3
    db_path = None
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT webhook_id FROM managed_repos WHERE user_id=? AND repo_owner=? AND repo_name=?",
                      (user_id, owner, repo))
            row = c.fetchone()
            return row[0] if row and row[0] else None
    except:
        return None


def remove_webhook(api_token: str, owner: str, repo: str, webhook_id: int) -> bool:
    """删除指定仓库上的 webhook。"""
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks/{webhook_id}"
    _req("DELETE", url, api_token)
    return True
