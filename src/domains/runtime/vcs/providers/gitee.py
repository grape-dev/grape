"""Gitee provider 中性翻译（S107/G2：方言层补齐——此前 gitee 有卡无嘴）。

Gitee v5 与 GitCode v5 同族（github 方言），差异点：
- 凭据走 query 参数 access_token（v5 标准，非 Bearer header）；
- bot 凭据走 rc 单表通用链（get_repo_bot_token，#343 跳 session 级）；
- 用户凭据走统一 get_user_token_for（provider=gitee）；
- PR create 无 close_related_issue 机制（drop，不发）。
"""
from . import base
import json as _json
import re as _re
import urllib.request as _ur
import urllib.error as _ue
import urllib.parse as _up

from src.domains.mgmt.user.repo_manager import UpstreamAPIError

API_BASE = "https://gitee.com/api/v5"

# 上游契约适配开关（代理层 git_repo_api 读取；2026-11-11 部署实弹实测）：
# - issues update 官方契约是 PATCH /repos/{owner}/issues/{number}（路径无 repo 段，
#   body 必带 repo；v5 SDK 文档明载）。repo 路径形态上游对 IK* ident 恒 404
#   "project or enterprise"（form/JSON 双体、qt=ident 与否均 404）——此前误判为
#   "平台限制"，实为集成路径错误（参见 imjoey/ge PR !45 同案）。
ISSUE_UPDATE_OWNER_PATH = True
# ⑥ body.repo 纯仓库名：owner/repo 全称 404 "Not Found Project"（2026-11-11
# 实测对照）；gitcode 上游两种形态均收（历史行为保持全称）。代理层 getattr
# 派生（无平台枚举分支，test_guard_no_new_platform_branches 抽象不变量）。
ISSUE_BODY_REPO_BARE = True
# ⑦ milestones create 上游强制 due_on（2026-11-11 三平台矩阵实弹：空 due_on →
#   400 "due_on is missing/empty"，补后 200；日期形态宽容，无需 DATE_ONLY 开关）。
#   代理层入口拦截给可读 400；GitHub 方言无此约束。
MILESTONE_DUE_ON_REQUIRED = True


def _redact_url(url: str) -> str:
    """日志/异常 URL 脱敏：access_token 是 query 凭据，不能进日志（N1）。

    UpstreamAPIError 的 str() 与 _vcs_req 的 log.warning 都会渲染完整 URL，
    这里在 raise 前把凭据参数值替换为 ***。
    """
    return _re.sub(r"([?&]access_token=)[^&]+", r"\1***", url)


def request(method, url, token, body=None):
    """v5 查询参数鉴权（access_token=query），透传上游真实状态码。"""
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={_up.quote(token, safe='')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    data = _json.dumps(body).encode() if body else None
    req = _ur.Request(url, data=data, headers=headers, method=method)
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return _json.loads(raw) if raw.strip() else {}
    except _ue.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise UpstreamAPIError(method, _redact_url(url), e.code, raw)
    except _ue.URLError as e:
        raise RuntimeError(f"Cannot reach Gitee API: {e.reason}")


def bot_token(repo_key):
    # S82：rc 单表唯一权威（bot 列→全局→用户降级，跳 session 级，平台通用）
    from src.domains.mgmt.user.db import get_repo_bot_token
    return get_repo_bot_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_token_for
    return get_user_token_for(username, user_id=user_id, provider="gitee")


# 实弹验证 2026-11-10（部署实例 livefire）：gitee v5 只认 open/closed/all/progressing，
# "opened" 是 gitlab 值——GET /repos/{o}/{r}/issues?state=opened 返回 400 "state does not
# have a valid value"，state=open 返回 200。此映射此前抄自 gitlab 模块。
_QUERY_STATE = {"open": "open", "closed": "closed", "all": "all"}
_UPDATE_STATE = {"open": "reopen", "opened": "reopen", "reopen": "reopen",
                 "closed": "close", "close": "close"}


def _drop(v, neutral, native, ctx):
    """显式丢弃中性字段（apply_rules 无 drop 规则形态，用 no-op transform 实现）。"""


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
        "milestone_id": "milestone",
        "state": {"native": "state", "values": _UPDATE_STATE},
        "body": "body",
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "head",
        "base": "base",
        "body": "body",
        "related_issue_nums": {"native": None, "transform": _drop},
        "auto_close_issues": {"native": None, "transform": _drop},
        # Gitee 建 PR 无 prune_source_branch；不加规则会原样透传成脏字段。
        "prune_source_branch": {"native": None, "transform": _drop},
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)
