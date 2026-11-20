"""GitLab v4 webhook 钩子三件套（唯一实现源）。

Issue #3580 复审：托管流程（mgmt 域 repo_manager）与 provider（kernel 层
GitLabProvider）此前各自实现了一遍 GitLab hooks 调用，两版在事件位、密钥
语义、404 处理上出现分叉（其中一版把 PAT 当 webhook 密钥）。本模块是唯一
实现：两侧都只做薄委托。

约定：
- `token` 一律 PRIVATE-TOKEN 头；`api_base` 由调用方从平台卡派生（支持自建实例）；
- 只接受 https api_base（由平台卡校验保证），超时 30s，错误只带状态码不回声凭据；
- webhook 的 `token` 只能是共享密钥（调用方保证），本模块不做任何 token 回落。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class GitLabHookError(RuntimeError):
    """GitLab hooks 调用失败（携带 status_code；str() 含 "(<code>)" 以兼容既有解析）。"""

    def __init__(self, method: str, url: str, status_code: int, message: str = ""):
        self.method = method
        self.url = url
        self.status_code = int(status_code)
        self.message = message or ""
        super().__init__(f"GitLab API {method} {url} failed ({self.status_code})")


#: 托管 webhook 需要的事件位（与 GitLab hooks API 原生字段一致）
WEBHOOK_EVENTS = {
    "push_events": True,
    "tag_push_events": True,
    "issues_events": True,
    "note_events": True,
    "merge_requests_events": True,
    "pipeline_events": True,
    "wiki_page_events": True,
    "releases_events": True,
}


def project_path(owner: str, repo: str) -> str:
    """GitLab 项目标识：owner/repo 全路径 URL 编码（多级组原样保留）。"""
    return urllib.parse.quote(f"{owner}/{repo}", safe="")


def _request(method: str, url: str, token: str = "", body: dict | None = None,
             timeout: int = 30):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        # 不回显请求头/凭据：只带状态码与截断后的响应体
        detail = e.read().decode(errors="replace")[:500]
        raise GitLabHookError(method, url, e.code, detail)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitLab API: {e.reason}") from None


def _hooks_url(api_base: str, owner: str, repo: str) -> str:
    return f"{(api_base or '').rstrip('/')}/projects/{project_path(owner, repo)}/hooks"


def find_hook(api_base: str, token: str, owner: str, repo: str, webhook_url: str) -> "int | None":
    """查已有同 URL 的 hook id（分页取全）；查不到/查询失败返回 None。

    查询失败**必须**由调用方记日志（返回 None 会让调用方新建钩子）。
    """
    if not webhook_url:
        return None
    page = 1
    while True:
        hooks = _request("GET", f"{_hooks_url(api_base, owner, repo)}?per_page=100&page={page}", token)
        if not isinstance(hooks, list):
            return None
        for h in hooks:
            if isinstance(h, dict) and h.get("url") == webhook_url:
                return h.get("id")
        if len(hooks) < 100:
            return None
        page += 1


def setup_hook(api_base: str, token: str, owner: str, repo: str, webhook_url: str,
               secret: str) -> int:
    """建钩；`secret` 必填且**不得**复用 API token（GitLab 只有共享密钥语义）。"""
    secret = str(secret or "").strip()
    if not secret:
        raise GitLabHookError("POST", _hooks_url(api_base, owner, repo), 0,
                              "webhook secret required (never reuse api_token)")
    body = {"url": webhook_url, "token": secret, **WEBHOOK_EVENTS}
    result = _request("POST", _hooks_url(api_base, owner, repo), token, body)
    hook_id = int((result or {}).get("id", 0) or 0)
    if not hook_id:
        raise RuntimeError(
            f"Created GitLab webhook but got no id: {json.dumps(result, ensure_ascii=False)[:200]}")
    return hook_id


def update_hook_secret(api_base: str, token: str, owner: str, repo: str, hook_id: int,
                       webhook_url: str, secret: str) -> int:
    """原地更新已有钩子（改 URL/密钥/事件位），不先删（避免窗口期丢投递）。"""
    secret = str(secret or "").strip()
    if not secret:
        raise GitLabHookError("PUT", _hooks_url(api_base, owner, repo), 0,
                              "webhook secret required (never reuse api_token)")
    body = {"url": webhook_url, "token": secret, **WEBHOOK_EVENTS}
    result = _request("PUT", f"{_hooks_url(api_base, owner, repo)}/{int(hook_id)}", token, body)
    return int((result or {}).get("id", hook_id) or hook_id)


def remove_hook(api_base: str, token: str, owner: str, repo: str, hook_id: int) -> bool:
    """删钩；上游 404 视为幂等成功。"""
    url = f"{_hooks_url(api_base, owner, repo)}/{int(hook_id)}"
    try:
        _request("DELETE", url, token)
    except GitLabHookError as e:
        if e.status_code == 404:
            return True
        raise
    return True