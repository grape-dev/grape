"""GitHub provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。

GitHub 无「PR 关联自动关 Issue」原生字段：related_issue_nums → body 追加 `Closes #N`；
auto_close_issues 无原生等价（Closes 即自动关闭），丢弃。
"""
from . import base

API_BASE = "https://api.github.com"


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
    line = " ".join(f"Closes #{n}" for n in nums)
    native["body"] = (body.rstrip() + "\n\n" + line).strip()


_QUERY_STATE = {"open": "open", "closed": "closed", "all": "all"}
_UPDATE_STATE = {"open": "open", "closed": "closed", "close": "closed"}

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
        "auto_close_issues": {"native": None},
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)