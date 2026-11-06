"""事件文件清单补齐（Issue #996）。

归一层优先复用 webhook payload 自带的 commits/files；缺失时（典型：GitCode
merge webhook 无 commits、GitHub PR closed 无 files）按事件类型调 provider
diff/files 接口兜底补齐，写入 payload.files，供 QA 增量复核与产物清理预检。

设计原则：
- payload 优先，API 兜底（减少额外调用与延迟）；
- 失败 fail-open：拿不到清单只告警，不阻断事件流水线；
- GitLab 当前无统一 git-repo provider，API 兜底跳过（payload 自带 changes 仍可用）。
"""
from __future__ import annotations

import base64

from src.config import log
from src.vcs.event_types import (
    FILE_EVENT_TYPES, normalize_files, has_file_changes,
)

_MAX_PAGES = 20


def _split_repo_key(repo_key: str) -> tuple[str, str, str]:
    """repo_key（gitcode:owner/repo / owner/repo）→ (provider, owner, repo)。"""
    raw = repo_key or ""
    provider = "gitcode"
    if ":" in raw:
        provider, raw = raw.split(":", 1)
    if "/" in raw:
        owner, repo = raw.split("/", 1)
        return provider, owner, repo
    return provider, "", raw


def _pr_number(payload: dict) -> str:
    pr = payload.get("pull_request") or payload.get("merge_request") or {}
    if isinstance(pr, dict):
        n = pr.get("number") or pr.get("iid") or ""
        if n:
            return str(n)
    oa = payload.get("object_attributes") or {}
    if isinstance(oa, dict):
        n = oa.get("iid") or oa.get("number") or ""
        if n:
            return str(n)
    return str(payload.get("pr_number") or "")


def fetch_files_via_api(source: str, payload: dict, repo_key: str, token: str,
                        provider: str = "") -> list:
    """调 provider API 取事件变更文件原始列表（供 normalize_files 归一）。

    - PR 事件：/pulls/{number}/files 全量翻页（与 merge gate 同源能力）；
    - push 事件：优先 /commits/{after}，否则 /compare/{before}...{after}。
    """
    from src.api.git_repo_api import _vcs_req, _vcs_url

    p = provider or _split_repo_key(repo_key)[0]
    if p not in ("gitcode", "github"):
        return []
    _, owner, repo = _split_repo_key(repo_key)
    if not owner or not repo:
        return []

    pr_num = _pr_number(payload)
    if pr_num:
        files: list = []
        for page in range(1, _MAX_PAGES + 1):
            part = _vcs_req(p, "GET",
                            _vcs_url(owner, repo, f"/pulls/{pr_num}/files",
                                     {"per_page": 100, "page": page}, p), token)
            if not isinstance(part, list) or not part:
                break
            files.extend(part)
            if len(part) < 100:
                break
        return files

    after = str(payload.get("after") or payload.get("head") or "")
    before = str(payload.get("before") or "")
    if before and after and not set(before) == {"0"}:
        cmp = _vcs_req(p, "GET",
                       _vcs_url(owner, repo, f"/compare/{before}...{after}", None, p),
                       token)
        if isinstance(cmp, dict):
            return cmp.get("files") or []
    if after:
        detail = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/commits/{after}", None, p), token)
        if isinstance(detail, dict):
            return detail.get("files") or []
    return []


def enrich_event_files(evt: dict, repo_key: str, token: str = "",
                       fetcher=None) -> dict:
    """确保变更类事件的 payload.files 存在（payload 优先，API 兜底）。

    fetcher 注入用于测试：fetcher(source, payload, repo_key) -> 原始 files/commits。
    返回已归一化的 files dict（无可用文件清单时返回空结构）。
    """
    evt_type = evt.get("type") or ""
    if evt_type not in FILE_EVENT_TYPES:
        return {}
    payload = evt.get("payload")
    if not isinstance(payload, dict):
        return {}

    current = normalize_files(payload)
    if has_file_changes(current):
        payload["files"] = current
        return current

    try:
        if fetcher is not None:
            raw = fetcher(evt.get("source") or "", payload, repo_key)
        else:
            if not token:
                try:
                    from src.user.db import get_repo_token_with_refresh
                    token = get_repo_token_with_refresh(repo_key) or ""
                except Exception:
                    token = ""
            if not token:
                return normalize_files({})
            raw = fetch_files_via_api(evt.get("source") or "", payload, repo_key, token)
    except Exception as e:
        log.warning(f"files enrich: fetch failed type={evt_type} repo={repo_key}: {e}")
        return normalize_files({})

    files = normalize_files(raw)
    if has_file_changes(files):
        payload["files"] = files
        log.info(f"files enrich: type={evt_type} repo={repo_key} "
                 f"added={len(files['added'])} modified={len(files['modified'])} "
                 f"deleted={len(files['deleted'])} truncated={files.get('truncated')}")
    return files