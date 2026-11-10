"""飞书异步封装单元测试（src/im/feishu_async.py）。

覆盖：seen 消息去重持久化、BotState/BotMetrics/BotContext、GitCode 协作者拉取、
WS 消息解析（parse_ws_message / _parse_event）、ack 帧构造、BotManager 编排。
网络（aiohttp/lark）与 WS 连接用 mock，不触真实飞书。
"""
import asyncio
import json
import time
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.im import feishu_async as fa
from src.im import seen_store as ss  # 旧 IM 下线 M1：seen 去重持久化迁到中立模块
from src.im.adapters.feishu import wire as fw  # M1：WS 帧/事件解析辅助迁到适配器侧
from src.session_manager import db, db_lock


def _app_id():
    return "cli_test_" + uuid.uuid4().hex[:8]


# ====== seen 消息去重 ======


class TestSeenMsgIds:
    def _clean(self, app_id):
        with db_lock:
            db.cursor().execute("DELETE FROM bot_seen_messages WHERE app_id=?", (app_id,))
            db.commit()

    def test_persist_then_load(self):
        app = _app_id()
        try:
            ss._persist_seen_msg_id(app, "msg-1")
            ss._persist_seen_msg_id(app, "msg-1")  # 幂等
            ss._persist_seen_msg_id(app, "msg-2")
            loaded = ss._load_seen_msg_ids(app)
            assert loaded == {"msg-1", "msg-2"}
        finally:
            self._clean(app)

    def test_load_returns_empty_when_none(self):
        app = _app_id()
        assert ss._load_seen_msg_ids(app) == set()

    def test_cleanup_removes_expired(self):
        app = _app_id()
        try:
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES (?,?,?)",
                          (app, "old", time.time() - fa._SEEN_MSG_TTL - 100))
                c.execute("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES (?,?,?)",
                          (app, "new", time.time()))
                db.commit()
            ss._cleanup_seen_msg_ids(app)
            loaded = ss._load_seen_msg_ids(app)
            assert "old" not in loaded and "new" in loaded
        finally:
            self._clean(app)


# ====== 状态 / 上下文 ======


class TestBotStateContext:
    def test_bot_state_enum(self):
        assert fa.BotState.IDLE.value == "idle"
        assert fa.BotState.CONNECTED.value == "connected"
        assert fa.BotState.STOPPED.value == "stopped"

    def test_metrics_to_dict(self):
        m = fa.BotMetrics(messages_received=3, messages_sent=2, last_error="boom")
        d = m.to_dict()
        assert d["messages_received"] == 3 and d["messages_sent"] == 2
        assert d["last_error"] == "boom"

    def test_context_default_name(self):
        ctx = fa.BotContext(app_id="cli_abcdef123456", app_secret="s")
        assert ctx.name.startswith("chat_bot_")
        assert ctx.state == fa.BotState.IDLE
        assert ctx.max_concurrent == 20
        assert isinstance(ctx.metrics, fa.BotMetrics)

    def test_context_explicit_name_and_max_concurrent(self):
        ctx = fa.BotContext(app_id="a", app_secret="s", name="mybot", max_concurrent=3)
        assert ctx.name == "mybot"
        assert ctx.max_concurrent == 3

    def test_is_connected(self):
        ctx = fa.BotContext(app_id="a", app_secret="s")
        assert ctx.is_connected is False
        ctx.state = fa.BotState.CONNECTED
        assert ctx.is_connected is True

    def test_to_dict(self):
        ctx = fa.BotContext(app_id="a", app_secret="s", name="n")
        ctx.state = fa.BotState.RECONNECTING
        d = ctx.to_dict()
        assert d["app_id"] == "a" and d["name"] == "n"
        assert d["state"] == "reconnecting"
        assert "metrics" in d


# ====== GitCode 协作者拉取 ======


class TestFetchCollaborators:
    def test_single_page(self):
        payload = [{"username": "u1"}, {"username": "u2"}]

        def _resp(data):
            return MagicMock(read=MagicMock(return_value=json.dumps(data).encode()))

        with patch("urllib.request.urlopen", side_effect=[_resp(payload), _resp([])]):
            collabs = fa._fetch_gitcode_collaborators("owner", "repo", "tok")
        assert len(collabs) == 2
        assert collabs[0]["username"] == "u1"

    def test_empty_page_stops(self):
        with patch("urllib.request.urlopen", return_value=MagicMock(read=MagicMock(return_value=b"[]"))):
            collabs = fa._fetch_gitcode_collaborators("owner", "repo", "tok")
        assert collabs == []


# ====== WS 消息解析 ======


