"""飞书 chat 动态回复统一流式（feishu-chat-stream-unified-design.md Step 1~3）测试。

对齐 tests/test_chat_handler.py 风格：AsyncMock 驱动 _process_chat_inner_async，
patch 隔离 DB/引擎/consume_stream_async；断言 task_id 写入、ChatTask 缓冲、飞书 update_message 节流。
"""
import pytest
import os
import sys
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


@pytest.fixture(autouse=True)
def _clear_chat_caches():
    """清除 ChatTaskManager 任务表 + 引擎缓存，防跨测试污染。"""
    try:
        from src.api.chat_task_manager import ChatTaskManager
        with ChatTaskManager._lock:
            ChatTaskManager._tasks.clear()
    except Exception:
        pass
    try:
        from src.runtime.engine_select import _resolve_local_cache
        _resolve_local_cache.clear()
    except Exception:
        pass
    yield
    try:
        from src.api.chat_task_manager import ChatTaskManager
        with ChatTaskManager._lock:
            ChatTaskManager._tasks.clear()
    except Exception:
        pass


def _make_ctx(app_id="cli_chat"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-test")


def _drive_inner(api=None, ctx=None, text="测试执行", monkeypatch=None, **kwargs):
    """驱动 _process_chat_inner_async 到直接执行，返回 (api, session_manager 可查用的 conv_id)。"""
    from src.im.handler_async import _process_chat_inner_async
    ctx = ctx or _make_ctx()
    api = api or AsyncMock()
    parsed = {
        "type": "message", "text": text, "chat_id": "chat_1",
        "message_id": "msg_1", "sender_id": "user_1", "sender_open_id": "ou_1",
    }
    asyncio.run(_process_chat_inner_async(
        ctx, api, parsed, text,
        repo_key=kwargs.pop("repo_key", "gitcode:Jason7602/test"),
        repository=kwargs.pop("repository", "Jason7602/test"),
        msg_id="msg_1", chat_id="chat_1", sess="sess-exec-test",
        history=[], oc_sid="", cc_sid="", is_new=False,
        bind_account="", exec_account="gitcode:Jason7602/test",
        **kwargs,
    ))
    return api


# ===================== helpers =====================

def _last_user_msg(task_id_key="task_id"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT content, task_id, generate_status FROM chat_messages WHERE role='user' ORDER BY id DESC LIMIT 1")
        r = c.fetchone()
    return {"content": r[0], "task_id": r[1], "generate_status": r[2]} if r else None


def _last_agent_msg():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT content, task_id, generate_status FROM chat_messages WHERE role='agent' ORDER BY id DESC LIMIT 1")
        r = c.fetchone()
    return {"content": r[0], "task_id": r[1], "generate_status": r[2]} if r else None


def _fake_consume_done(final="LOCAL_OK", exit_code=0, with_segments=True):
    """构造 consume_stream_async 的 fake：yield step_start/text/step_finish → done。"""
    async def _fake(*a, **k):
        if with_segments:
            yield ("step_start", "Step 1 started")
        yield ("text", "答")
        yield ("text", "案")
        if with_segments:
            yield ("step_finish", "Step 1 done")
        yield ("done", {"exit_code": exit_code, "text": final, "oc_session_id": "ses_new"})
    return _fake


# ===================== Step 1 =====================

class TestStep1TaskId:
    def test_direct_writes_user_and_agent_task_id(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.runtime.service.enqueue_task") as m_enq, \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            api = _drive_inner(monkeypatch=monkeypatch)

        m_enq.assert_not_called(), "direct 模式不应入队"
        u = _last_user_msg()
        a = _last_agent_msg()
        assert u and u["task_id"] and u["task_id"].startswith("chat_"), "user 消息需带 chat_ 前缀 task_id"
        assert u["generate_status"] == 1, "user 消息 generate_status=1（运行中）"
        assert a and a["task_id"] == u["task_id"], "agent 回复需与 user 消息同一 task_id"
        assert a["generate_status"] == 2, "成功 reply generate_status=2"
        assert "LOCAL_OK" in a["content"]

    def test_direct_failed_sets_generate_status_3(self, monkeypatch):
        """失败且无文本产出：不写 agent 消息（与现有 SKIP/空回复语义一致）；
        有文本但 exit≠0 时应 gen_status=3。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async",
                   side_effect=_fake_consume_done(final="SOME_OUTPUT", exit_code=1)):
            api = _drive_inner(monkeypatch=monkeypatch)
        a = _last_agent_msg()
        assert a and "SOME_OUTPUT" in a["content"]
        assert a["generate_status"] == 3, "失败 reply 有文本时应 generate_status=3"

    def test_remote_writes_agent_task_id(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        with patch("src.runtime.service.enqueue_task", return_value="t_x") as m_enq, \
             patch("src.runtime.service.wait_task_terminal",
                   new=AsyncMock(return_value={"status": "completed", "exit_code": 0,
                                               "result_text": "HELLO_QUEUE",
                                               "usage_json": '{"session_id": "ses_q"}'})) as m_wait, \
             patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"):
            api = _drive_inner(monkeypatch=monkeypatch)
        m_enq.assert_called_once()
        u = _last_user_msg()
        a = _last_agent_msg()
        assert u and u["task_id"].startswith("chat_")
        assert a and a["task_id"] == u["task_id"] and a["generate_status"] == 2
        assert "HELLO_QUEUE" in a["content"]


# ===================== Step 2 =====================

class TestStep2ChatTaskBuffer:
    def test_direct_events_land_in_chattask(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        from src.api.chat_task_manager import ChatTaskManager
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            api = _drive_inner(monkeypatch=monkeypatch)
        u = _last_user_msg()
        tid = u["task_id"]
        with ChatTaskManager._lock:
            task = ChatTaskManager._tasks.get(tid)
        assert task is not None, "ChatTask 应已注册"
        # 缓冲应含 text 增量事件与 done 终态（final_text）
        texts = [json.loads(ev) for ev in task.events if json.loads(ev).get("type") == "text"]
        assert any(("答" in t.get("text", "")) for t in texts), "text 事件应进缓冲"
        assert task.final_text == "LOCAL_OK", "done 应写入 final_text"
        assert task.status == "done", "外部驱动结束应为 done"

    def test_direct_no_duplicate_reply(self, monkeypatch):
        """direct 默认关闭 Step3：不发送占位卡、无增量更新，仅终态卡一次 + OK 表情受理。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            api = _drive_inner(monkeypatch=monkeypatch)
        # Step3 默认关闭 → 无占位 / 无增量，仅终态卡 1 次（绝无重复）
        assert api.send_card_to_user.call_count == 0, "默认不应发送占位卡"
        assert api.update_message.call_count == 0, "默认不应有增量更新"
        assert api.send_card_to_chat.call_count == 1
        args = api.send_card_to_chat.call_args[0]
        assert "LOCAL_OK" in args[2]


# ===================== Step 3（默认关闭；env FEISHU_CHAT_DYNAMIC_CARD=1 可开）=====================

def _enable_dynamic(monkeypatch):
    monkeypatch.setenv("FEISHU_CHAT_DYNAMIC_CARD", "1")


class TestStep3FeishuDynamicCardDisabledByDefault:
    def test_default_off_no_placeholder_no_update(self, monkeypatch):
        """默认关闭：direct 不发占位卡、不 update_message；仅终态单卡 + OK 表情受理恰一次。"""
        monkeypatch.delenv("FEISHU_CHAT_DYNAMIC_CARD", raising=False)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            api = _drive_inner(monkeypatch=monkeypatch)
        assert api.send_card_to_user.call_count == 0
        assert api.update_message.call_count == 0
        assert api.send_card_to_chat.call_count == 1
        args = api.send_card_to_chat.call_args[0]
        assert "LOCAL_OK" in args[2]
        assert api.add_reaction.call_count == 1, "OK 表情为唯一受理确认"
        assert api.add_reaction.call_args[0][2] == "OK"

    def test_no_ack_bubble_in_source(self):
        """不再有独立「⏳」受理气泡（reply_text(msg_id,'⏳')）——与 OK 表情重复；也不残留未撤回的占位。"""
        import inspect
        import src.im.handler_async as h
        src = inspect.getsource(h)
        assert 'reply_text(ctx, msg_id, "⏳")' not in src, "独立 ⏳ 气泡已移除，仅 OK 表情受理"
        assert "FEISHU_CHAT_DYNAMIC_CARD" in src, "Step3 开关需存在（默认关闭，env=1 可开）"


class TestStep3FeishuDynamicCardEnabled:
    def test_placeholder_then_update_then_terminal(self, monkeypatch):
        _enable_dynamic(monkeypatch)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            api = _drive_inner(monkeypatch=monkeypatch)
        # 占位卡片发送给 open_id（send_card_to_user(ctx, open_id, text)）
        assert api.send_card_to_user.call_count >= 1
        cast = api.send_card_to_user.call_args
        assert cast.args and len(cast.args) >= 2 and cast.args[1] == "ou_1"
        # text 增量 → update_message 至少被调用（step/text 事件）
        assert api.update_message.call_count >= 1, "应有节流增量 update_message"
        # 终态仍落 send_card_to_chat
        args = api.send_card_to_chat.call_args[0]
        assert "LOCAL_OK" in args[2]

    def test_update_message_failure_degrades(self, monkeypatch):
        _enable_dynamic(monkeypatch)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        api = AsyncMock()
        api.update_message.return_value = False  # 更新失败
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            _drive_inner(api=api, monkeypatch=monkeypatch)
        # 失败不中断：终态卡片照常
        assert api.send_card_to_chat.call_count == 1
        args = api.send_card_to_chat.call_args[0]
        assert "LOCAL_OK" in args[2]

    def test_long_inline_caps_and_terminal(self, monkeypatch):
        """超长增量达上限后不再 update_message，终态卡片含全文（开关下）。"""
        _enable_dynamic(monkeypatch)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        big = "x" * 2000

        async def _fake_long(*a, **k):
            for i in range(0, len(big), 500):
                yield ("text", big[i:i + 500])
            yield ("done", {"exit_code": 0, "text": big, "oc_session_id": "ses_new"})

        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_long):
            api = _drive_inner(monkeypatch=monkeypatch)
        assert api.send_card_to_chat.call_count == 1
        args = api.send_card_to_chat.call_args[0]
        assert len(args[2]) <= 2000, "终态卡片不超单条上限"
        assert "x" in args[2]

    def test_no_update_when_no_placeholder_id(self, monkeypatch):
        """send_card_to_user 未拿到 message_id → 跳过增量更新，只落终态卡片（开关下）。"""
        _enable_dynamic(monkeypatch)
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        api = AsyncMock()
        api.send_card_to_user.return_value = ""  # 拿不到 message_id
        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume_done()):
            _drive_inner(api=api, monkeypatch=monkeypatch)
        api.update_message.assert_not_called()
        assert api.send_card_to_chat.call_count == 1