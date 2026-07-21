"""GitCode 仓库托管管理：获取管理员仓库、创建/删除 webhook"""
import json, urllib.request, urllib.error
from src.config import log, GITCODE_WEBHOOK_TOKEN

API_BASE = "https://api.gitcode.com/api/v5"
WEBHOOK_URL = "http://203.0.113.2:8080/webhook/gitcode"


def _req(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    """调用 GitCode API，返回 JSON 响应或抛出异常"""
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


def fetch_admin_repos(api_token: str) -> list[dict]:
    """获取用户有管理员权限的仓库列表。
    返回：[{"full_name": "owner/repo", "owner": "owner", "name": "repo"}, ...]
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/user/repos?type=all&per_page=100&page={page}&sort=full_name"
        data = _req("GET", url, api_token)
        log.info(f"fetch_admin_repos page {page}: type={type(data).__name__}, len={len(data) if isinstance(data, (list, dict)) else '?'}")
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
            # log first item keys for debugging
            if not repos and items.index(r) == 0:
                log.info(f"First repo keys: {list(r.keys())}, permission={r.get('permission')}, permissions={r.get('permissions')}, role={r.get('role')}")
            perm = r.get("permission") or r.get("permissions")
            if isinstance(perm, dict) and perm.get("admin"):
                owner, name = full_name.split("/", 1)
                repos.append({"full_name": full_name, "owner": owner, "name": name})
        if len(items) < 100:
            break
        page += 1
    log.info(f"fetch_admin_repos total: {len(repos)} repos")
    return repos


def setup_webhook(api_token: str, owner: str, repo: str) -> int:
    """在指定仓库上创建 DevOps Bot webhook，返回 webhook_id。"""
    url = f"{API_BASE}/repos/{owner}/{repo}/hooks"
    body = {
        "url": WEBHOOK_URL,
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
    url = f"{API_BASE}/repos/{owner}/{repo}/hooks/{webhook_id}"
    _req("DELETE", url, api_token)
    return True