def _receive_payload(message_type="text", content='{"text":"hello"}', sender_open="ou1", chat_id="c1", at="", chat_type="group"):
    msg = {
        "type": "event",
        "event": {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {"message_id": "m1", "chat_id": chat_id, "message_type": message_type,
                            "content": content, "chat_type": chat_type},
                "sender": {"sender_id": {"open_id": sender_open}},
            },
        },
    }
    if at:
        msg["event"]["event"]["message"]["content"] = json.dumps({"text": "@_%s hello" % at})
    return json.dumps(msg)


class TestParseEvent:
    def test_non_message_returns_none(self):
        assert fw._parse_event({"header": {"event_type": "im.chat.updated"}}) is None

    def test_text_message(self):
        data = json.loads(_receive_payload())
        evt = data["event"]
        parsed = fw._parse_event(evt)
        assert parsed["type"] == "message" and parsed["text"] == "hello"
        assert parsed["chat_type"] == "group"

    def test_post_message(self):
        content = json.dumps({"content": [[{"tag": "text", "text": "a"}, {"tag": "text", "text": "b"}]]})
        data = json.loads(_receive_payload(message_type="post", content=content))
        parsed = fw._parse_event(data["event"])
        assert parsed["text"] == "a b"


class TestFrameHelpers:
    def test_get_header_found(self):
        h = [MagicMock(key="k1", value="v1"), MagicMock(key="k2", value="v2")]
        assert fw._get_header(h, "k2") == "v2"
        assert fw._get_header(h, "missing") == ""

    def test_build_ack_frame(self):
        frame = MagicMock()
        frame.SerializeToString.return_value = b"\x01\x02"
        out = fw._build_ack_frame(frame)
        frame.payload = json.dumps({"code": 200}).encode("utf-8")
        assert out == b"\x01\x02"
        assert json.loads(frame.payload) == {"code": 200}


# ====== BotManager ======


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data


class _HttpCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _api_with_session():
    api = fa.FeishuAsync()
    sess = MagicMock()
    api._get_session = AsyncMock(return_value=sess)
    return api, sess


def _ctx():
    return fa.BotContext(app_id="cli_h", app_secret="s", name="http")


