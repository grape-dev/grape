"""Issue #4335：issues update 传 body 被 409 一致性断言误拦（主语判定误伤）。

主语收窄为「开头行 / markdown 标题行」；正文段落内 Issue #N 样例引用（含「」内
错误消息引用）一律白名单。update 侧与评论侧共用 `first_issue_anchor` 同一提取器。

验收：
1. 样例引用正文：update 传 body / 评论写入均不再 409（真实反馈正文回填可落地）；
2. 真实主语不一致（开头行 / 标题行指向其他 Issue）仍被 409 拦截，语义不放宽；
3. 不传 body（labels/assignee-only）行为不变；
4. 同族 #4329/#4257/#2225 防线（样例前缀/文件名、链路引用、引用行）不回归。
"""
from src.domains.runtime.vcs import release_guard

# 真实反馈正文样本（本单同族）：首行是标题、样例 Issue 编号出现在正文段落与
# 「」引用的错误消息里——旧实现取首个命中即误判主语为 #3888。
_FEEDBACK_BODY = """## 用户反馈
- 后台 id：14117
- 反馈标题：[Agent feedback] issues update 回填原始 body 被 409 拦截
- 原文：按 runbook Step 4 用 git-repo issues update 回填原始 body+labels 时，服务端返回 409「正文主语为 Issue #3888，与当前任务 Issue #4334 不一致」——原描述本身以实例 Issue 编号为主题，主语判定误伤。
- 正文内 issueNNN_*、Issue #3888 等样例引用进白名单不计入。

## 真实问题
守卫把描述文字里的样例 Issue #3888 判定为「正文主语」，导致合法回填被 409 中止。
"""


# ---------- 验收①：样例引用放行（update 侧 + 评论侧共用提取器） ----------

def test_feedback_body_has_no_subject_anchor():
    assert release_guard.first_issue_anchor(_FEEDBACK_BODY) is None


def test_update_with_sample_refs_body_passes():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=4335, body=_FEEDBACK_BODY,
        operation="issues update #4335")
    assert ok, msg


def test_comment_with_sample_refs_body_passes():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=4335, body=_FEEDBACK_BODY,
        operation="issues comment #4335")
    assert ok, msg


def test_prose_sample_reference_not_subject():
    """正文段落内的样例引用（非开头行/非标题行）不作主语。"""
    assert release_guard.first_issue_anchor("## 复现\n对 Issue #3888 执行 update 被拦截") is None
    assert release_guard.first_issue_anchor(
        "修复守卫。\n\n正文段落引用 Issue #2085 仅作样例说明") is None


# ---------- 验收②：真实主语不一致仍 409 拦截 ----------

def test_opening_line_foreign_subject_still_blocks():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=4335,
        body="Issue #9999 — 整篇正文来自另一个会话",
        operation="issues update #4335")
    assert not ok


def test_heading_line_foreign_subject_still_blocks():
    for body in (
        "# Spec: Issue #9999 — 另一个会话的正文",
        "## Issue #9999 修复方案\n\n整篇正文围绕他号展开",
    ):
        ok, _ = release_guard.check_issue_consistency(
            task_issue=4335, target_issue=4335, body=body,
            operation="issues update #4335")
        assert not ok, body


def test_matching_opening_subject_passes():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=4335,
        body="Issue #4335：修复主语判定误伤。\n\n参考 Issue #2080 的实测。",
        operation="issues update #4335")
    assert ok, msg


# ---------- 验收③：不传 body 行为不变 ----------

def test_metadata_only_update_on_other_issue_unchanged():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=3888, body="", operation="issues update #3888")
    assert ok


def test_body_write_to_other_target_still_blocks():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=4335, target_issue=3888, body="普通正文",
        operation="issues update #3888")
    assert not ok


# ---------- 验收④：同族防线不回归（4329 / 4257 / 2225） ----------

def test_sample_prefix_and_filenames_whitelisted():
    """#4329：样例前缀/测试文件名（词元/路径边界）不作主语。"""
    assert release_guard.first_issue_anchor("复现样例：issue456_* 前缀被误判") is None
    assert release_guard.first_issue_anchor(
        "回归 tests/runtime/test_issue_2225_release_guard.py") is None
    assert release_guard.first_issue_anchor("样例 Issue 456_* 不算主语") is None
    assert release_guard.first_issue_anchor("评审 PR #83，commit #123") is None


def test_quote_lines_and_chain_refs_whitelisted():
    """> 引用行是他号正文摘录；链路引用是事件序号，均不作主语。"""
    assert release_guard.first_issue_anchor("> Issue #2403 only quote") is None
    assert release_guard.first_issue_anchor("vod#25105 → issue#25107 → coding#25108") is None
    assert release_guard.first_issue_anchor(
        "> Issue #2085 引用行\nIssue #4335 正文主语") == 4335
