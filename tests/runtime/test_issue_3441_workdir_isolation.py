"""Issue #3441：coding 任务 worktree 按 Issue 隔离分配 + 启动一致性校验（fail-fast）。

覆盖 Spec 验收项：
- 按 Issue 分配路径唯一性（同 Issue 复用、跨 Issue 硬隔离）；
- workdir 中 Issue 编号与任务不一致时启动即失败并输出含双方编号的显式错误；
- 解析失败按不一致处理（fail-fast，不静默继续）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.engine.worktree_guard import (
    extract_workdir_issue_num,
    validate_workdir_issue_consistency,
)
from src.domains.runtime.engine.dependency_check import (
    DependencyError,
    validate_task_dependencies,
)
from src.api import event_pipeline as ep


def _base_task(**kw):
    task = {
        "id": "t_test3441",
        "agent_type": "coding",
        "prompt": "do work",
        "skill": "coding-cli-1",
        "repo_key": "gitcode:org/test",
        "issue_id": "#3383",
        "issue_session_id": "s1",
        "cwd": "/ws/gitcode/org/test/issues/3383/coding",
    }
    task.update(kw)
    return task


class TestWorkdirAllocationUniqueness:
    """按 Issue 维度派生 workdir：跨 Issue 硬隔离、同 Issue 复用。"""

    def test_cross_issue_paths_distinct(self):
        ws_a = ep.get_agent_workdir("gitcode:org/test", "coding", "#3383")
        ws_b = ep.get_agent_workdir("gitcode:org/test", "coding", "#3350")
        assert ws_a != ws_b
        assert ws_a.endswith(os.path.join("issues", "3383", "coding"))
        assert ws_b.endswith(os.path.join("issues", "3350", "coding"))

    def test_same_issue_path_reused(self):
        ws_a = ep.get_agent_workdir("gitcode:org/test", "coding", "#3383")
        ws_b = ep.get_agent_workdir("gitcode:org/test", "coding", "3383")
        assert ws_a == ws_b

    def test_agent_dirs_isolated_within_issue(self):
        ws_cod = ep.get_agent_workdir("gitcode:org/test", "coding", "#3383")
        ws_qa = ep.get_agent_workdir("gitcode:org/test", "qa", "#3383")
        assert ws_cod != ws_qa


class TestExtractWorkdirIssueNum:
    def test_new_layout(self):
        assert extract_workdir_issue_num(
            "/root/grape-workspaces/gitcode/org/test/issues/1588/coding") == "1588"

    def test_legacy_layout(self):
        assert extract_workdir_issue_num(
            "/root/grape-workspaces/test/coding/issues/3383") == "3383"

    def test_no_issue_segment(self):
        assert extract_workdir_issue_num("/root/grape-workspaces/org/test/coding-agent") == ""
        assert extract_workdir_issue_num("") == ""

    def test_pr_subscription_pseudo_id_segment(self):
        # PR 订阅伪 id（get_agent_workdir 对 #pr_<PR>_<scene> 生成 issues/pr_.../<agent>）
        assert extract_workdir_issue_num(
            "/ws/gitcode/org/test/issues/pr_257_default/qa") == "pr_257_default"

    def test_string_issue_segment(self):
        # Gitee 等平台的字符串型 Issue 标识
        assert extract_workdir_issue_num(
            "/ws/gitee/org/test/issues/IABC123/coding") == "IABC123"
        assert extract_workdir_issue_num(
            "/ws/test/coding/issues/IABC123") == "IABC123"

    def test_earlier_issues_segment_not_misparsed(self):
        # 尾部布局锚定：更早出现的名为 issues 的目录/仓库段不得被误取
        assert extract_workdir_issue_num(
            "/ws/issues/repo/gitcode/org/test/issues/3383/coding") == "3383"
        assert extract_workdir_issue_num(
            "/ws/issues/repo/test/coding/issues/3383") == "3383"

    def test_trailing_separator_tolerated(self):
        assert extract_workdir_issue_num(
            "/ws/gitcode/org/test/issues/3383/coding/") == "3383"

    def test_consistent_with_parse_issue_workdir(self):
        # 同一路径两种解析口径一致（复用同一尾部锚定解析）
        from src.domains.runtime.engine.worktree_guard import parse_issue_workdir
        path = "/root/grape-workspaces/gitcode/org/test/issues/3383/coding"
        assert extract_workdir_issue_num(path) == "3383"
        assert parse_issue_workdir(
            path, "/root/grape-workspaces") == ("gitcode:org/test", "3383", "coding")


class TestValidateWorkdirIssueConsistency:
    def test_match_passes(self):
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/issues/3383/coding", "#3383", "coding")
        assert ok is True and msg == ""

    def test_mismatch_aborts_with_both_numbers(self):
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/issues/1588/coding", "#3383", "coding")
        assert ok is False
        assert "#1588" in msg and "#3383" in msg

    def test_unparsable_workdir_treated_as_mismatch(self):
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/coding-agent", "#3383", "coding")
        assert ok is False
        assert "#3383" in msg

    def test_non_issue_task_skipped(self):
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/coding-agent", "#0", "coding")
        assert ok is True and msg == ""
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/coding-agent", "", "coding")
        assert ok is True and msg == ""
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/coding-agent", "0", "coding")
        assert ok is True and msg == ""

    def test_pr_subscription_pseudo_id_consistent(self):
        """PR 订阅伪 id：issues/pr_... 段与去 # 后的任务标识按字符串相等 → 放行。

        回归守卫：旧实现按首段数字提取（pr_257_default → 257）且正则只认纯数字，
        会把合法 PR 订阅任务误判为 mismatch，叠加 dependency_check 硬拦截导致
        启动即失败。
        """
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/issues/pr_257_default/qa", "#pr_257_default", "qa")
        assert ok is True and msg == ""

    def test_pr_subscription_pseudo_id_not_collapsed_to_digits(self):
        """伪 id 不得被数字提取折叠：pr_257_default ≠ issues/257（字符串比较）。"""
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/issues/257/qa", "#pr_257_default", "qa")
        assert ok is False
        assert "257" in msg and "pr_257_default" in msg

    def test_string_issue_id_consistent(self):
        # Gitee 字符串型 Issue 标识：workdir 段与任务标识字符串相等 → 放行
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitee/org/test/issues/IABC123/coding", "IABC123", "coding")
        assert ok is True and msg == ""

    def test_string_issue_id_mismatch(self):
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitee/org/test/issues/IABC124/coding", "IABC123", "coding")
        assert ok is False
        assert "IABC124" in msg and "IABC123" in msg

    def test_numeric_id_in_workdir_not_matched_by_string_task(self):
        # 任务为字符串标识时不得从 workdir 数字段放行（防止反向数字折叠）
        ok, msg = validate_workdir_issue_consistency(
            "/ws/gitcode/org/test/issues/257/qa", "pr_257_default", "qa")
        assert ok is False


class TestDependencyCheckGate:
    """任务启动入口（dependency_check）的 fail-fast 中止行为。"""

    def test_mismatched_cwd_blocks_task_start(self, monkeypatch):
        monkeypatch.setattr(
            "src.kernel.token_service.resolve_repo_config_level",
            lambda rk: type("L", (), {"level": "repo"})())
        task = _base_task(cwd="/ws/gitcode/org/test/issues/1588/coding")
        try:
            validate_task_dependencies(task, "opencode-test")
            raised = None
        except DependencyError as e:
            raised = str(e)
        assert raised is not None
        assert "#1588" in raised and "#3383" in raised

    def test_matched_cwd_passes(self, monkeypatch):
        monkeypatch.setattr(
            "src.kernel.token_service.resolve_repo_config_level",
            lambda rk: type("L", (), {"level": "repo"})())
        task = _base_task()
        validate_task_dependencies(task, "opencode-test")

    def test_non_git_agent_not_gated(self, monkeypatch):
        task = _base_task(agent_type="chat",
                          cwd="/ws/gitcode/org/test/issues/1588/coding")
        validate_task_dependencies(task, "opencode-test")

    def test_pr_subscription_worker_task_passes(self, monkeypatch):
        """PR 订阅 worker 任务（伪 id cwd + 伪 id issue_id）不再被误判 mismatch。"""
        monkeypatch.setattr(
            "src.kernel.token_service.resolve_repo_config_level",
            lambda rk: type("L", (), {"level": "repo"})())
        task = _base_task(issue_id="#pr_257_default",
                          cwd="/ws/gitcode/org/test/issues/pr_257_default/qa",
                          agent_type="qa")
        validate_task_dependencies(task, "opencode-test")

    def test_pr_subscription_pseudo_id_mismatch_still_blocked(self, monkeypatch):
        """伪 id 与 workdir 段不一致（真错配）仍 fail-fast 硬拦截。"""
        monkeypatch.setattr(
            "src.kernel.token_service.resolve_repo_config_level",
            lambda rk: type("L", (), {"level": "repo"})())
        task = _base_task(issue_id="#pr_257_default",
                          cwd="/ws/gitcode/org/test/issues/pr_258_default/qa",
                          agent_type="qa")
        try:
            validate_task_dependencies(task, "opencode-test")
            raised = None
        except DependencyError as e:
            raised = str(e)
        assert raised is not None
        assert "pr_258_default" in raised and "pr_257_default" in raised
