"""GitCode 仓库托管管理：获取管理员仓库、创建/删除 webhook"""
import json, urllib.request, urllib.error, urllib.parse
from src.config import log, GITCODE_WEBHOOK_TOKEN

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
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{username}/permission"
    try:
        data = _req("GET", url, api_token)
        perm = data.get("permissions", {})
        return bool(perm.get("admin")) if isinstance(perm, dict) else False
    except RuntimeError:
        return False


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


def setup_webhook(api_token: str, owner: str, repo: str, webhook_url: str) -> int:
    """在指定仓库上创建 DevOps Bot webhook，返回 webhook_id。"""
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    body = {
        "url": webhook_url,
        "password": GITCODE_WEBHOOK_TOKEN,
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


def remove_webhook(api_token: str, owner: str, repo: str, webhook_id: int) -> bool:
    """删除指定仓库上的 webhook。"""
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks/{webhook_id}"
    _req("DELETE", url, api_token)
    return True
