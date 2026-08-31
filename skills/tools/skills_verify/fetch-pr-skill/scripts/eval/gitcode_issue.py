#!/usr/bin/env python3
import json
import os
import time
import urllib.request
from typing import Optional

from .report_uploader import build_report_content

GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
REPORT_ISSUE_LABEL = "评测参考"
WEB_API_BASE = "https://web-api.gitcode.com"
GITCODE_ACCESS_TOKEN_ENV = "GITCODE_ACCESS_TOKEN"
GITCODE_REFRESH_TOKEN_ENV = "GITCODE_REFRESH_TOKEN"
ACCESS_TOKEN_MIN_LIFETIME = 86400  # 剩余 <1 天自动刷新

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


def _http_json(method: str, url: str, headers: dict, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _decode_jwt_exp(token: str) -> Optional[int]:
    """从 JWT payload 解码 exp（unix 秒）。解析失败返回 None。"""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        import base64
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp") or 0)
    except Exception:
        return None


def _persist_tokens(access: str, refresh: Optional[str] = None) -> None:
    """把刷新后的 token 写回 /root/.gitcode_tokens（600），失败仅告警不影响流程。"""
    path = os.getenv("GITCODE_TOKENS_FILE", "/root/.gitcode_tokens")
    try:
        lines = []
        if os.path.exists(path):
            lines = open(path, encoding="utf-8").read().splitlines()
        out = []
        written = set()
        for l in lines:
            if l.startswith("GITCODE_ACCESS_TOKEN="):
                out.append(f"GITCODE_ACCESS_TOKEN={access}"); written.add("A")
            elif l.startswith("GITCODE_REFRESH_TOKEN="):
                if refresh:
                    out.append(f"GITCODE_REFRESH_TOKEN={refresh}"); written.add("R")
                else:
                    out.append(l)
            else:
                out.append(l)
        if "A" not in written:
            out.append(f"GITCODE_ACCESS_TOKEN={access}")
        if refresh and "R" not in written:
            out.append(f"GITCODE_REFRESH_TOKEN={refresh}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(path, 0o600)
    except Exception as e:
        print(f"    Warning: persist tokens failed: {e}")


def _refresh_access_token() -> Optional[str]:
    """用 GITCODE_REFRESH_TOKEN 刷新 access_token，成功返回新 token 并更新环境变量 + 落盘。"""
    refresh = os.getenv(GITCODE_REFRESH_TOKEN_ENV, "")
    access = os.getenv(GITCODE_ACCESS_TOKEN_ENV, "")
    if not refresh:
        print(f"    Warning: {GITCODE_REFRESH_TOKEN_ENV} not set, cannot refresh")
        return None
    url = f"{WEB_API_BASE}/uc/api/v1/user/token/refresh?refresh_token={refresh}"
    headers = _browser_headers(access)
    try:
        data = _http_json("POST", url, headers)
    except Exception as e:
        print(f"    Warning: token refresh failed: {e}")
        return None
    new_access = data.get("access_token") or ""
    if new_access:
        os.environ[GITCODE_ACCESS_TOKEN_ENV] = new_access
        new_refresh = data.get("refresh_token") or refresh
        if new_refresh != refresh:
            os.environ[GITCODE_REFRESH_TOKEN_ENV] = new_refresh
        _persist_tokens(new_access, new_refresh)
        return new_access
    print("    Warning: token refresh returned no access_token")
    return None


def _load_tokens_file() -> dict:
    """读取 /root/.gitcode_tokens（每日 cron 保活），返回 {KEY: value}。"""
    path = os.getenv("GITCODE_TOKENS_FILE", "/root/.gitcode_tokens")
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if v:
                out[k] = v
    except Exception:
        pass
    return out


def ensure_access_token() -> Optional[str]:
    """读取 GITCODE_ACCESS_TOKEN；缺失或临近过期（剩余 <1 天）则自动刷新。

    env 缺失/过期时，优先用磁盘 /root/.gitcode_tokens（每日 cron 保活的新 token）兜底，
    再尝试用磁盘 REFRESH 刷新。
    """
    token = os.getenv(GITCODE_ACCESS_TOKEN_ENV, "")
    refresh = os.getenv(GITCODE_REFRESH_TOKEN_ENV, "")

    def _exp_ok(t):
        e = _decode_jwt_exp(t)
        return bool(e and e > int(time.time()) + 300)

    if not _exp_ok(token):
        disk = _load_tokens_file()
        d_access = disk.get("GITCODE_ACCESS_TOKEN", "")
        if _exp_ok(d_access):
            os.environ[GITCODE_ACCESS_TOKEN_ENV] = d_access
            os.environ.setdefault(GITCODE_REFRESH_TOKEN_ENV, disk.get("GITCODE_REFRESH_TOKEN", ""))
            token = d_access
            return token
        refresh = refresh or disk.get("GITCODE_REFRESH_TOKEN", "")
        if refresh and not os.environ.get(GITCODE_REFRESH_TOKEN_ENV):
            os.environ[GITCODE_REFRESH_TOKEN_ENV] = refresh
        refreshed = _refresh_access_token()
        if refreshed:
            return refreshed
        print("    Warning: access token unavailable and refresh failed")
        return None

    exp = _decode_jwt_exp(token)
    if exp - int(time.time()) < ACCESS_TOKEN_MIN_LIFETIME:
        refreshed = _refresh_access_token()
        if refreshed:
            return refreshed
        print("    Warning: access token expired and refresh failed, using stale token")
    return token


def get_pr_creator_login(owner: str, repo: str, pr_number: str, token: str) -> Optional[str]:
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/issues"
    headers = {"PRIVATE-TOKEN": token}
    data = _http_json("GET", url, headers)
    if isinstance(data, list) and data:
        user = data[0].get("user") or {}
        return user.get("login") or None
    return None


def create_report_issue(owner: str, repo: str, title: str, body: str, token: str) -> Optional[int]:
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}/issues"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    data = _http_json("POST", url, headers, {"title": title, "body": body})
    number = data.get("number") or data.get("id")
    return int(number) if number else None


def add_issue_label(owner: str, repo: str, issue_number: int, label: str, token: str) -> bool:
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    _http_json("PATCH", url, headers, {"labels": label})
    return True


def close_issue(owner: str, repo: str, issue_number: int, token: str) -> bool:
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    _http_json("PATCH", url, headers, {"state": "close"})
    return True


def build_issue_body(skill_name, pr_url, eval_result, metrics, security_risk,
                     report_id, report_url, creator_login) -> str:
    lines = []
    if creator_login:
        lines.append(f"@{creator_login} 评测完成，请查看评测报告。")
        lines.append("")
    content = build_report_content(
        skill_name, pr_url, eval_result, metrics, security_risk, report_id, "",
    )
    lines.append(content)
    if report_url:
        lines.append("")
        lines.append(f"**报告URL**: {report_url}")
    return "\n".join(lines)


def _browser_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-App-Channel": "gitcode-fe",
        "Origin": "https://gitcode.com",
        "Referer": "https://gitcode.com/",
        "User-Agent": _BROWSER_UA,
    }


def get_repo_numeric_id(owner: str, repo: str, token: str) -> Optional[int]:
    url = f"{GITCODE_API_BASE}/repos/{owner}/{repo}"
    headers = {"PRIVATE-TOKEN": token}
    data = _http_json("GET", url, headers)
    return data.get("id") if isinstance(data, dict) else None


def resolve_assignee_id(project_id: int, login: str, access_token: str) -> Optional[int]:
    url = (
        f"{WEB_API_BASE}/issuepr/api/v1/issue/{project_id}/issues/"
        f"assignee_candidates?keyword={login}"
    )
    headers = _browser_headers(access_token)
    data = _http_json("GET", url, headers)
    for item in data.get("content", []) if isinstance(data, dict) else []:
        if item.get("username") == login:
            return item.get("id")
    return None


def set_issue_assignee(project_id: int, issue_iid: int, assignee_ids: list, access_token: str) -> bool:
    url = f"{WEB_API_BASE}/issuepr/api/v1/issue/{project_id}/issues/{issue_iid}"
    headers = _browser_headers(access_token)
    _http_json("PUT", url, headers, {"assignee_ids": assignee_ids})
    return True
