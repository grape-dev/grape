"""GitCode provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。"""
from . import base

API_BASE = "https://gitcode.com/api/v5"


def request(method, url, token, body=None):
    from src.user.repo_manager import _req
    return _req(method, url, token, body)


def bot_token(repo_key):
    from src.user.db import get_gitcode_token
    return get_gitcode_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_token_with_refresh
    return get_user_token_with_refresh(username, user_id=user_id)


def _gc_inner_nums(owner_repo, nums):
    return ",".join(f"{owner_repo}#{n}" for n in nums)


def _gc_related(v, neutral, native, ctx):
    native["inner_issue_nums"] = _gc_inner_nums(ctx.get("owner_repo", ""), v)


_QUERY_STATE = {"open": "opened", "closed": "closed", "all": "all"}
_UPDATE_STATE = {"open": "open", "closed": "close", "close": "close"}

_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "body",
        "assignee": "assignee",
    },
    ("issues", "update"): {
        "labels": "labels",
        "assignee": "assignee",
        "milestone_id": "milestone_id",
        "state": {"native": "state", "values": _UPDATE_STATE},
        "body": "body",
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "head",
        "base": "base",
        "body": "body",
        "related_issue_nums": {"native": "inner_issue_nums", "transform": _gc_related},
        "auto_close_issues": "close_related_issue",
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)