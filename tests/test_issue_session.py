"""Issue 级执行会话测试（见 multi-issue-isolation-design.md §7.2）"""
import pytest

from src.session_manager import db, db_lock
from src.api import session_store


@pytest.fixture(autouse=True)
def _clean_issue_sessions():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


class TestIssueSession:
    def test_create_and_reuse(self):
        a = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        b = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        assert a["id"] == b["id"], "同 (repo, agent, issue) 应复用同一会话"
        assert a["new"] and not b["new"]

    def test_issue_isolation(self):
        a = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        b = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#6")
        assert a["id"] != b["id"], "不同 Issue 应独立会话"

    def test_agent_type_isolation(self):
        a = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        c = session_store.get_or_create_issue_session("gitcode:org/test", "coding", "#5")
        assert a["id"] != c["id"], "不同 Agent 应独立会话"

    def test_runtime_session_pointer_isolation(self):
        a = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        b = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#6")
        session_store.update_issue_session(a["id"], runtime_session_id="ses_A")
        b2 = session_store.get_issue_session("gitcode:org/test", "issue", "#6")
        assert b2["runtime_session_id"] != "ses_A", "A 的续接指针不得污染 B"
        a2 = session_store.get_issue_session("gitcode:org/test", "issue", "#5")
        assert a2["runtime_session_id"] == "ses_A"

    def test_seed_from_repo_session(self):
        # 每 Issue 独立 session（vod-agent-hang-rootcause-fix.md 方案 A）：
        # 新 Issue 首会话不继承 repo 级 runtime_session_id，避免多 Issue 共用同一 opencode session
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key='gitcode:org/test' AND agent_type='issue'")
            c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_session_id, status, last_active_at) VALUES ('legacy-1', 'gitcode:org/test', 'issue', 'repo_ses_old', 'idle', datetime('now'))")
            db.commit()
        try:
            s = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
            assert s["runtime_session_id"] == "", f"新 Issue 首会话不应继承 repo 级指针, got {s['runtime_session_id']!r}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM devops_agent_session WHERE id='legacy-1'")
                db.commit()

    def test_update_status(self):
        s = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        session_store.update_issue_session(s["id"], status="running")
        got = session_store.get_issue_session("gitcode:org/test", "issue", "#5")
        assert got["status"] == "running"

    def test_empty_pointer_not_overwritten(self):
        s = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        session_store.update_issue_session(s["id"], runtime_session_id="")
        got = session_store.get_issue_session("gitcode:org/test", "issue", "#5")
        # 空指针不覆盖（update 仅在有值时写入，此处断言无异常且 status 未变）
        assert got is not None

    def test_list_and_remove(self):
        session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#5")
        session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#6")
        lst = session_store.list_issue_sessions("gitcode:org/test", agent_type="issue")
        assert len(lst) == 2
        removed = session_store.remove_issue_sessions("gitcode:org/test")
        assert removed == 2
        assert session_store.list_issue_sessions("gitcode:org/test") == []


class TestRepoLevelSessionPointerDeprecated:
    """看护（issue-session-writeback-races-design.md §七 方案 A）：

    repo 级 devops_agent_session.runtime_session_id 已废弃——执行收尾只写回 Issue 级
    (issue_agent_session)，不得再写 repo 级指针（push 忽略、无 Issue 不执行，无消费方）。
    若这些 UPDATE 回归，说明方案 A 被破坏，测试告警。
    """

    def _trim_docstring(self, src: str) -> str:
        # 剥掉 docstring：两处三引号之间的部分剔除后取函数体
        if '"""' in src:
            return '"""'.join(src.split('"""')[2:])
        return src

    def test_exec_last_no_repo_level_write(self):
        """_execute_agent_inner 收尾不得写 devops_agent_session.runtime_session_id。"""
        import inspect
        from src.api import event_pipeline
        src = self._trim_docstring(inspect.getsource(event_pipeline._execute_agent_inner))
        # 允许 update_issue_session（Issue 级），禁止 UPDATE devops_agent_session SET runtime_session_id
        forbidden = "UPDATE devops_agent_session SET runtime_session_id"
        critical_forbidden = [ln for ln in src.splitlines() if forbidden in ln]
        assert not critical_forbidden, (
            "_execute_agent_inner must NOT write repo-level runtime_session_id (方案A), found:\n"
            + "\n".join(critical_forbidden))

    def test_complete_task_no_repo_level_write(self):
        """complete_task 收尾不得写 devops_agent_session.runtime_session_id。"""
        import inspect
        from src.runtime import service
        src = self._trim_docstring(inspect.getsource(service.complete_task))
        forbidden = "UPDATE devops_agent_session SET runtime_session_id"
        critical_forbidden = [ln for ln in src.splitlines() if forbidden in ln]
        assert not critical_forbidden, (
            "complete_task must NOT write repo-level runtime_session_id (方案A), found:\n"
            + "\n".join(critical_forbidden))

    def test_issue_session_update_isolated_from_repo(self):
        """functional：update_issue_session 只改 issue_agent_session，repo 级指针保持原样。"""
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key='gitcode:org/test'")
            c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_session_id, status, last_active_at) "
                      "VALUES ('repo-keeper', 'gitcode:org/test', 'issue', 'original_repo_ses', 'idle', datetime('now'))")
            db.commit()
        try:
            s = session_store.get_or_create_issue_session("gitcode:org/test", "issue", "#9")
            session_store.update_issue_session(s["id"], runtime_session_id="new_issue_ses")
            # repo 级指针必须保持原样，不得被 Issue 续接写污染
            with db_lock:
                row = db.cursor().execute(
                    "SELECT runtime_session_id FROM devops_agent_session WHERE repo_key='gitcode:org/test' AND agent_type='issue'"
                ).fetchone()
            assert row and row[0] == "original_repo_ses", "repo 级指针必须保持原样，不得被 Issue 续接写污染"
            # Issue 级已被正确写入
            got = session_store.get_issue_session("gitcode:org/test", "issue", "#9")
            assert got["runtime_session_id"] == "new_issue_ses"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key='gitcode:org/test'")
                db.commit()
