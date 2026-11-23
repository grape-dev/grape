"""GitLab provider 中性翻译（S107/G2：方言层补齐）。

GitLab API v4 与 v5 族的关键偏离（token_resolver 侧 GitLabProvider 已处理
同款形态，本模块保持一致）：
- 仓库资源路径 /projects/{owner}%2F{repo}（URL 编码全路径），非 /repos/...；
- 凭据走 PRIVATE-TOKEN header（卡 api_auth=private_token）；
- 正文语义 body → description；PR = merge_requests（head/base → source/target_branch）；
- 指派人 assignee_id 为 GitLab 用户 id（跨平台不可映射 → drop，不发）。
"""
from . import base
import json as _json
import urllib.request as _ur
import urllib.error as _ue
import urllib.parse as _up

# 上游异常类型走 owner 域 public
from src.domains.mgmt import public as _mgmt_pub
UpstreamAPIError = _mgmt_pub.UpstreamAPIError

API_BASE = "https://gitlab.com/api/v4"

# 网关子路径 → GitLab 原生路径（_vcs_url 消费）
PATH_MAP = {"/pulls": "/merge_requests"}


def repo_segment(owner: str, repo: str) -> str:
    """GitLab 仓库资源段：/projects/{owner}%2F{repo}（全路径 URL 编码）。"""
    return "projects/" + _up.quote(f"{owner}/{repo}", safe="")


def request(method, url, token, body=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    data = _json.dumps(body).encode() if body else None
    req = _ur.Request(url, data=data, headers=headers, method=method)
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return _json.loads(raw) if raw.strip() else {}
    except _ue.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise UpstreamAPIError(method, url, e.code, raw)
    except _ue.URLError as e:
        raise RuntimeError(f"Cannot reach GitLab API: {e.reason}")


def bot_token(repo_key):
    from src.domains.mgmt.public import get_repo_bot_token
    return get_repo_bot_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_token_for
    return get_user_token_for(username, user_id=user_id, provider="gitlab")


_QUERY_STATE = {"open": "opened", "closed": "closed", "all": "all"}
_UPDATE_STATE = {"open": "reopen", "opened": "reopen", "reopen": "reopen",
                 "closed": "close", "close": "close"}


def _drop(v, neutral, native, ctx):
    """显式丢弃（跨平台不可映射字段：assignee_id / related_issues 机制）。"""


_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "description",
        # assignee（用户名）跨平台不可映射为 GitLab assignee_id → drop
        "assignee": {"native": None, "transform": _drop},
    },
    ("issues", "update"): {
        "labels": "labels",
        "milestone_id": "milestone_id",
        "state": {"native": "state_event", "values": _UPDATE_STATE},
        "body": "description",
        "assignee": {"native": None, "transform": _drop},
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "source_branch",
        "base": "target_branch",
        "body": "description",
        "related_issue_nums": {"native": None, "transform": _drop},
        "auto_close_issues": {"native": None, "transform": _drop},
        # GitLab 建 MR 无 prune_source_branch；其等价能力是 MR 级 remove_source_branch
        # （本改动暂不映射，先丢弃避免脏字段）。
        "prune_source_branch": {"native": None, "transform": _drop},
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)

# PR/MR 网页路径段（前端 PR 详情链接与 QA 门禁链接共用；能力位驱动，业务侧零平台判断）。
PR_WEB_PATH = "merge_requests"


# ── 通道能力位（Issue #3632 后继：checks/status 通道不再按平台名分派）──
# GitLab v4 无 check-runs / combined status 通道 → 显式 404，不打别的平台。
CHECK_RUNS_NATIVE = False
STATUS_NATIVE = False
PR_ISSUE_LINK_NATIVE = False
# PR 评论 id 的路径不是扁平 /pulls/comments/{id}：GitLab 需要 MR 号
# （/merge_requests/{iid}/notes/{id}），故关闭扁平形态。
PR_COMMENT_FLAT_PATH = False


# ── 子路径重写（PATH_MAP 只映首段：/pulls→/merge_requests；尾段方言差异在此收口）──
# GitLab 的评论是 notes、MR 变更是 changes；不映会 404（A 链 VcsApiClient 已按 dialect
# 派生，透传面此前没有，导致"同一平台一处通一处不通"）。
import re as _re2

_PATH_REWRITES = (
    (_re2.compile(r"^/issues/([^/]+)/comments$"), r"/issues/\1/notes"),
    (_re2.compile(r"^/merge_requests/([^/]+)/comments$"), r"/merge_requests/\1/notes"),
    (_re2.compile(r"^/merge_requests/([^/]+)/comments/([^/]+)$"), r"/merge_requests/\1/notes/\2"),
    (_re2.compile(r"^/merge_requests/([^/]+)/files$"), r"/merge_requests/\1/changes"),
)


def rewrite_path(subpath: str) -> str:
    """把 github 方言子路径改写成 GitLab 资源路径（未命中则原样返回）。"""
    for rx, repl in _PATH_REWRITES:
        if rx.match(subpath or ""):
            return rx.sub(repl, subpath)
    return subpath
