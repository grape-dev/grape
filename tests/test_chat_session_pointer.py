"""Issue #400：chat 会话引擎指针一致性 — get_or_create_agent_session(runtime_id=) 测试。

验收：显式传入会话引擎指针（用户 chat 配置 runtime_id）时，新建 devops_agent_session
的 runtime_id 与该指针一致（会话指针 == 实际执行引擎，对齐 guard_issue_session_pointer）；
未传指针时保持既有按名/按集合解析行为。
"""
import pytest

from src.api import session_store
from src.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean_sessions():
    with db_lock:
        db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'chat:issue400%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'chat:issue400%'")
        db.commit()


class TestChatSessionPointer:
    def test_create_with_explicit_runtime_id(self):
        s = session_store.get_or_create_agent_session("chat:issue400_1", "chat", "opencode", runtime_id=42)
        assert s["new"] is True
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_id FROM devops_agent_session WHERE repo_key='chat:issue400_1' AND agent_type='chat'"
            ).fetchone()
        assert row and row[0] == 42, "会话引擎指针应等于传入的 runtime_id"

    def test_existing_session_untouched(self):
        session_store.get_or_create_agent_session("chat:issue400_2", "chat", "opencode", runtime_id=42)
        s2 = session_store.get_or_create_agent_session("chat:issue400_2", "chat", "opencode", runtime_id=99)
        assert s2["new"] is False
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_id FROM devops_agent_session WHERE repo_key='chat:issue400_2' AND agent_type='chat'"
            ).fetchone()
        assert row and row[0] == 42, "已存在会话不回写指针（与既有语义一致）"

    def test_no_runtime_id_keeps_legacy_behavior(self):
        """未传 runtime_id（默认 0）：仍走引擎集合/按名解析，不报错。"""
        s = session_store.get_or_create_agent_session("chat:issue400_3", "chat", "opencode")
        assert s.get("new") is True
        assert "runtime_session_id" in s