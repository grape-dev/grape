"""S107 用例（D80）：G2 方言翻译模块补齐——gitee/gitlab 注册进 provider 注册表。

解耦红线验收：新平台接入 = 加卡 + 加翻译模块，业务代码零平台分支。
- 注册表从 2 → 4 平台（与卡集合对齐——探活/镜像/镜像关单自动纳管）；
- gitee 规则与 gitcode v5 同族 parity（update state reopen/close 等）；
- gitlab 偏离形态锁定：body→description、head/base→source/target_branch、
  跨平台不可映射字段 drop（assignee）、state→state_event；
- gitlab URL 形态：/projects/{owner}%2F{repo} + /pulls→/merge_requests（钩子消费）；
- 存量 gitcode/github 行为零变更（guard）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.domains.runtime.vcs.providers import _MODULES
from src.domains.runtime.vcs.providers import gitee as g, gitlab as gl, gitcode as gc


def test_s107_registry_matches_card_platforms():
    """注册表与平台卡集合对齐：4 平台全部可翻译（探活/镜像关单自动纳管的载体）。"""
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms
    assert set(_MODULES) == set(list_vcs_platforms())


def test_s107_gitee_v5_parity_with_gitcode():
    """gitee v5 与 gitcode v5 同族：update state 事件语义、create 字段一致
    （query state 上游值域不同，按各平台真实值域断言）。"""
    for (res, op), neutral, ctx in [
        (("issues", "update"), {"state": "open"}, {"owner_repo": "o/r"}),
        (("issues", "create"), {"title": "t", "body": "b", "assignee": "u"}, {"owner_repo": "o/r"}),
    ]:
        assert g.translate(res, op, dict(neutral), ctx) == gc.translate(res, op, dict(neutral), ctx), \
            f"gitee 与 gitcode v5 规则偏离: {res}/{op}"
    # 口径修正：query state 两端上游值域不同——Gitee v5 = open/progressing/closed
    # （Gitee 官方 issues 列表参数），GitCode/GitHub 系 = open(s)/closed。
    # gitee 折叠成 "opened" 会 405/422 空结果，此断言原为同族误判。
    assert g.query_state("open") == "open" and gc.query_state("open") == "opened"


def test_s107_gitee_drops_unsupported_pr_mechanism():
    """gitee PR create：close_related_issue 机制不存在 → drop（不发未知参数）。"""
    out = g.translate("pulls", "create",
                      {"title": "t", "head": "h", "base": "b",
                       "auto_close_issues": "1", "related_issue_nums": [1, 2]},
                      {"owner_repo": "o/r"})
    assert "auto_close_issues" not in out and "related_issue_nums" not in out
    assert out["head"] == "h" and out["base"] == "b"


def test_s107_gitlab_field_semantics():
    """gitlab 偏离形态：body→description、head/base→source/target、assignee drop、state_event。"""
    out = gl.translate("issues", "create",
                       {"title": "t", "body": "正文", "assignee": "someone"}, {})
    assert out == {"title": "t", "description": "正文"}  # assignee 用户名不可映射 → drop
    out = gl.translate("issues", "update", {"state": "closed"}, {})
    assert out == {"state_event": "close"}
    out = gl.translate("pulls", "create",
                       {"title": "t", "head": "feat", "base": "main", "body": "b"}, {})
    assert out == {"title": "t", "source_branch": "feat", "target_branch": "main", "description": "b"}


def test_s107_gitlab_url_shape():
    """gitlab URL 形态：/projects/{owner}%2F{repo} + /pulls→/merge_requests（钩子消费）。"""
    from src.api.git_repo_api import _vcs_url
    url = _vcs_url("acme", "web", "/issues", {"state": "opened"}, provider="gitlab")
    assert url.startswith("https://gitlab.com/api/v4/projects/acme%2Fweb/issues")
    assert "state=opened" in url
    url_pr = _vcs_url("acme", "web", "/pulls", None, provider="gitlab")
    assert "/merge_requests" in url_pr and "/pulls" not in url_pr
    # 存量形态零变更
    url_gc = _vcs_url("acme", "web", "/issues", None, provider="gitcode")
    assert url_gc == "https://gitcode.com/api/v5/repos/acme/web/issues"
    url_gh = _vcs_url("acme", "web", "/pulls", None, provider="github")
    assert url_gh == "https://api.github.com/repos/acme/web/pulls"


def test_s107_query_state_parity():
    """query_state 三态映射：按各平台上游真实值域（Gitee=open，GitLab=opened）。"""
    for st, expect in (("open", "open"), ("closed", "closed"), ("all", "all")):
        assert g.query_state(st) == expect
    for st, expect in (("open", "opened"), ("closed", "closed"), ("all", "all")):
        assert gl.query_state(st) == expect
