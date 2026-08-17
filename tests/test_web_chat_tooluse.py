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


def _seed_local_engine():
    """本机直连引擎（credential_id=0，已知类型 opencode），供 direct 用例按 engine-selection-design.md v2 解析。"""
    with db_lock:
        c = db.cursor()
        # 隔离：清掉其他测试遗留的非 opencode 本机直连候选（如幽灵 'default'），保证 resolve_local_engine
        # 命中本测试引擎；同时清掉用户 99901 的 chat 配置残留，避免 get_chat_config 命中幽灵 runtime_id。
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name) != 'opencode'")
        c.execute("DELETE FROM chat_agent_session WHERE user_id=99901")
        # 复用/恢复健康的 opencode 本机行（其他用例也依赖其做兜底），缺失则插入
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode'").fetchone()
        if row:
            c.execute("UPDATE agent_runtimes SET installed=1, healthy=1, status='online' WHERE id=?", (row[0],))
        else:
            c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                      "VALUES ('opencode','test-local',0,1,1,'online','global','')")
        db.commit()


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
        c2.execute("DELETE FROM agent_runtimes WHERE display_name='test-local'")
        c2.execute("DELETE FROM chat_agent_session WHERE user_id=99901")
        convs = c2.execute("SELECT id FROM chat_conversations WHERE user_id=99901").fetchall()
        for (cid,) in convs:
            c2.execute("DELETE FROM chat_messages WHERE conversation_id=?", (cid,))
            c2.execute("DELETE FROM chat_conversations WHERE id=?", (cid,))
        db.commit()


class TestWebChatToolUseRegression:
    def test_send_stream_handles_dict_tool_use(self, client):
        """send/stream：agent 产出 dict tool_use 时不应崩溃，回复应送达。"""
        _seed_local_engine()
        with patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = client.post("/api/chat/send/stream", json={"message": "测试", "repo_key": ""})
        assert r.status_code == 200
        body = r.text
        assert "后端错误" not in body, f"agent_worker 不应因 tool_use dict 崩溃: {body[:500]}"
        assert '"type": "done"' in body, "SSE 应包含 done 事件（含 agent 回复）"

    def test_send_handles_dict_tool_use(self, client):
        """send（非流式）：同样不应因 dict tool_use 崩溃，reply 应返回。"""
        _seed_local_engine()
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

    def test_send_stream_queue_mode_returns_result(self, client, monkeypatch):
        """send/stream 非 direct 模式：应入队 + 轮询，由远端 RuntimeAgent 执行后以 SSE done 返回结果。

        新模型（engine-selection-design.md §8.2）：chat 未配置引擎 → 本机直连(direct)；
        本用例显式配置远端引擎（chat_agent_session.runtime_id），走 queue_remote 入队。
        """
        from unittest.mock import AsyncMock, patch
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        # seed 远端引擎 + chat 配置指向它
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=99901")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'wt_%'")
            c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                      "VALUES ('hwt','wt-cred','wt-dev','global','tester','active')")
            _cid = c.lastrowid
            c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                      "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            db.commit()
        from src.session_manager import set_chat_config
        set_chat_config(99901, backend="opencode")
        with patch("src.runtime.service.enqueue_task", return_value="t_q") as m_enq, \
             patch("src.runtime.service.wait_task_terminal",
                   new=AsyncMock(return_value={"status": "completed", "exit_code": 0,
                                               "result_text": "QUEUE_OK",
                                               "usage_json": '{"session_id": "ses_q"}'})), \
             patch("src.runtime.executor.stream_agent") as m_sa:
            r = client.post("/api/chat/send/stream", json={"message": "测试", "repo_key": ""})

        assert r.status_code == 200
        assert "QUEUE_OK" in r.text, f"SSE 应包含任务结果: {r.text[:500]}"
        assert '"type": "done"' in r.text
        m_enq.assert_called_once()
        assert m_enq.call_args.kwargs["runtime_id"] == _rid
        m_sa.assert_not_called(), "queue 模式不应本地 stream_agent"
        # 清理 get_or_create_agent_session 产生的会话行，避免污染其他测试
        with db_lock:
            c2 = db.cursor()
            c2.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'chat:99901%' OR id LIKE 'chat:99901%'")
            c2.execute("DELETE FROM chat_agent_session WHERE user_id=99901")
            c2.execute("DELETE FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'wt-%')")
            c2.execute("DELETE FROM runtime_credentials WHERE name LIKE 'wt-%'")
            db.commit()

    def test_send_stream_direct_still_streams(self, client, monkeypatch):
        """send/stream direct 模式：仍走本地 stream_agent（回归保护）。"""
        from unittest.mock import patch

        def _done_text(sse):
            import json
            for line in sse.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[len("data: "):])
                    if evt.get("type") == "done":
                        return evt.get("text", "")
            return ""

        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        _seed_local_engine()
        with patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = client.post("/api/chat/send/stream", json={"message": "测试", "repo_key": ""})
        assert r.status_code == 200
        assert _done_text(r.text) == "联通成功"
        assert '"type": "done"' in r.text
