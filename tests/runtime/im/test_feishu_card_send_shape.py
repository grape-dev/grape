"""飞书主动卡片发送的请求形状回归（2026-11-18）。

现象：/repo、/chat 等命令的卡片回复在飞书里完全看不到；日志
`send_card_to_chat failed: code=230001 ... invalid receive_id`。
实测（同一 chat_id，同一卡片体）：
  - query 带 receive_id（旧写法）→ 230001 invalid receive_id
  - body 带 receive_id（新写法）→ 0 success
本测试锁死"receive_id 必须在请求体、query 只带 receive_id_type"。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
ROOT = os.path.abspath(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.agent import _backend_for_engine  # noqa: F401,E402
from src.domains.runtime.im import feishu_async  # noqa: E402
from src.domains.runtime.im.feishu_async import BotContext, FeishuAsync  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _Post:
    def __init__(self, payload):
        self.url = None
        self.kwargs = None
        self._payload = payload

    def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        resp = _Resp(self._payload)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False

        return _Ctx()


class _Session:
    def __init__(self, payload):
        self.last = _Post(payload)

    def post(self, url, **kwargs):
        return self.last.post(url, **kwargs)


@pytest.mark.asyncio
async def test_chat_card_puts_receive_id_in_body(monkeypatch):
    api = FeishuAsync()
    session = _Session({"code": 0, "data": {"message_id": "om_test"}})

    async def _token(_ctx):
        return "t-test"

    async def _sess():
        return session

    monkeypatch.setattr(api, "_ensure_token", _token)
    monkeypatch.setattr(api, "_get_session", _sess)

    msg_id = await api.send_card_to_chat(BotContext(app_id="a", app_secret="s"),
                                         "oc_chat_1", "hello", title="t")

    assert msg_id == "om_test"
    url = session.last.url
    payload = session.last.kwargs["json"]
    assert "receive_id=" not in url, f"query 里不能带 receive_id（飞书判 230001）: {url}"
    assert "receive_id_type=chat_id" in url
    assert payload["receive_id"] == "oc_chat_1"
    assert payload["msg_type"] == "interactive"


def test_source_has_no_query_receive_id():
    """源码级兜底：整个 feishu_async 不再出现 `?receive_id=` 的旧写法。"""
    import inspect
    src = inspect.getsource(feishu_async)
    assert "?receive_id=" not in src


@pytest.mark.asyncio
async def test_raw_card_proactive_send_keeps_content(monkeypatch):
    """主动发送 raw card：卡片体必须原样进 content（否则用户看到空卡片）。"""
    api = FeishuAsync()
    session = _Session({"code": 0, "data": {"message_id": "om_raw"}})

    async def _token(_ctx):
        return "t-test"

    async def _sess():
        return session

    monkeypatch.setattr(api, "_ensure_token", _token)
    monkeypatch.setattr(api, "_get_session", _sess)

    card = {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "待审核：请确认"}}],
            "header": {"title": {"tag": "plain_text", "content": "DevOps Bot"}}}
    msg_id = await api.send_card_raw_to_chat(BotContext(app_id="a", app_secret="s"),
                                             "oc_chat_1", card)

    assert msg_id == "om_raw"
    url = session.last.url
    payload = session.last.kwargs["json"]
    assert "receive_id=" not in url and "receive_id_type=chat_id" in url
    assert payload["receive_id"] == "oc_chat_1"
    body = json.loads(payload["content"])
    assert body["elements"][0]["text"]["content"] == "待审核：请确认"
