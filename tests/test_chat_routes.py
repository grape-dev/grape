"""Chat Agent 路由测试（src/api/routes/chat.py）。

覆盖：会话 CRUD、send/send-stream 各路径（无引擎/direct/queue/任务会话）、
反馈、chat 配置（GET/PUT、app_id 占用校验、技能解析）、内部辅助函数。
通过 patch get_chat_config 确定性控制 backend/runtime_id；stream_agent / BotManager 用 mock。
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user
from src.session_manager import db, db_lock, get_chat_config
from src.user import db as user_db

UID = 99001
USERNAME = "chat_tester"


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        c2 = db.cursor()
        c2.execute("DELETE FROM chat_conversations WHERE user_id=?", (UID,))
        c2.execute("DELETE FROM chat_agent_session WHERE user_id=?", (UID,))
        c2.execute("DELETE FROM agent_runtimes WHERE name LIKE 'chat_rt%'")
        c2.execute("DELETE FROM agent_runtimes WHERE display_name='chat_test_local'")
        c2.execute("DELETE FROM runtime_credentials WHERE name LIKE 'chat_cred%'")
        db.commit()


def _local_engine_id():
    """确保存在健康本机 opencode 引擎（credential_id=0），返回其 id。"""
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode' AND healthy=1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('opencode','chat_test_local',0,1,1,'online','global','')")
        db.commit()
        return c.lastrowid


def _remote_engine_id():
    """创建远端引擎（credential_id>0），返回其 id。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'chat_cred%'")
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                  "VALUES ('h%s', 'chat_cred', 'chat-dev', 'global', 'tester', 'active')" % uuid.uuid4().hex)
        cid = c.lastrowid
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                  "VALUES ('chat_rt', 'chat_rt', ?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        db.commit()
        return c.lastrowid


def _fake_stream_agent(prompt, cwd, session_id="", **kw):
    yield ("step_start", "started")
    yield ("text", "联通成功")
    yield ("done", {"exit_code": 0, "text": "联通成功", "oc_session_id": "ses_x"})


def _new_conv(client):
    r = client.post("/api/chat/conversations", json={})
    return r.json()["conversation_id"]


def _sse_done_text(body):
    """从 SSE body 提取 done 事件的 text。"""
    for line in body.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[len("data: "):])
            if evt.get("type") == "done":
                return evt.get("text", "")
    return ""


# ====== 会话 CRUD ======


class TestConversations:
    def test_list_empty(self, _client):
        r = _client.get("/api/chat/conversations")
        assert r.status_code == 200 and r.json()["conversations"] == []

    def test_new_and_list(self, _client):
        cid = _new_conv(_client)
        assert cid
        r = _client.get("/api/chat/conversations")
        assert len(r.json()["conversations"]) == 1

    def test_delete_own(self, _client):
        cid = _new_conv(_client)
        r = _client.delete(f"/api/chat/conversations/{cid}")
        assert r.status_code == 200
        assert _client.delete(f"/api/chat/conversations/{cid}").status_code == 404

    def test_delete_other_user_404(self, _client):
        cid = _new_conv(_client)

        def _other():
            return {"id": 99002, "username": "other"}

        app.dependency_overrides[get_current_user] = _other
        try:
            assert _client.delete(f"/api/chat/conversations/{cid}").status_code == 404
        finally:
            app.dependency_overrides[get_current_user] = lambda: {"id": UID, "username": USERNAME}

    def test_list_messages(self, _client):
        cid = _new_conv(_client)
        with db_lock:
            db.cursor().execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
                                (cid, "user", "hi"))
            db.commit()
        r = _client.get(f"/api/chat/messages/{cid}")
        assert r.status_code == 200
        assert len(r.json()["messages"]) == 1
        assert _client.get("/api/chat/messages/nope").status_code == 404


# ====== POST /send ======


