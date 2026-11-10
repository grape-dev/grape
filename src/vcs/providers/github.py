"""GitHub provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。

GitHub 无「PR 关联自动关 Issue」原生字段：related_issue_nums → body 追加 `Closes #N`；
auto_close_issues 无原生等价（Closes 即自动关闭），丢弃。
"""
import re

from . import base

API_BASE = "https://api.github.com"

# 调用方 body 自带的自动关闭引用（auto_close=false 时必须清除，避免绕过门禁）
_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*[:：]?\s*#(\d+)",
    re.IGNORECASE,
)


def _strip_closing_refs(body: str) -> str:
    _stripped = _CLOSING_REF_RE.sub("", body or "")
    return re.sub(r"\n{3,}", "\n\n", _stripped)


def request(method, url, token, body=None):
    from src.user.repo_manager import _github_req
    return _github_req(method, url, token, body)


def bot_token(repo_key):
    from src.user.db import get_github_token
    return get_github_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_github_token_with_refresh
    return get_user_github_token_with_refresh(username, user_id=user_id) or ""


def _assignee_list(v, neutral, native, ctx):
    native["assignees"] = [v] if isinstance(v, str) else (v or [])


def _gh_close_body(nums, neutral, native, ctx):
    body = neutral.get("body") or ""
    # Issue #1739：qa 失败时调用方会置 auto_close_issues=false，但 GitHub 无原生
    # 关闭开关、Closes #N 即自动关闭。此时必须降级为 Related 语义（仅关联不关闭），
    # 并清除调用方 body 自带的 Closes/Fixes/Resolves #N，否则经 VCS UI 合入仍会
    # 自动关闭 Issue，绕过合入门禁。
    if neutral.get("auto_close_issues") is False:
        body = _strip_closing_refs(body)
        line = " ".join(f"Related to #{n}" for n in nums)
    else:
        _existing = {str(n) for n in _CLOSING_REF_RE.findall(body)}
        line = " ".join(f"Closes #{n}" for n in nums if str(n) not in _existing)
    native["body"] = (body.rstrip() + "\n\n" + line).strip() if line else body


def _gh_auto_close(value, neutral, native, ctx):
    """auto_close=false 且无 related_issue_nums 时，仍需清除 body 自带的关闭引用。"""
    if value is False and "body" in native:
        native["body"] = _strip_closing_refs(native["body"]).rstrip()


_QUERY_STATE = {"open": "open", "closed": "closed", "all": "all"}
# Issue #707: 与 gitcode 通道对齐中性别名集合（reopen=设为 open；opened/close 为词形别名）
_UPDATE_STATE = {
    "open": "open", "opened": "open", "reopen": "open",
    "closed": "closed", "close": "closed",
}

_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "body",
        "assignee": {"native": "assignees", "transform": _assignee_list},
        "assignees": {"native": "assignees", "transform": _assignee_list},
    },
    ("issues", "update"): {
        "labels": "labels",
        "assignee": {"native": "assignees", "transform": _assignee_list},
        "assignees": {"native": "assignees", "transform": _assignee_list},
        "milestone_id": "milestone_id",
        "state": {"native": "state", "values": _UPDATE_STATE},
        "body": "body",
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "head",
        "base": "base",
        "body": "body",
        "related_issue_nums": {"native": None, "transform": _gh_close_body},
        "auto_close_issues": {"native": None, "transform": _gh_auto_close},
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)