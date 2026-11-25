"""未绑定引导卡的按钮流（绑定我 / 我是谁 / 两段式确认）。

背景：未绑定发送者只被身份层挡住，群里打字回复还要 @机器人 才会被送达；所以
「绑定」和「确认这是哪个账号」必须做成卡片按钮，否则未绑定用户永远走不到绑定完成。

覆盖：
  - 未绑定任意消息 → 回带按钮的引导卡（/绑定、/我是谁、/help），不写身份行；
  - 群消息 → 引导卡保留「打字要先 @我」的提示；
  - 点「绑定我」（cmd=/绑定）→ 只回确认卡（/确认绑定、/取消绑定），此时不得落库；
  - 点「确认绑定」→ 才调用身份落库原语；
  - 点「取消绑定」→ 不落库；
  - 「我是谁」→ 回未绑定说明。

独立文件：test_engine_wiring.py 在基线分支上已有既有失败（含 installation owner 回填不到的
夹具漂移），新用例混进去分不清归属；这里用 monkeypatch 把 owner/落库原语钉死，只验布线契约。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from test_engine_wiring import FakeApi, Recorder, _backfill, _drive, _legacy_tables, _msg
from src.domains.runtime.im import engine_wiring
from src.domains.runtime.im.channel.types import ChatType


class CardApi(FakeApi):
    """FakeApi + 主动 raw 卡片通道（按钮卡走这条）。"""

    def __init__(self):
        super().__init__()
        self.raw_cards: list[dict] = []

    async def send_card_raw_to_chat(self, ctx, chat_id, card, fallback_open_id=""):
        self.raw_cards.append(card)
        return "om_raw_card"


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "engine.db")
    conn = sqlite3.connect(path)
    _legacy_tables(conn)
    conn.execute(
        "INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) "
        "VALUES (101, '', 'app_chat_1', 'sec1')")
    conn.commit()
    _backfill(conn)                      # A2 Phase B：启动不再回填，夹具显式跑一次性回填
    inst_id = conn.execute(
        "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='app_chat_1'"
    ).fetchone()[0]
    conn.close()
    return path, inst_id


@pytest.fixture(autouse=True)
def _pin_installation(monkeypatch, db_path):
    """安装查找走 _mgmt_public（测试里查不到行 → owner 为空），这里钉成夹具里的那行。"""
    _, inst_id = db_path

    def _fake_lookup(db, app_id):
        if app_id != "app_chat_1":
            return None
        return {"installation_id": inst_id, "scope": "user", "owner_user_id": "101",
                "repo_key": "", "config": {"app_id": "app_chat_1"}, "status": "active"}

    monkeypatch.setattr(engine_wiring, "_lookup_installation_by_app_sync", _fake_lookup)


def _identity_rows(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT installation_id, platform_user_id, user_id "
        "FROM grape_t_channel_sender_identity").fetchall()
    conn.close()
    return rows


def _cmds(card: dict) -> list[str]:
    out = []
    for el in card.get("elements", []):
        for btn in el.get("actions", []) or []:
            out.append(str((btn.get("value") or {}).get("cmd", "")))
    return out


async def _run(db_path, mid, text, chat_type=ChatType.P2P):
    path, _ = db_path
    api = CardApi()
    engine, api = await _drive(path, Recorder(), api,
                               msg=_msg(mid, text, sender="ou_unknown", chat_type=chat_type))
    try:
        await asyncio.sleep(0.3)
        return api
    finally:
        await engine.stop()


async def test_unbound_message_gets_guide_card_with_buttons(db_path):
    api = await _run(db_path, "om_btn_1", "帮我看看这个需求")
    assert api.raw_cards, "未绑定消息必须回一张带按钮的引导卡"
    card = api.raw_cards[-1]
    assert _cmds(card) == ["/绑定", "/我是谁", "/help"]
    assert _identity_rows(db_path[0]) == []       # 引导不改绑定状态


async def test_unbound_group_message_keeps_at_hint(db_path):
    api = await _run(db_path, "om_btn_2", "在群里问一句", chat_type=ChatType.GROUP)
    assert api.raw_cards
    body = str(api.raw_cards[-1])
    assert "@我" in body and "点按钮" in body


@pytest.mark.parametrize("mid", ["om_btn_3", "om_btn_4"])
async def test_bind_word_only_asks_for_confirmation(db_path, mid, monkeypatch):
    """点「绑定我」只发确认卡，不落库。"""
    calls: list = []

    def _spy(*a, **kw):
        calls.append(a)
        return "101"

    monkeypatch.setattr(engine_wiring, "_bind_identity_sync", _spy)
    api = await _run(db_path, mid, "/绑定")
    assert api.raw_cards, "绑定词必须先回确认卡"
    assert _cmds(api.raw_cards[-1]) == ["/确认绑定", "/取消绑定"]
    assert calls == [], "第一步不得落库"
    assert _identity_rows(db_path[0]) == []


async def test_confirm_word_calls_bind_primitive_once(db_path, monkeypatch):
    calls: list = []

    def _spy(*a, **kw):
        calls.append(a)
        return "101"

    monkeypatch.setattr(engine_wiring, "_bind_identity_sync", _spy)
    api = await _run(db_path, "om_btn_5", "/确认绑定")
    assert len(calls) == 1, "确认后必须落库一次"
    assert any("已绑定" in text for _, text in api.cards)


async def test_confirm_word_reports_failure_for_repo_scope(db_path, monkeypatch):
    monkeypatch.setattr(engine_wiring, "_bind_identity_sync", lambda *a, **kw: "")
    api = await _run(db_path, "om_btn_6", "/确认绑定")
    assert any("绑定失败" in text for _, text in api.cards)


async def test_cancel_word_writes_nothing(db_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(engine_wiring, "_bind_identity_sync",
                        lambda *a, **kw: calls.append(a) or "101")
    api = await _run(db_path, "om_btn_7", "/取消绑定")
    assert calls == []
    assert any("已取消" in text for _, text in api.cards)


async def test_whoami_for_unbound_says_not_bound(db_path):
    api = await _run(db_path, "om_btn_8", "/我是谁")
    assert any("还没有绑定" in text for _, text in api.cards)
    assert _identity_rows(db_path[0]) == []