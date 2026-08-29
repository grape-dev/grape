"""Tests for chat handler (async version) routing logic."""
import pytest
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _make_ctx(app_id="cli_chat"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-test")


async def _run(text, parsed_extra=None, api=None, ctx=None):
    from src.im.handler_async import process_chat_async
    ctx = ctx or _make_ctx()
    api = api or AsyncMock()
    parsed = {
        "type": "message", "text": text, "chat_id": "chat_1",
        "message_id": "msg_1", "sender_id": "user_1", "sender_open_id": "ou_1",
        "chat_type": "group",
    }
    if parsed_extra:
        parsed.update(parsed_extra)
    await process_chat_async(ctx, api, parsed)
    return api


class TestProcessChatAsync:
    def test_non_message_returns(self):
        """Non-message type should return immediately."""
        import asyncio
        from src.im.handler_async import process_chat_async
        ctx = _make_ctx()
        api = AsyncMock()
        asyncio.run(process_chat_async(ctx, api, {"type": "event"}))
        api.reply_text.assert_not_called()
        api.reply_card.assert_not_called()

    def test_command_routing(self):
        """/command should route to async process_command."""
        import asyncio
        with patch("src.im.handler_async.process_command_async", new=AsyncMock(return_value="/status result")) as mock_pc:
            asyncio.run(_run("/status"))
            mock_pc.assert_called_once()

    def test_unbound_repo_shows_help(self):
        """Message with no bound repo should show binding help."""
        import asyncio
        with patch("src.im.handler_async.resolve_repo", return_value=""):
            with patch("src.im.handler_async.get_user_id_by_app_id", return_value=None):
                api = asyncio.run(_run("check issue"))
        assert api.reply_text.call_count >= 1
        args = api.reply_text.call_args[0]
        assert "绑定" in args[2] or "未绑定" in args[2]

    def test_bind_intent_detected(self):
        """Natural language bind intent should bind repo directly (no /bind cmd) + card confirm."""
        import asyncio
        from src.im.handler_async import db
        from src.session_manager import db_lock
        with db_lock:
            db.cursor().execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='chat_1'")
            db.commit()
        with patch("src.im.handler_async.detect_bind_intent", return_value="owner/repo"):
            with patch("src.im.handler_async._send_card_reply", new=AsyncMock(return_value=True)) as m_card:
                api = asyncio.run(_run("绑定到 owner/repo"))
                m_card.assert_called_once()
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id='chat_1'").fetchone()
        assert row and "owner/repo" in row[0]
        with db_lock:
            db.cursor().execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='chat_1'")
            db.commit()

    def test_clean_intent_called(self):
        """'清除会话缓存' should route to /clean command."""
        import asyncio
        with patch("src.im.handler_async.detect_clean_intent", return_value=True):
            with patch("src.im.handler_async.process_command_async", new=AsyncMock(return_value="✅ 已清除")) as mock_pc:
                asyncio.run(_run("清除会话缓存"))
                mock_pc.assert_called_once()


def _run_inner_execute():
    """驱动 _process_chat_inner_async 到执行分支。返回 api mock。

    执行模式由调用方通过 monkeypatch 设置 DEVOPS_EXECUTION_MODE。
    """
    import asyncio
    from src.im.handler_async import _process_chat_inner_async
    ctx = _make_ctx()
    api = AsyncMock()
    parsed = {
        "type": "message", "text": "测试执行", "chat_id": "chat_1",
        "message_id": "msg_1", "sender_id": "user_1", "sender_open_id": "ou_1",
    }
    asyncio.run(_process_chat_inner_async(
        ctx, api, parsed, "测试执行",
        repo_key="gitcode:Jason7602/test", repository="Jason7602/test",
        msg_id="msg_1", chat_id="chat_1", sess="sess-exec-test",
        history=[], oc_sid="", cc_sid="", is_new=False,
        bind_account="", exec_account="gitcode:Jason7602/test",
    ))
    return api


class TestChatHandlerQueueMode:
    """飞书 chat bot：非 direct 模式应入队由 RuntimeAgent 执行，direct 保持本地流式"""

    def test_queue_mode_enqueues_and_replies(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        with patch("src.runtime.service.enqueue_task", return_value="t_x") as m_enq, \
             patch("src.runtime.service.wait_task_terminal",
                   new=AsyncMock(return_value={"status": "completed", "exit_code": 0,
                                               "result_text": "HELLO_QUEUE",
                                               "usage_json": '{"session_id": "ses_q"}'})) as m_wait, \
             patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async") as m_consume:
            api = _run_inner_execute()

        m_enq.assert_called_once()
        assert m_enq.call_args.kwargs["runtime_id"] == 1, "入队应带解析出的 runtime_id"
        assert m_enq.call_args.kwargs["agent_session_id"] == "sess-exec-test"
        assert m_enq.call_args.kwargs["runtime_session_id"] == ""
        m_wait.assert_called_once_with("t_x")
        m_consume.assert_not_called(), "queue 模式不应本地流式执行"
        # 回复应为任务 result_text
        assert api.send_card_to_chat.call_count >= 1
        args = api.send_card_to_chat.call_args[0]
        assert "HELLO_QUEUE" in args[2]

    def test_direct_mode_streams_locally(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")

        async def _fake_consume(*a, **k):
            yield ("done", {"exit_code": 0, "text": "LOCAL_OK", "oc_session_id": "ses_new"})

        with patch("src.runtime.service.enqueue_task") as m_enq, \
             patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume):
            api = _run_inner_execute()

        m_enq.assert_not_called(), "direct 模式不应入队"
        args = api.send_card_to_chat.call_args[0]
        assert "LOCAL_OK" in args[2]
