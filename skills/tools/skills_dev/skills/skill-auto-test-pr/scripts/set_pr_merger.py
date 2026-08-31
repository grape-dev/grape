#!/usr/bin/env python3
"""set_pr_merger.py — 设置 PR 合并人（内网 API，数字用户 id）

用法:
  python3 set_pr_merger.py <PR_URL>

环境变量:
  GITCODE_ACCESS_TOKEN  内网 API Bearer JWT（必需）
  GITCODE_TOKEN         v5 API PRIVATE-TOKEN（解析仓库数字 id，必需）
  GITCODE_PR_ASSIGNEE_ID  合并人数字用户 id（默认 11027048 = g30074593）

退出码: 0 成功; 1 参数/凭证缺失; 2 设置失败
"""
import os
import re
import sys
import json
import time
import base64
import urllib.request
import urllib.error

GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
WEB_API_BASE = "https://web-api.gitcode.com"
DEFAULT_MERGER_ID = "11027048"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

_PR_URL_RE = re.compile(
    r"^https://gitcode\.com/([^/]+)/([^/]+)/(?:pull|merge_requests)/(\d+)$"
)


def _decode_jwt_exp(token):
    """JWT exp（unix 秒）；解析失败返回 None。"""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp") or 0)
    except Exception:
        return None


def _refresh_access_token(access, refresh):
    """用 REFRESH token 刷新 ACCESS；成功返回新 token 并写回 /root/.gitcode_tokens。"""
    url = f"{WEB_API_BASE}/uc/api/v1/user/token/refresh?refresh_token={refresh}"
    req = urllib.request.Request(url, data=b"", method="POST", headers=_browser_headers(access))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    new_access = data.get("access_token") or ""
    if not new_access:
        return None
    new_refresh = data.get("refresh_token") or refresh
    _persist_tokens(new_access, new_refresh)
    return new_access


def _persist_tokens(access, refresh):
    path = os.getenv("GITCODE_TOKENS_FILE", "/root/.gitcode_tokens")
    try:
        lines = []
        if os.path.exists(path):
            lines = open(path, encoding="utf-8").read().splitlines()
        out, written = [], set()
        for l in lines:
            if l.startswith("GITCODE_ACCESS_TOKEN="):
                out.append(f"GITCODE_ACCESS_TOKEN={access}"); written.add("A")
            elif l.startswith("GITCODE_REFRESH_TOKEN="):
                out.append(f"GITCODE_REFRESH_TOKEN={refresh}"); written.add("R")
            else:
                out.append(l)
        if "A" not in written:
            out.append(f"GITCODE_ACCESS_TOKEN={access}")
        if "R" not in written:
            out.append(f"GITCODE_REFRESH_TOKEN={refresh}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        pass


def _browser_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-App-Channel": "gitcode-fe",
        "Origin": "https://gitcode.com",
        "Referer": "https://gitcode.com/",
        "User-Agent": _BROWSER_UA,
    }


def _http(method, url, headers, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def main():
    if len(sys.argv) < 2:
        print("用法: python3 set_pr_merger.py <PR_URL>", file=sys.stderr)
        return 1
    pr_url = sys.argv[1]
    m = _PR_URL_RE.match(pr_url)
    if not m:
        print(f"错误: 无法解析 PR URL: {pr_url}", file=sys.stderr)
        return 1
    owner, repo, pr_number = m.group(1), m.group(2), m.group(3)

    access = os.getenv("GITCODE_ACCESS_TOKEN", "")
    token = os.getenv("GITCODE_TOKEN", "")
    refresh = os.getenv("GITCODE_REFRESH_TOKEN", "")
    merger_id = os.getenv("GITCODE_PR_ASSIGNEE_ID", DEFAULT_MERGER_ID)
    if not token:
        print("错误: 缺少 GITCODE_TOKEN", file=sys.stderr)
        return 1

    def _load_disk_tokens():
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

    exp = _decode_jwt_exp(access) if access else 0
    valid = bool(access) and exp and exp - time.time() > 600

    # env ACCESS 过期/缺失 → 优先用磁盘（每日 cron 保活的新 token）兜底
    if not valid:
        disk = _load_disk_tokens()
        d_access = disk.get("GITCODE_ACCESS_TOKEN", "")
        d_exp = _decode_jwt_exp(d_access) if d_access else 0
        if d_access and d_exp and d_exp - time.time() > 600:
            access = d_access
            refresh = refresh or disk.get("GITCODE_REFRESH_TOKEN", "")
            valid = True
            os.environ["GITCODE_ACCESS_TOKEN"] = access
            print("   [已从磁盘加载新 ACCESS]")
        elif not access:
            access = d_access
            refresh = refresh or disk.get("GITCODE_REFRESH_TOKEN", "")

    # 仍不可用且能刷新 → 刷新（此时 ACCESS 可能是磁盘有效 token 或磁盘也过期）
    if not valid:
        if refresh:
            base = access  # 刷新 Bearer 用现有 access（磁盘 token 若有效则可成功）
            refreshed = _refresh_access_token(base, refresh)
            if refreshed:
                access = refreshed
                os.environ["GITCODE_ACCESS_TOKEN"] = refreshed
                print("   [已自动刷新 ACCESS]")
            else:
                print("   警告: ACCESS 刷新失败", file=sys.stderr)
    if not access:
        print("错误: 缺少 GITCODE_ACCESS_TOKEN", file=sys.stderr)
        return 1

    try:
        repo_data = _http("GET", f"{GITCODE_API_BASE}/repos/{owner}/{repo}",
                          {"PRIVATE-TOKEN": token})
        repo_id = repo_data.get("id")
        if not repo_id:
            print(f"错误: 无法解析仓库数字 id: {owner}/{repo}", file=sys.stderr)
            return 2
        url = (
            f"{WEB_API_BASE}/issuepr/api/v1/projects/{repo_id}"
            f"/isource/merge_requests/{pr_number}"
        )
        body = {"repoId": repo_id, "iid": int(pr_number), "assignee_ids": str(merger_id)}
        data = _http("PUT", url, _browser_headers(access), body)
        n = data.get("assignees_number", 0)
        if n > 0:
            print(f"✅ 合并人已设置: id {merger_id} (PR {pr_number})")
            return 0
        # 幂等：回读确认
        detail = _http("GET", url, _browser_headers(access))
        assigns = detail.get("assignees") or []
        if any(str(a.get("id")) == str(merger_id) for a in assigns):
            print(f"✅ 合并人已设置(幂等确认): id {merger_id} (PR {pr_number})")
            return 0
        print("⚠ 合并人未设置成功（回读未确认）", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as e:
        print(f"错误: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
