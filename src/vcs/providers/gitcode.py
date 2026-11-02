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
# Issue #707: 上游 PATCH state 合法值为事件语义 reopen/close —— open/opened 为中性
# 别名（映射为 reopen），原 open→open 直传会触发上游 400 'state_event' 误导文案
_UPDATE_STATE = {
    "open": "reopen", "opened": "reopen", "reopen": "reopen",
    "closed": "close", "close": "close",
}

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
        # Issue #640: 上游 PATCH /repos/{owner}/issues/{number} 里程碑参数名为 milestone（int），
        # 不存在 milestone_id —— 同名直传会被静默忽略（200 假成功）
        "milestone_id": "milestone",
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