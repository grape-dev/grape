"""IM 回复长度上限（2026-11-19：2000 → 10000 字符）回归。

现象：IM（飞书/企微/微信/钉钉）里 agent 回复到 2000 字就被硬切，后面内容全丢。
根因：handler_async / vod_handler_async 各自写死 `[:2000]`。

本测试锁：
  1. 上限口径 = 10000（单一来源 reply_limits.MAX_REPLY_CHARS）；
  2. chat / VOD 两条回复链路都按该口径切片，源码里不再有 `[:2000]`；
  3. 飞书卡片请求体 30KB 上限 → 长正文按 6000 字符切块发多张卡片，且内容不丢、每张 <30KB。
"""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.agent import _backend_for_engine  # noqa: F401,E402
from src.domains.runtime.im import feishu_async  # noqa: E402
from src.domains.runtime.im.feishu_async import (  # noqa: E402
    BotContext, FeishuAsync, FEISHU_CARD_TEXT_CHARS, _card_text_parts,
)
from src.domains.runtime.im.reply_limits import MAX_REPLY_CHARS  # noqa: E402

FEISHU_CARD_MAX_BYTES = 30 * 1024  # 飞书卡片请求体上限（超了报 230025）


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        resp = _Resp({"code": 0, "data": {"message_id": f"om_{len(self.calls)}"}})

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _api(session):
    api = FeishuAsync()

    async def _token(_ctx):
        return "t-test"

    async def _sess():
        return session

    api._ensure_token = _token
    api._get_session = _sess
    return api


def test_reply_cap_is_10000():
    assert MAX_REPLY_CHARS == 10000


def test_both_reply_chains_use_shared_cap():
    """chat / VOD 两条链路都走共享常量，源码里不允许再写死 [:2000]。"""
    handler = open(os.path.join(ROOT, "src/domains/runtime/im/handler_async.py"), encoding="utf-8").read()
    vod = open(os.path.join(ROOT, "src/domains/runtime/im/vod_handler_async.py"), encoding="utf-8").read()
    assert "output[:_MAX_REPLY_CHARS]" in handler
    assert "output[:2000]" not in handler
    assert "reply[:MAX_REPLY_CHARS]" in vod
    assert "reply[:2000]" not in vod


def test_card_text_parts_no_content_loss():
    text = "\n".join(f"第{i}行：这是一段比较长的正文内容，用来验证分块不丢内容。" for i in range(400))
    assert len(text) > MAX_REPLY_CHARS
    parts = _card_text_parts(text)
    assert len(parts) >= 2
    assert all(len(p) <= FEISHU_CARD_TEXT_CHARS for p in parts)
    assert "\n".join(parts) == text  # 按行切 → 拼回去与原文一致


def test_card_text_parts_hard_splits_single_long_line():
    text = "x" * (FEISHU_CARD_TEXT_CHARS * 2 + 10)
    parts = _card_text_parts(text)
    assert all(len(p) <= FEISHU_CARD_TEXT_CHARS for p in parts)
    assert "".join(parts) == text


def test_short_text_single_part_unchanged():
    assert _card_text_parts("hello") == ["hello"]


@pytest.mark.asyncio
async def test_long_chat_reply_sent_as_multiple_cards_under_30kb():
    """10000 字回复 → 多张卡片，每张请求体 <30KB，且不丢内容。"""
    api = _api(_Session())
    sess = _Session()
    api._get_session = _mk_sess(sess)

    text = "中" * MAX_REPLY_CHARS  # 最坏情况：全中文，UTF-8 3 字节/字
    msg_id = await api.send_card_to_chat(BotContext(app_id="a", app_secret="s"),
                                         "oc_chat_1", text, title="Chat Agent")
    assert msg_id == "om_1"
    assert len(sess.calls) >= 2
    joined = ""
    for url, kwargs in sess.calls:
        assert "receive_id=" not in url and "receive_id_type=chat_id" in url
        content = kwargs["json"]["content"]
        assert len(content.encode("utf-8")) < FEISHU_CARD_MAX_BYTES, "卡片请求体超 30KB 会被飞书拒（230025）"
        card = json.loads(content)
        for el in card["elements"]:
            joined += (el.get("text") or {}).get("content", "")
    assert joined.replace("\n", "") == text
    assert "续" in json.loads(sess.calls[1][1]["json"]["content"])["header"]["title"]["content"]


def _mk_sess(sess):
    async def _s():
        return sess

    return _s


@pytest.mark.asyncio
async def test_long_vod_reply_split_by_reply_card():
    api = _api(_Session())
    sess = _Session()
    api._get_session = _mk_sess(sess)
    text = "y" * (FEISHU_CARD_TEXT_CHARS + 50)
    msg_id = await api.reply_card(BotContext(app_id="a", app_secret="s"), "om_in", text,
                                  title="VOD Agent")
    assert msg_id == "om_1"
    assert len(sess.calls) >= 2
    assert "".join(json.loads(kw["json"]["content"])["elements"][0]["text"]["content"]
                   for _u, kw in sess.calls).replace("\n", "") == text
    for _url, kwargs in sess.calls:
        assert len(kwargs["json"]["content"].encode("utf-8")) < FEISHU_CARD_MAX_BYTES