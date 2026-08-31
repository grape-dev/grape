#!/usr/bin/env python3
import os
import re
import urllib.request
import json
from typing import Optional

import pymysql

from .config import DB_CONFIG

GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
WEB_API_BASE = "https://web-api.gitcode.com"
GITCODE_TOKEN_ENV = "GITCODE_TOKEN"
GITCODE_ACCESS_TOKEN_ENV = "GITCODE_ACCESS_TOKEN"
MERGER_ID_ENV = "GITCODE_PR_ASSIGNEE_ID"
AUTO_MERGE_ENV = "AUTO_MERGE_PR"

# 合并人数字用户 id（内网 API 用数字 id，非 login）。默认 lccode(汪亮初) 的数字 id 11935679（内网 isource id）。
DEFAULT_MERGER_ID = "11935679"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

VALID_REPO_STATUSES = {"pending", "processing", "done", "failed", "skipped"}

_PR_URL_RE = re.compile(
    r"^https://gitcode\.com/([^/]+)/([^/]+)/(?:pull|merge_requests)/(\d+)$"
)


def _get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def parse_pr_url(pr_url: str) -> Optional[dict]:
    if not pr_url:
        return None
    m = _PR_URL_RE.match(pr_url)
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2), "pr_number": m.group(3)}


def is_auto_merge_enabled() -> bool:
    return os.getenv(AUTO_MERGE_ENV, "off").strip().lower() == "on"


def update_repo_status(issue_id: str, status: str, url: Optional[str] = None) -> bool:
    if status not in VALID_REPO_STATUSES:
        print(f"    Warning: invalid repo status '{status}', skipped")
        return False
    if not issue_id:
        return False
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE `t_skills_dev_flow` "
                    "SET node_repo_status = %s, node_repo_time = NOW(), node_repo_url = %s "
                    "WHERE multica_issue_id = %s",
                    (status, url, issue_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        print(f"    Warning: update_repo_status failed: {e}")
        return False


def _http_put(url: str, headers: dict, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _http_post(url: str, headers: dict, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _browser_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-App-Channel": "gitcode-fe",
        "Origin": "https://gitcode.com",
        "Referer": "https://gitcode.com/",
        "User-Agent": _BROWSER_UA,
    }


def _repo_numeric_id(owner: str, repo: str, token: str) -> Optional[int]:
    """仓库数字 id（内网 API 用，来自 v5 repo 详情）。"""
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}"
    data = _http_get(url, {"PRIVATE-TOKEN": token})
    return data.get("id") if isinstance(data, dict) else None


def assign_merger(pr_url: str, access_token: str, merger_id: Optional[str] = None) -> dict:
    """设置 PR 合并人（内网 API，用数字用户 id）。

    审查人(v5 /pulls/{n}/assignees, login) 与 合并人(内网 assignee_ids, 数字id)
    是两个不同字段；本函数只设置合并人。
    注意：重复设置同一合并人时接口幂等返回 assignees_number=0，
    需回读 GET 确认实际 assignees，避免误报未设置。
    """
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(f"cannot parse PR URL: {pr_url}")
    merger_id = (merger_id or os.getenv(MERGER_ID_ENV, DEFAULT_MERGER_ID)).strip()
    repo_id = _repo_numeric_id(
        parsed["owner"], parsed["repo"], os.getenv(GITCODE_TOKEN_ENV, "")
    )
    if not repo_id:
        raise ValueError(f"cannot resolve repo numeric id: {parsed['owner']}/{parsed['repo']}")
    url = (
        f"{WEB_API_BASE}/issuepr/api/v1/projects/{repo_id}"
        f"/isource/merge_requests/{parsed['pr_number']}"
    )
    headers = _browser_headers(access_token)
    body = {"repoId": repo_id, "iid": int(parsed["pr_number"]), "assignee_ids": str(merger_id)}
    try:
        data = _http_put(url, headers, body)
    except Exception as e:
        print(f"    Warning: assign_merger PUT failed: {e}")
        return {"assigned": False, "assignees_number": 0}
    n = data.get("assignees_number", 0)
    if n > 0:
        return {"assigned": True, "assignees_number": n}
    # 幂等：回读 GET 确认当前合并人
    try:
        detail = _http_get(url, headers)
        assigns = detail.get("assignees") or []
        ok = any(str(a.get("id")) == str(merger_id) for a in assigns)
        return {"assigned": ok, "assignees_number": len(assigns)}
    except Exception:
        return {"assigned": False, "assignees_number": n}


def merge_pr(pr_url: str, token: str) -> dict:
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(f"cannot parse PR URL: {pr_url}")
    url = (
        f"{GITCODE_API_BASE}/repos/{parsed['owner']}/{parsed['repo']}"
        f"/pulls/{parsed['pr_number']}/merge"
    )
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    body = {"merge_method": "merge", "prune_branch": True}
    data = _http_put(url, headers, body)
    html_url = data.get("html_url") or pr_url
    return {"merged": bool(data.get("merged")), "html_url": html_url}


def assign_pr(pr_url: str, token: str, assignee_login: str) -> dict:
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(f"cannot parse PR URL: {pr_url}")
    url = (
        f"{GITCODE_API_BASE}/repos/{parsed['owner']}/{parsed['repo']}"
        f"/pulls/{parsed['pr_number']}/assignees"
    )
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    body = {"assignees": assignee_login}
    data = _http_post(url, headers, body)
    assignees_number = data.get("assignees_number", 0)
    return {"assigned": assignees_number > 0, "assignees_number": assignees_number}
