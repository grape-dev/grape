"""GitHub provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。

GitHub 无「PR 关联自动关 Issue」原生字段：related_issue_nums → body 追加 `Closes #N`；
auto_close_issues 无原生等价（Closes 即自动关闭），丢弃。
"""
import re

from . import base

API_BASE = "https://api.github.com"

# D31/S63 凭据域口径（settings.py 读，LABEL_COLOR 同款模块契约开关）：本平台
# settings 表单 PAT 落 <p>_token 专用字段（与专用分支/前端存量口径一致）；
# 未声明该开关的平台走 <p>_pat 泛型凭据键。
SETTINGS_TOKEN_FIELD_SUFFIX = "token"

# PATCH /issues 的 labels 必须是数组（逗号串 422 实测）；v5 系平台接收逗号串。
LABELS_ARRAY_FORM = True

# - milestones due_on 上游只认 ISO 8601 datetime（纯日期 422 "not a valid
#   date-time"，2026-11-11 三平台实弹；ISO 时间戳 200）。与 v5 系的
#   MILESTONE_DUE_DATE_ONLY（gitcode）对称：代理层把纯日期补成 T00:00:00Z，
#   调用方无需记忆平台差异（平台差异收进开关，业务侧零感知）。
MILESTONE_DUE_TIME_ONLY = True

# 同 gitcode MILESTONE_VERIFY_READBACK：github 上游亦有 milestone 静默忽略
# 实证（B1，2026-11-13 P1-full 实弹：milestone_id 未映射上游 `milestone` 时
# 200 假成功）。写后 GET 回读校验按此开关派生。
MILESTONE_VERIFY_READBACK = True

# ── S89/D65 同族口径的 provider 模块契约开关（git_repo_api.py 消费）──

# GitHub closing keywords（fixes/closes/resolves #N）由上游自动关联关闭；
# auto_close_issues 缺省语义=true（v5 系上游无此行为，缺省 false）。
# b439764fd 原版经 native_field_for 派生，但 gitee 规则 auto_close_issues
# 为 native:None + _drop（无原生承载）——native_field_for 口径会给 gitee/gitlab
# 也注入 Closes，与已发布行为（仅 github 注入）不符；改模块开关收窄为零回归。
CLOSING_REFS_AUTO_CLOSE_DEFAULT = True

# 调用方 body 自带的自动关闭引用（auto_close=false 时必须清除，避免绕过门禁）
_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*[:：]?\s*#(\d+)",
    re.IGNORECASE,
)


def _strip_closing_refs(body: str) -> str:
    _stripped = _CLOSING_REF_RE.sub("", body or "")
    return re.sub(r"\n{3,}", "\n\n", _stripped)


def request(method, url, token, body=None):
    from src.domains.mgmt.user.repo_manager import _github_req
    return _github_req(method, url, token, body)


def bot_token(repo_key):
    from src.domains.mgmt.public import get_github_token  # 跨域走 owner public
    return get_github_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_token_for
    return get_user_token_for(username, user_id=user_id, provider="github") or ""


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
        # Issue #640 家族（2026-11-13 P1-full 实弹）：GitHub PATCH /issues 契约字段名
        # 为 `milestone`（int，里程碑 number）——同名直传 milestone_id 会被上游
        # 静默忽略（200 假成功、milestone 恒为 null）。与 gitcode/gitee 通道同口径。
        "milestone_id": "milestone",
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
        # GitHub 建 PR 无 prune_source_branch；不加此规则会原样透传成脏字段。
        # 等价能力是仓库级 delete_branch_on_merge，不在建 PR 时给。
        "prune_source_branch": {"native": None},
    },
}


def default_rule_fields(resource: str, op: str) -> set:
    """D105-14 改动点 C：github 缺省规则的中性字段集（模板构建期校验的合法域）。"""
    return set(_RULES.get((resource, op), {}).keys())


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)

# 托管全流程（bot 合作者 + webhook 建/删/探 + 库回写）已实装：
HOSTING_FULL_FLOW = True

# PR/MR 网页路径段（前端 PR 详情链接与 QA 门禁链接共用；能力位驱动，业务侧零平台判断）。
PR_WEB_PATH = "pull"


def repo_segment(owner: str, repo: str) -> str:
    """资源根片段（github 方言）：repos/{owner}/{repo}（URL 段编码）。"""
    import urllib.parse
    return (f"repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repo, safe='')}")
