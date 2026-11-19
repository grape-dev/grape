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
