"""Web chat 回归测试：stream_agent 的 tool_use 事件是 dict，日志切片不得崩溃

2026-08-09 生产事故：send_message / send_message_stream 的 agent_worker 日志 `data[:150]`
对 dict 切片抛 KeyError → agent_worker 异常中止 → web chat 无回复。
（根因见 docs/new-version2/web-chat-postmortem.md）
"""
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user
from src.session_manager import db, db_lock


def _fake_stream_agent(prompt, cwd, session_id="", **kw):
    """模拟 stream_agent：tool_use 为 dict（M1 结构化），随后 text + done。"""
    yield ("step_start", "Step 1 started (session: ses_x)")
    yield ("tool_use", {"tool": "read", "input": '{"filePath": "/x/SKILL.md"}'})
    yield ("tool_use", {"tool": "bash", "input": '{"command": "curl -s https://api.gitcode.com/x"}'})
    yield ("text", "联通成功")
    yield ("done", {"exit_code": 0, "text": "联通成功", "oc_session_id": "ses_x"})


@pytest.fixture
def client():
    def _fake_user():
        return {"id": 99901, "username": "tester"}
    app.dependency_overrides[get_current_user] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        c2 = db.cursor()
        convs = c2.execute("SELECT id FROM chat_conversations WHERE user_id=99901").fetchall()
        for (cid,) in convs:
            c2.execute("DELETE FROM chat_messages WHERE conversation_id=?", (cid,))
            c2.execute("DELETE FROM chat_conversations WHERE id=?", (cid,))
        db.commit()


class TestWebChatToolUseRegression:
    def test_send_stream_handles_dict_tool_use(self, client):
        """send/stream：agent 产出 dict tool_use 时不应崩溃，回复应送达。"""
        with patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = client.post("/api/chat/send/stream", json={"message": "测试", "repo_key": ""})
        assert r.status_code == 200
        body = r.text
        assert "后端错误" not in body, f"agent_worker 不应因 tool_use dict 崩溃: {body[:500]}"
        assert '"type": "done"' in body, "SSE 应包含 done 事件（含 agent 回复）"

    def test_send_handles_dict_tool_use(self, client):
        """send（非流式）：同样不应因 dict tool_use 崩溃，reply 应返回。"""
        with patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = client.post("/api/chat/send", json={"message": "测试", "repo_key": ""})
        assert r.status_code == 200
        data = r.json()
        assert data.get("reply") == "联通成功", data

    def test_tool_use_logging_uses_str(self):
        """日志对 dict 用 str(data)[:N]（修复点：不能 data[:N] 直接切片 dict）。"""
        from src.api.routes import chat as chat_mod
        import inspect
        src = inspect.getsource(chat_mod)
        # 断言不再存在对 tool_use 的裸切片
        assert "data[:150]" not in src, "tool_use 日志不得对 data 直接切片（dict）"
