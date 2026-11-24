"""Issue #4329：主语锚点词元/路径边界——样例前缀/测试文件名不再误判为正文主语。

四类验收：
1. 样例前缀（`issue456_*` 等标识符形态）放行；
2. `test_issue<数字>_*.py` 类测试文件名/路径片段放行；
3. 正文显式引用其它 Issue（`Issue #N` / `Issue N`）仍 409 拦截；
4. 既有主语语义与链路引用（同域 4257 单）不回归。
"""
from src.domains.runtime.vcs import release_guard


def test_sample_prefix_identifiers_are_not_subject():
    """验收①：样例前缀/紧邻标识符不作主语（不 409）。"""
    assert release_guard.first_issue_anchor("复现样例：issue456_* 前缀被误判") is None
    assert release_guard.first_issue_anchor("样例 issue456_knowledge 与 issue190_x") is None
    assert release_guard.first_issue_anchor("issue#2085 链路引用") is None
    assert release_guard.first_issue_anchor("Issue#2085 链路引用") is None


def test_sample_prefix_body_passes_consistency():
    """验收①：正文含样例前缀的评论写入不再被 409 中止。"""
    body = (
        "用户反馈：样例前缀 issue456_* 与 issue14088_* 在评论里被当成主语引用其它"
        "编号，导致发布被 409 中止。"
    )
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329, body=body, operation="issues comments create")
    assert ok, msg


def test_test_filenames_and_paths_are_not_subject():
    """验收②：测试文件名/路径片段不作主语（不 409）。"""
    assert release_guard.first_issue_anchor(
        "回归 tests/runtime/test_issue190_knowledge_dedup.py") is None
    assert release_guard.first_issue_anchor(
        "新增 tests/runtime/test_issue_4329_release_guard_subject_boundary.py") is None
    assert release_guard.first_issue_anchor(
        "路径 src/domains/runtime/vcs/test_issue2225_release_guard.py 已存在") is None
    # 词元续写形态：编号后紧跟下划线/字母 = 标识符片段，不是完整编号词元。
    assert release_guard.first_issue_anchor("样例 Issue 456_* 不算主语") is None
    assert release_guard.first_issue_anchor("样例 Issue 456abc 不算主语") is None


def test_test_filename_body_passes_consistency():
    """验收②：正文含测试文件名/路径的评论写入不再被 409 中止。"""
    body = (
        "请回归 tests/runtime/test_issue_2225_release_guard.py 与"
        " tests/runtime/test_issue_3811_release_guard_domain_routing.py，"
        "以及 tests/runtime/test_issue_4257_release_guard_link_chain.py。"
    )
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329, body=body, operation="issues comments create")
    assert ok, msg


def test_explicit_cross_issue_subject_still_blocks():
    """验收③：正文显式引用其它 Issue 仍 409 拦截，语义不放宽。"""
    body = "## 概述\n\n实现 Issue #9999 的需求，与当前任务无关。"
    ok, _ = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329, body=body, operation="issues comments create")
    assert not ok

    ok, _ = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329,
        body="Spec: Issue 9999 — 另一个会话的正文", operation="issues update")
    assert not ok

    # 标识符续写不算主语，但正文中真实存在的显式主语仍要命中。
    assert release_guard.first_issue_anchor(
        "issue456_* 样例外，正文另有 Issue #8888 显式主语") == 8888


def test_bare_hash_ref_is_not_body_subject():
    """裸 `#N`（PR/commit 引用）不提升为正文主语。"""
    assert release_guard.first_issue_anchor("评审 PR #83，commit #123，参考 #2080") is None
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329,
        body="评审 PR #83 与 commit #123 的改动", operation="pulls comments create")
    assert ok, msg


def test_existing_subject_semantics_not_regressed():
    """验收④：既有显式主语语义不回归。"""
    assert release_guard.first_issue_anchor(
        "## 概述\n\n实现 Issue #4329：收紧主语边界。\n\n参考 Issue #2080 与 #2085") == 4329
    assert release_guard.first_issue_anchor("实现Issue #4329的隔离") == 4329
    assert release_guard.first_issue_anchor("Spec: Issue #2085—正文") == 2085
    assert release_guard.first_issue_anchor("实现 Issue 4329 的修复") == 4329
    assert release_guard.first_issue_anchor("实现 Issue # 4329 的修复") == 4329
    # 引用行（> 开头）是他号正文摘录，不作主语。
    assert release_guard.first_issue_anchor(
        "> Issue #2085 引用行\n\n正文没有其它主语") is None


def test_chain_ref_still_not_regressed():
    """验收④：链路引用（同域 4257 单）剔除不回归。"""
    chain = "vod#25105 → issue#25107 → coding#25108"
    assert release_guard.first_issue_anchor(chain) is None
    assert release_guard.first_issue_anchor(
        chain + "\n\n[Coding Agent] 实现 Issue #25107 已完成") == 25107
    ok, msg = release_guard.check_issue_consistency(
        task_issue=25107, target_issue=25107, body=chain,
        operation="issues comments create")
    assert ok, msg


def test_matching_subject_body_passes():
    """主语与任务一致时放行（正常写路径）。"""
    ok, msg = release_guard.check_issue_consistency(
        task_issue=4329, target_issue=4329,
        body="[Coding Agent] 实现 Issue #4329 完成，测试文件"
             " test_issue_4329_release_guard_subject_boundary.py 已新增。",
        operation="issues comments create")
    assert ok, msg
