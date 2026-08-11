"""方案 A：每个 Issue 独立 session（见 vod-agent-hang-rootcause-fix.md 方案 A）。

核心：get_or_create_issue_session 新 Issue 首次创建时 runtime_session_id 为空，
不继承 repo 级 devops_agent_session 指针，避免多 Issue 共用同一 opencode session。
"""
import uuid
from src.session_manager import db, db_lock
from src.api.session_store import get_or_create_issue_session, update_issue_session


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


def _seed_repo_session(repo_key="gitcode:owner/repo", agent_type="vod", sid="ses_repo_xxx"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_session_id, status, enable) "
            "VALUES (?,?,?,?, 'idle', 1)",
            (str(uuid.uuid4()), repo_key, agent_type, sid)
        )
        db.commit()


class TestIssueSessionIsolation:
    def teardown_method(self):
        _clean()

    # A1: 新 Issue 不继承 repo 级 session
    def test_a1_new_issue_does_not_inherit_repo_session(self):
        _clean()
        _seed_repo_session(sid="ses_repo_xxx")
        s = get_or_create_issue_session("gitcode:owner/repo", "vod", "#2")
        assert s["runtime_session_id"] == "", f"新 Issue 不应继承 repo 级 session, got {s['runtime_session_id']!r}"
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT runtime_session_id FROM issue_agent_session WHERE id=?", (s["id"],)
            ).fetchone()
        assert row[0] == "" or row[0] is None

    # A2: 两个 Issue 会话指针相互独立
    def test_a2_two_issues_independent(self):
        _clean()
        s1 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#1")
        s2 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#2")
        assert s1["id"] != s2["id"]
        update_issue_session(s1["id"], runtime_session_id="ses_one")
        update_issue_session(s2["id"], runtime_session_id="ses_two")
        with db_lock:
            c = db.cursor()
            r1 = c.execute("SELECT runtime_session_id FROM issue_agent_session WHERE id=?", (s1["id"],)).fetchone()[0]
            r2 = c.execute("SELECT runtime_session_id FROM issue_agent_session WHERE id=?", (s2["id"],)).fetchone()[0]
        assert r1 == "ses_one" and r2 == "ses_two" and r1 != r2

    # A3: 已有 Issue 会话复用（不重复建）
    def test_a3_same_issue_reuse(self):
        _clean()
        s1 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#5")
        s2 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#5")
        assert s1["id"] == s2["id"]
        assert s2["new"] is False

    # A4: 首次无 session 可正常新跑（返回空 session，不报错）
    def test_a4_first_no_session_ok(self):
        _clean()
        _seed_repo_session(sid="ses_repo_xxx")
        s = get_or_create_issue_session("gitcode:owner/repo", "vod", "#9")
        assert s["runtime_session_id"] == ""
        assert s["new"] is True

    # A5: 同一 Issue 后续事件复用该 Issue 自己的 session（同 Issue 串行续接）
    def test_a5_same_issue_continues_own_session(self):
        _clean()
        s1 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#5")
        # 第一次执行后回写该 Issue 自己的 session
        update_issue_session(s1["id"], runtime_session_id="ses_issue5_own")
        s2 = get_or_create_issue_session("gitcode:owner/repo", "vod", "#5")
        assert s2["runtime_session_id"] == "ses_issue5_own", "同 Issue 后续事件应复用本 Issue 的 session"
