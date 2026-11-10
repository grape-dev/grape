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
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock, get_chat_config
from src.user import db as user_db

UID = 99001
USERNAME = "chat_tester"


def _inst(app_id):
    from src.im.engine_wiring import _connect, default_db_path
    conn = _connect(default_db_path())
    try:
        return conn.execute(
            "SELECT channel_type, status, config FROM grape_t_channel_installation WHERE legacy_app_id=?",
            (app_id,)).fetchone()
    finally:
        conn.close()


def _seed_inst(app_id, secret="s", uid=UID, scope="user", repo_key="", channel_type="feishu"):
    from src.im.engine_wiring import sync_installation
    return sync_installation(scope, owner_user_id=str(uid), repo_key=repo_key,
                             app_id=app_id, app_secret=secret, channel_type=channel_type)


def _clean_inst(*app_ids):
    from src.im.engine_wiring import _connect, default_db_path
    conn = _connect(default_db_path())
    try:
        for a in app_ids:
            conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (a,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
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
        c2.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (UID,))
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

        # 删除路由鉴权走 require_no_session_token → get_current_user_or_api_key，
        # 仅覆盖 get_current_user 不生效（fixture 的 owner 覆盖残留会放行 → 200）
        app.dependency_overrides[get_current_user] = _other
        app.dependency_overrides[get_current_user_or_api_key] = _other
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
             patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream_agent):
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
             patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream_agent), \
             patch("src.prompt_builder.build_issue_chat_prompt", return_value="task-prompt") as m_build:
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
             patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream_agent):
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
             patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_with_new_session):
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

    def test_get_config_filters_user_scope_engines(self, _client):
        """Issue #37：get_chat_config 个人引擎必须只显示当前用户 + active。

        构造 4 条 scope=user 凭据：当前用户 active / 当前用户 revoked / 他人 active / 他人 revoked，
        以及全局 active 凭据（不应出现在 _backends 的 user scope 列表中）。
        """
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'chat_cred%'")
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'chat_rt%'")
            rows = [
                ("h_u_act", "chat_cred_u_act", "chat-dev", "user", USERNAME, "active"),
                ("h_u_rev", "chat_cred_u_rev", "chat-dev", "user", USERNAME, "revoked"),
                ("h_o_act", "chat_cred_o_act", "chat-dev", "user", "other_user", "active"),
                ("h_o_rev", "chat_cred_o_rev", "chat-dev", "user", "other_user", "revoked"),
            ]
            for tok, name, dev, scope, owner, status in rows:
                c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                          "VALUES (?,?,?,?,?,?)", (tok, name, dev, scope, owner, status))
                cid = c.lastrowid
                c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, scope, owner_id, installed, healthy) "
                          "VALUES (?, ?, ?, 'online', 'user', '', 1, 1)", (name, name, cid))
            db.commit()

        r = _client.get("/api/chat/config")
        assert r.status_code == 200
        user_backends = [b for b in r.json()["_backends"] if b.get("scope") == "user"]
        names = [b.get("name") for b in user_backends]
        # 只应出现：当前用户 + active 的那条
        assert any("chat_cred_u_act" in n for n in names), f"缺少当前用户 active 引擎: {names}"
        assert not any("chat_cred_u_rev" in n for n in names), f"当前用户 revoked 不应出现: {names}"
        assert not any("chat_cred_o_act" in n for n in names), f"他人 active 不应出现: {names}"
        assert not any("chat_cred_o_rev" in n for n in names), f"他人 revoked 不应出现: {names}"

    def test_put_config_vod_conflict(self, _client):
        """A2 收尾：独占校验查 active 安装行——repo 轨已占用的 app_id 不能绑 Chat。"""
        try:
            assert _seed_inst("cli_conflict", scope="repo", repo_key="gitcode:org/rv", uid="")
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "cli_conflict", "app_secret": "s"}}})
            assert r.status_code == 400
            assert "VOD" in r.json()["detail"]["message"]
        finally:
            _clean_inst("cli_conflict")

    def test_put_config_other_user_occupied(self, _client):
        try:
            assert _seed_inst("cli_occ", uid=8888)
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "cli_occ", "app_secret": "s"}}})
            assert r.status_code == 400
            assert "已被其他用户" in r.json()["detail"]["message"]
        finally:
            _clean_inst("cli_occ")

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
        """Issue #431：body 无 im 键的普通保存不再清空 IM 字段（原断言的是缺陷行为，已修正）。"""
        app_id = "cli_keep_%s" % uuid.uuid4().hex[:6]
        try:
            assert _seed_inst(app_id, secret="sec")
            r = _client.put("/api/chat/config", json={})
            assert r.status_code == 200
            row = _inst(app_id)
            assert row and row[1] == "active", f"无 im 键保存不应把安装弄失效, got {row}"
        finally:
            _clean_inst(app_id)

    def test_put_config_unbind_cleans_im(self, _client):
        """Issue #294：解绑（app_id 置空）→ 安装置 revoked + 删渠道会话 + 映射对账。"""
        app_id = "cli_unbind_%s" % uuid.uuid4().hex[:6]
        try:
            assert _seed_inst(app_id, secret="sec")
            with db_lock:
                c = db.cursor()
                c.execute("INSERT OR REPLACE INTO chat_conversations (id, user_id, im_channel_id, title) VALUES (?,?,?,?)",
                          ("ubc-%s" % app_id, UID, app_id, "t"))
                c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, app_id) VALUES (?,?,?,?,?)",
                          (UID, USERNAME, "ou_unbind", "绑定人", app_id))
                db.commit()
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "", "app_secret": ""}}})
            assert r.status_code == 200, r.text
            with db_lock:
                c = db.cursor()
                conv = c.execute("SELECT COUNT(*) FROM chat_conversations WHERE im_channel_id=?", (app_id,)).fetchone()[0]
                um = c.execute("SELECT feishu_open_id, feishu_name, app_id FROM unified_user_mapping WHERE gitcode_user=?", (USERNAME,)).fetchone()
            assert _inst(app_id)[1] == "revoked", "解绑后安装行应置 revoked"
            assert conv == 0, "解绑后该渠道会话应被清理"
            assert um and tuple(um) == ("", "", ""), "解绑后 unified_user_mapping 应清空 open_id/name/app_id"
            r2 = _client.get("/api/chat/config")
            assert r2.json()["im"]["feishu"]["app_id"] == ""
            assert r2.json()["im"]["feishu"]["connected"] is False
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (app_id,))
                c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (USERNAME,))
                db.commit()
            _clean_inst(app_id)

    def test_put_config_unbind_then_rebind(self, _client):
        """Issue #294：绑定 → 解绑 → 再绑定（同一凭证）循环无残留。"""
        app_id = "cli_cycle_%s" % uuid.uuid4().hex[:6]
        try:
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec"}}})
            assert r.status_code == 200, r.text
            assert _inst(app_id)[1] == "active"
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "", "app_secret": ""}}})
            assert r.status_code == 200, r.text
            assert _inst(app_id)[1] == "revoked"
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec"}}})
            assert r.status_code == 200, r.text
            ct, status, cfg = _inst(app_id)
            assert status == "active", "重绑后安装行应回到 active"
            assert "sec" in (cfg or ""), "重绑后 secret 应快照进安装 config"
        finally:
            _clean_inst(app_id)