class TestFeishuAsyncHttp:
    def test_get_tenant_token_cached(self):
        api, sess = _api_with_session()
        ctx = _ctx()
        ctx.token = "cached"
        ctx.token_expire = time.time() + 3600
        tok = asyncio.run(api.get_tenant_token(ctx))
        assert tok == "cached"
        sess.post.assert_not_called()

    def test_get_tenant_token_fetches_and_caches(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"tenant_access_token": "new_tok", "expire": 7200}))
        ctx = _ctx()
        assert asyncio.run(api.get_tenant_token(ctx)) == "new_tok"
        assert ctx.token == "new_tok"
        sess.post.assert_called_once()

    def test_get_tenant_token_error_sets_last_error(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"code": 999}))
        ctx = _ctx()
        assert asyncio.run(api.get_tenant_token(ctx)) == ""
        assert "token api error" in ctx.metrics.last_error

    def test_get_ws_endpoint_success(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"code": 0, "data": {"URL": "wss://x"}}))
        assert asyncio.run(api.get_ws_endpoint(_ctx())) == ("wss://x", 0)

    def test_get_ws_endpoint_error(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"code": 1}))
        ctx = _ctx()
        assert asyncio.run(api.get_ws_endpoint(ctx)) == ("", 1)
        assert "endpoint api error" in ctx.metrics.last_error

    def test_reply_text_success(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "rid1"}}))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            rid = asyncio.run(api.reply_text(_ctx(), "m1", "hi"))
        assert rid == "rid1"

    def test_reply_text_no_token(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="")):
            assert asyncio.run(api.reply_text(_ctx(), "m1", "hi")) == ""
        sess.post.assert_not_called()

    def test_reply_text_exception_counts_failed(self):
        api, sess = _api_with_session()
        sess.post.side_effect = RuntimeError("net")
        ctx = _ctx()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.reply_text(ctx, "m1", "hi")) == ""
        assert ctx.metrics.messages_failed == 1

    def test_reply_card_success(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "rid"}}))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.reply_card(_ctx(), "m1", "card")) == "rid"

    def test_reply_card_exception(self):
        api, sess = _api_with_session()
        sess.post.side_effect = RuntimeError("net")
        ctx = _ctx()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.reply_card(ctx, "m1", "card")) == ""
        assert ctx.metrics.messages_failed == 1

    def test_send_card_to_user_no_token(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="")):
            assert asyncio.run(api.send_card_to_user(_ctx(), "ou1", "card")) == ""
        sess.post.assert_not_called()

    def test_update_message_no_token(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="")):
            assert asyncio.run(api.update_message(_ctx(), "m1", "x")) is False
        sess.patch.assert_not_called()

    def test_add_reaction_no_token(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="")):
            assert asyncio.run(api.add_reaction(_ctx(), "m1")) is False
        sess.post.assert_not_called()

    def test_get_user_name_exception(self):
        api, sess = _api_with_session()
        sess.get.side_effect = RuntimeError("net")
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.get_user_name(_ctx(), "u1")) == ""

    def test_send_card_to_user_success(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "mid"}}))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.send_card_to_user(_ctx(), "ou1", "card")) == "mid"

    def test_send_card_to_user_with_actions(self):
        """feishu-notification-card-quick-actions-design.md：actions 参数追加 action 元素。"""
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "mid"}}))
        actions = [{"tag": "button", "text": {"tag": "plain_text", "content": "go"},
                    "value": {"cmd": "/task-open"}}]
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.send_card_to_user(_ctx(), "ou1", "card", actions=actions)) == "mid"
        req = sess.post.call_args
        payload = req[1]["json"]
        card = json.loads(payload["content"])
        assert card["elements"][-1]["tag"] == "action"
        assert card["elements"][-1]["actions"] == actions

    def test_send_card_to_user_no_actions(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "mid"}}))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            asyncio.run(api.send_card_to_user(_ctx(), "ou1", "card"))
        req = sess.post.call_args
        payload = req[1]["json"]
        card = json.loads(payload["content"])
        assert all(el.get("tag") != "action" for el in card["elements"])

    def test_send_card_to_user_template_fields_note(self):
        """通知卡格式优化：header 模板色 + 结构化字段 + 底部时间戳。"""
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({"data": {"message_id": "mid"}}))
        fields = [{"is_short": True, "text": {"tag": "lark_md", "content": "**📦 仓库**\nowner/repo"}}]
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            asyncio.run(api.send_card_to_user(
                _ctx(), "ou1", "body", title="❓ 需求待澄清", template="orange",
                fields=fields, note="发送时间：2026-10-11 14:30"))
        req = sess.post.call_args
        payload = req[1]["json"]
        card = json.loads(payload["content"])
        assert card["header"]["template"] == "orange"
        assert card["header"]["title"]["content"] == "❓ 需求待澄清"
        assert any(el.get("tag") == "div" and el.get("fields") == fields for el in card["elements"])
        assert card["elements"][-1]["tag"] == "note"
        assert card["elements"][-1]["elements"][0]["content"] == "发送时间：2026-10-11 14:30"

    def test_parse_card_action_passes_card_value(self):
        evt = {
            "event": {
                "operator": {"open_id": "ou_x"},
                "context": {"open_chat_id": "oc_x", "open_message_id": "om_x"},
                "action": {"value": {"cmd": "/task-open", "repo_key": "gitcode:a/b",
                                     "issue_number": "#7", "conv_id": "c1"}},
            },
        }
        parsed = fw._parse_card_action(evt)
        assert parsed["text"] == "/task-open"
        assert parsed["_card_value"]["repo_key"] == "gitcode:a/b"
        assert parsed["_card_value"]["conv_id"] == "c1"

    def test_update_message_true_on_200(self):
        api, sess = _api_with_session()
        sess.patch.return_value = _HttpCtx(_FakeResp({}, status=200))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.update_message(_ctx(), "m1", "x")) is True

    def test_update_message_false_on_non_200(self):
        api, sess = _api_with_session()
        sess.patch.return_value = _HttpCtx(_FakeResp({}, status=400))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.update_message(_ctx(), "m1", "x")) is False

    def test_add_reaction_true(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({}, status=200))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.add_reaction(_ctx(), "m1")) is True

    def test_add_reaction_false(self):
        api, sess = _api_with_session()
        sess.post.return_value = _HttpCtx(_FakeResp({}, status=403))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.add_reaction(_ctx(), "m1")) is False

    def test_get_chat_members_paginates(self):
        api, sess = _api_with_session()
        sess.get.side_effect = [
            _HttpCtx(_FakeResp({"data": {"items": [{"name": "张三", "member_id": "ou1"}],
                                          "page_token": "p2", "has_more": True}})),
            _HttpCtx(_FakeResp({"data": {"items": [{"name": "李四", "member_id": "ou2"}],
                                          "page_token": "", "has_more": False}})),
        ]
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            members = asyncio.run(api.get_chat_members(_ctx(), "oc1"))
        assert members == [{"name": "张三", "open_id": "ou1"}, {"name": "李四", "open_id": "ou2"}]

    def test_get_chat_members_no_token(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="")):
            assert asyncio.run(api.get_chat_members(_ctx(), "oc1")) == []
        sess.get.assert_not_called()

    def test_get_user_name_success(self):
        api, sess = _api_with_session()
        sess.get.return_value = _HttpCtx(_FakeResp({"data": {"user": {"name": "王五"}}}))
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.get_user_name(_ctx(), "u1")) == "王五"

    def test_get_user_name_no_user_id(self):
        api, sess = _api_with_session()
        with patch.object(api, "get_tenant_token", new=AsyncMock(return_value="tok")):
            assert asyncio.run(api.get_user_name(_ctx(), "")) == ""