class TestSend:
    def test_conv_not_found_404(self, _client):
        r = _client.post("/api/chat/send", json={"message": "hi", "conversation_id": "nope"})
        assert r.status_code == 404

    def test_no_backend_reply(self, _client):
        with patch("src.api.routes.chat._get_chat_config", return_value={"backend": ""}):
            r = _client.post("/api/chat/send", json={"message": "hi"})
        assert r.status_code == 200
        assert "无可用引擎" in r.json()["reply"]

    def test_direct_mode_streams(self, _client):
        rid = _local_engine_id()
        cid = _new_conv(_client)
        with patch("src.api.routes.chat._get_chat_config",
                          return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = _client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        assert r.json()["reply"] == "联通成功"

    def test_task_conversation_uses_task_prompt(self, _client):
        rid = _local_engine_id()
        cid = "ctask-%s" % uuid.uuid4().hex[:4]
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO chat_conversations (id, user_id, repo_key, conv_type, issue_number) "
                      "VALUES (?, ?, 'gitcode:org/x', 'task', 5)", (cid, UID))
            db.commit()
        with patch("src.api.routes.chat._get_chat_config",
                          return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent), \
             patch("src.prompt_builder.build_task_chat_prompt", return_value="task-prompt") as m_build:
            _client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        m_build.assert_called()

    def test_queue_mode_enqueues(self, _client):
        rid = _remote_engine_id()
        cid = _new_conv(_client)
        with patch("src.api.routes.chat._get_chat_config",
                          return_value={"backend": "chat_rt", "runtime_id": rid}), \
             patch("src.runtime.service.enqueue_task", return_value="t1") as m_enq:
            r = _client.post("/api/chat/send", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        data = r.json()
        assert data["queued"] is True and data["task_id"] == "t1"
        assert m_enq.call_args.kwargs["runtime_id"] == rid


# ====== POST /send/stream ======


class TestSendStream:
    def test_no_backend_done_event(self, _client):
        cid = _new_conv(_client)
        with patch("src.api.routes.chat._get_chat_config", return_value={"backend": ""}):
            r = _client.post("/api/chat/send/stream", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        assert "无可用引擎" in _sse_done_text(r.text)

    def test_direct_streams_events(self, _client):
        rid = _local_engine_id()
        cid = _new_conv(_client)
        with patch("src.api.routes.chat._get_chat_config",
                          return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_agent", side_effect=_fake_stream_agent):
            r = _client.post("/api/chat/send/stream", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200
        assert "联通成功" in _sse_done_text(r.text)

    def test_stream_worker_persists_reply_and_new_session_even_if_disconnected(self, _client):
        """看护：即使 SSE 流被客户端提前掐断（event_stream 生成器被取消），agent_worker 的
        finally 仍会把回复落库 + 新 session 回写 conv（根治会话失效后新 session 永驻问题，
        见 chat-prompt-history-injection-design.md §5.3 / d8f0592b）。"""
        rid = _local_engine_id()
        cid = _new_conv(_client)
        # 预置一个"过期" session，模拟陈旧 conv（应被新 session 覆盖）
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE chat_conversations SET runtime_session_id='ses_stale_old' WHERE id=?", (cid,))
            db.commit()

        def _fake_with_new_session(prompt, cwd, session_id="", **kw):
            # 模拟 session 失效→retry→成功，产出新 session
            yield ("done", {"exit_code": 1, "text": "", "oc_session_id": session_id or ""})
            yield ("text", "重试后的回复内容")
            yield ("done", {"exit_code": 0, "text": "重试后的回复内容", "oc_session_id": "ses_brand_new"})

        with patch("src.api.routes.chat._get_chat_config",
                          return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_agent", side_effect=_fake_with_new_session):
            # 客户端在读 SSE 流前就 abort：仅发出请求即关闭连接（testclient 用 stream=True 断开前读第一段）
            with _client.stream("POST", "/api/chat/send/stream", json={"message": "hi", "conversation_id": cid}) as resp:
                assert resp.status_code == 200
                for _ in resp.iter_lines():  # 只消费开头几行后中断
                    break

        # agent_worker.finally 已在此前完成（async 后台线程）；等待其落库完成
        import time as _t
        for _ in range(50):
            with db_lock:
                row = db.cursor().execute(
                    "SELECT runtime_session_id FROM chat_conversations WHERE id=?", (cid,)).fetchone()
                msgs = db.cursor().execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (cid,)).fetchall()
                n_agent = db.cursor().execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND role='agent'", (cid,)).fetchone()[0]
            if row and row[0] == "ses_brand_new" and n_agent >= 1:
                break
            _t.sleep(0.1)
        # 断言：过期 session 被替换、回复已落库（即使流被掐断）
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_session_id FROM chat_conversations WHERE id=?", (cid,)).fetchone()
            final_msgs = db.cursor().execute(
                "SELECT content FROM chat_messages WHERE conversation_id=? AND role='agent' ORDER BY id", (cid,)).fetchall()
        assert row and row[0] == "ses_brand_new", f"new session not persisted: {row}"
        assert any("重试后的回复内容" in (m[0] or "") for m in final_msgs), f"reply not persisted: {final_msgs}"
        assert n_agent >= 1


# ====== 反馈 ======


class TestFeedback:
    def test_missing_event_404(self, _client):
        r = _client.post("/api/chat/feedback", json={"seq": 999999, "feedback_type": "create_issue"})
        assert r.status_code == 404

    def test_feedback_ok(self, _client):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, title, payload) VALUES (?,?,?,?)",
                      ("gitcode:org/x", "issue_opened", "t", "{}"))
            seq = c.lastrowid
            db.commit()
        r = _client.post("/api/chat/feedback", json={"seq": seq, "feedback_type": "create_issue"})
        assert r.status_code == 200
        data = r.json()
        assert data["deprecated"] is True
        assert data["a2a_event"]["type"] == "user_feedback"


# ====== Chat 配置 ======


class TestConfig:
    def test_get_config(self, _client):
        r = _client.get("/api/chat/config")
        assert r.status_code == 200
        data = r.json()
        assert "backend" in data and "_backends" in data and "_skills" in data and "im" in data

    def test_put_config_vod_conflict(self, _client):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES ('cli_conflict','vod_bot')")
            db.commit()
        r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "cli_conflict", "app_secret": "s"}}})
        assert r.status_code == 400
        assert "VOD" in r.json()["detail"]["message"]

    def test_put_config_other_user_occupied(self, _client):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES ('occ-1', 8888, 'cli_occ')")
            db.commit()
        r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "cli_occ", "app_secret": "s"}}})
        assert r.status_code == 400
        assert "已被其他用户" in r.json()["detail"]["message"]

    def test_put_config_skill_resolution(self, _client):
        sname = "chat_skill_%s" % uuid.uuid4().hex[:4]
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, is_active) "
                      "VALUES (?,?,'d','chat',?,'official','',1)", (sname, sname, sname))
            sid = c.lastrowid
            db.commit()
        r = _client.put("/api/chat/config", json={"skill": sname, "skill_scope": "official"})
        assert r.status_code == 200
        with db_lock:
            row = db.cursor().execute("SELECT skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (UID,)).fetchone()
        assert row and row[0] == sid

    def test_put_config_clear_im(self, _client):
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200
        with db_lock:
            row = db.cursor().execute("SELECT im_app_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (UID,)).fetchone()
        assert row and row[0] == ""


# ====== 内部辅助函数 ======


class TestHelpers:
    def test_build_chat_prompt(self):
        from src.prompt_builder import build_general_chat_prompt
        prompt = build_general_chat_prompt("帮我看看", "org/repo", [{"role": "user", "content": "x"}])
        assert "帮我看看" in prompt

    def test_resolve_token_refreshes_conv(self):
        from src.api.routes.chat import _resolve_gitcode_token
        conv = {"id": "c1", "user_gitcode_token": "old"}
        with patch("src.api.routes.chat.update_conversation") as m_upd:
            assert _resolve_gitcode_token(conv, "c1", "new_tok") == "new_tok"
        m_upd.assert_called_once_with("c1", user_gitcode_token="new_tok")
        # 无 user_token → 返回缓存
        assert _resolve_gitcode_token(conv, "c1", "") == "old"

    def test_resolve_token_fallback_repo_token(self):
        # 用户 token 与会话缓存均空 → 仓库 api token 兜底（401 修复）
        from src.api.routes.chat import _resolve_gitcode_token
        conv = {"id": "c1", "user_gitcode_token": ""}
        with patch("src.user.db.get_gitcode_token", return_value="repo_tok") as m_gt:
            assert _resolve_gitcode_token(conv, "c1", "", "owner/repo") == "repo_tok"
        m_gt.assert_called_once_with(repo_key="owner/repo")
        # 仓库 token 也为空 → 兜底为空串，不抛异常
        with patch("src.user.db.get_gitcode_token", return_value=""):
            assert _resolve_gitcode_token(None, "", "", "owner/repo") == ""

    def test_push_to_feishu_no_channel(self):
        from src.api.routes.chat import _push_reply_to_feishu
        asyncio.run(_push_reply_to_feishu({"id": "c1", "im_channel_id": ""}, {"id": 1, "username": "u"}, "text"))

    def test_push_to_feishu_with_channel(self):
        from src.api.routes.chat import _push_reply_to_feishu
        with db_lock:
            db.cursor().execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES ('push-1', 1, 'cli_push')")
            db.commit()
        try:
            bm = MagicMock()
            bm.get_bot.return_value = MagicMock()
            bm.api = MagicMock()
            bm.api.send_card_to_user = AsyncMock()
            with patch("src.api.main.bot_manager", bm), \
                 patch("src.api.routes.tasks._get_feishu_open_id", return_value="ou_1"):
                asyncio.run(_push_reply_to_feishu({"id": "c1", "im_channel_id": "oc_x"}, {"id": 1, "username": "u"}, "hi"))
            bm.api.send_card_to_user.assert_awaited_once()
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM chat_agent_session WHERE id='push-1'")
                db.commit()