class TestHelpers:
    def test_build_chat_prompt(self):
        from src.prompt_builder import build_general_chat_prompt
        prompt = build_general_chat_prompt("帮我看看", "org/repo", [{"role": "user", "content": "x"}])
        assert "帮我看看" in prompt

    def test_resolve_token_returns_user_token(self):
        from src.api.routes.chat import _resolve_gitcode_token
        # Issue #344: no conv cache, user_token non-empty → return directly
        assert _resolve_gitcode_token("new_tok") == "new_tok"

    def test_resolve_token_fallback_repo_token(self):
        # user_token empty + repo_key → 走 token_service 策略链（R18.6.14 起）
        from types import SimpleNamespace

        from src.api.routes.chat import _resolve_gitcode_token
        with patch("src.token_service.resolve_repo_token",
                   return_value=SimpleNamespace(token="repo_tok")):
            assert _resolve_gitcode_token("", "owner/repo") == "repo_tok"
        # 策略链拿不到 token → 标记过期并返回空串，不抛异常
        with patch("src.token_service.resolve_repo_token",
                   return_value=SimpleNamespace(token="")), \
             patch("src.token_service.detect_and_mark_expired", return_value="NO_TOKEN_AVAILABLE"):
            assert _resolve_gitcode_token("", "owner/repo") == ""

    def test_push_to_feishu_no_channel(self):
        from src.api.routes.chat import _push_reply_to_feishu
        asyncio.run(_push_reply_to_feishu({"id": "c1", "im_channel_id": ""}, {"id": 1, "username": "u"}, "text"))

    def test_push_to_feishu_with_channel(self):
        """web→飞书推送：旧 IM 下线 M2 后改为读 grape_t_channel_installation + FeishuAsync 直发。"""
        from src.api.routes import chat as chat_mod
        from src.api.routes.chat import _push_reply_to_feishu
        from src.im.engine_wiring import revoke_installation, sync_installation
        from src.im.engine_wiring import default_db_path, run_startup_migration
        try:
            run_startup_migration(default_db_path())
        except Exception:
            pass  # 测试库可能缺 legacy 表（回填步骤会跳过），只要建出 grape_t_channel_installation 即可
        app_id = "cli_push_%s" % uuid.uuid4().hex[:6]
        assert sync_installation("user", owner_user_id="1", app_id=app_id, app_secret="s")
        try:
            fake_api = MagicMock()
            fake_api.send_card_to_user = AsyncMock()
            with patch("src.im.engine_wiring.feishu_api", return_value=fake_api), \
                 patch("src.api.issues_center._get_feishu_open_id", return_value="ou_1"):
                asyncio.run(_push_reply_to_feishu({"id": "c1", "im_channel_id": "oc_x"},
                                                  {"id": 1, "username": "u"}, "hi"))
            fake_api.send_card_to_user.assert_awaited_once()
        finally:
            revoke_installation("user", owner_user_id="1", app_id=app_id)
