"""direct 模式 chat 会话 token 注入回归测试（chat-direct-session-token-injection-design.md）。

任务会话（绑定真实仓库）的 Chat Agent 在 direct 执行时须注入 GRAPE_SESSION_TOKEN，
否则无法调用仓库作用域内部端点（如 git-repo issue comment 回写澄清结论）。queue 模式已由
enqueue_task 注入；本文件覆盖公共 helper `chat_session_env_extra`（Web 侧迁移后）与三条
direct 调用点透传（chat.py 经公共 helper 注入，见 feishu-task-chat-direct-session-token-injection-design.md）。
"""
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.session_manager import db, db_lock, create_conversation


def _seed_local_engine():
    """本机直连引擎（credential_id=0）→ auto 语义下 chat 走 direct（与既有 direct 用例同款）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name) != 'opencode'")
        c.execute("DELETE FROM chat_agent_session WHERE user_id=99902")
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode'").fetchone()
        if row:
            c.execute("UPDATE agent_runtimes SET installed=1, healthy=1, status='online' WHERE id=?", (row[0],))
        else:
            c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                      "VALUES ('opencode','test-local',0,1,1,'online','global','')")
        db.commit()
    # resolve_local_engine 有 30s 模块级缓存（engine_select.py:19/435），清掉避免吃到被其他测试清理的幽灵 runtime id
    from src.runtime import engine_select
    engine_select._resolve_local_cache.clear()


@pytest.fixture
def client():
    from src.api.deps import require_no_session_token
    app.dependency_overrides[require_no_session_token] = lambda: {"id": 99902, "username": "tester2"}
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        c2 = db.cursor()
        c2.execute("DELETE FROM agent_runtimes WHERE display_name='test-local'")
        c2.execute("DELETE FROM chat_agent_session WHERE user_id=99902")
        for (cid,) in c2.execute("SELECT id FROM chat_conversations WHERE user_id=99902").fetchall():
            c2.execute("DELETE FROM chat_messages WHERE conversation_id=?", (cid,))
            c2.execute("DELETE FROM chat_conversations WHERE id=?", (cid,))
        c2.execute("DELETE FROM session_api_tokens WHERE repo_key IN ('gitcode:owner/repo','chat:99902') OR user_id=99902")
        db.commit()


class TestChatSessionEnvExtra:
    def test_real_repo_injects_session_token(self):
        """真仓库 → 注入 tk_ 会话 token + GRAPE_CLI_MODE=WORKER，且作用域校验通过。"""
        from src.api.chat_session_env import chat_session_env_extra
        from src.api.auth_token import verify_session_token, verify_session_token_for_repo
        env = chat_session_env_extra(99902, "test-conv", "gitcode:owner/repo")
        assert env["USER_ID"] == "99902"
        assert env["CONVERSATION_ID"] == "test-conv"
        tok = env["GRAPE_SESSION_TOKEN"]
        assert tok.startswith("tk_") and env["GRAPE_CLI_MODE"] == "WORKER"
        info = verify_session_token(tok)
        assert info and info["repo_key"] == "gitcode:owner/repo"
        assert info["agent_type"] == "chat" and info["user_id"] == 99902
        assert verify_session_token_for_repo(tok, "gitcode:owner/repo")
        assert verify_session_token_for_repo(tok, "github:other/repo") is None

    def test_pseudo_or_empty_repo_no_token(self):
        """伪仓库 / 空仓库 → 不注入 token 与 WORKER（一般对话行为不变）。"""
        from src.api.chat_session_env import chat_session_env_extra
        for repo_key in ("", "chat:99902"):
            env = chat_session_env_extra(99902, "conv-x", repo_key)
            assert "GRAPE_SESSION_TOKEN" not in env
            assert "GRAPE_CLI_MODE" not in env
            assert env["USER_ID"] == "99902" and env["CONVERSATION_ID"] == "conv-x"


class TestDirectChatInjectsToken:
    def test_send_task_conv_injects_token(self, client, monkeypatch):
        """/chat/send direct + 任务会话 → stream_chat_agent 收到含 GRAPE_SESSION_TOKEN 的 env_extra。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        _seed_local_engine()
        cid = create_conversation(99902, "gitcode:owner/repo", title="Issue #2",
                                  conv_type="task", issue_number=2)
        captured = {}

        def _fake_stream(prompt, cwd, session_id="", **kw):
            captured.update(kw)
            yield ("text", "已回写澄清结论")
            yield ("done", {"exit_code": 0, "text": "已回写澄清结论", "oc_session_id": "ses_x"})

        with patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream):
            r = client.post("/api/chat/send", json={"message": "请把澄清结论回写 Issue #2", "conversation_id": cid})
        assert r.status_code == 200, r.text
        env = captured.get("env_extra") or {}
        tok = env.get("GRAPE_SESSION_TOKEN", "")
        assert tok.startswith("tk_"), "direct 任务会话必须注入 tk_ 会话 token"
        assert env.get("GRAPE_CLI_MODE") == "WORKER"
        from src.api.auth_token import verify_session_token_for_repo
        assert verify_session_token_for_repo(tok, "gitcode:owner/repo")

    def test_send_general_conv_no_token(self, client, monkeypatch):
        """/chat/send direct + 一般对话 → 不注入 token（行为不变）。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        _seed_local_engine()
        cid = create_conversation(99902, "", title="一般对话")
        captured = {}

        def _fake_stream(prompt, cwd, session_id="", **kw):
            captured.update(kw)
            yield ("text", "你好")
            yield ("done", {"exit_code": 0, "text": "你好", "oc_session_id": "ses_x"})

        with patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream):
            r = client.post("/api/chat/send", json={"message": "你好", "conversation_id": cid})
        assert r.status_code == 200, r.text
        env = captured.get("env_extra") or {}
        assert "GRAPE_SESSION_TOKEN" not in env
        assert "GRAPE_CLI_MODE" not in env