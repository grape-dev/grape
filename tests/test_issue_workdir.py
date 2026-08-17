"""Issue 级工作目录测试（见 multi-issue-isolation-design.md §7.3 / §3.4）"""
import sys, os
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.api import event_pipeline as ep


class TestWorkdir:
    def test_issue_level_workdir(self):
        ws = ep.get_agent_workdir("gitcode:org/test", "issue", "#5")
        assert ws.endswith(os.path.join("org", "test", "issues", "5", "issue")), ws

    def test_workdir_normalized_issue_num(self):
        ws = ep.get_agent_workdir("gitcode:org/test", "coding", "5")
        assert ws.endswith(os.path.join("org", "test", "issues", "5", "coding")), ws

    def test_workdir_repo_fallback_for_0(self):
        ws = ep.get_agent_workdir("gitcode:org/test", "issue", "#0")
        assert ws.endswith("issue-agent"), ws

    def test_workdir_repo_fallback_no_issue(self):
        ws = ep.get_agent_workdir("gitcode:org/test", "issue", "")
        assert ws.endswith("issue-agent"), ws


class TestIssueNum:
    def test_strips_hash(self):
        assert ep._issue_num_from("#5") == "5"
        assert ep._issue_num_from("5") == "5"

    def test_zero_or_empty_is_empty(self):
        assert ep._issue_num_from("#0") == ""
        assert ep._issue_num_from("") == ""
        assert ep._issue_num_from("0") == ""
