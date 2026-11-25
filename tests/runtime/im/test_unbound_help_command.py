"""未绑定身份时的帮助逃生口（点卡片「❓ 帮助」→ cmd=/help）。

回归背景：绑定引导卡底部由渠道自动追加「❓ 帮助」按钮（value={"cmd": "/help"}）。
未绑定发送者点这个按钮时，事件在 Router 的身份层被 unbound 分支接住；该逃生口原先
只认绑定词，/help 落不进去，于是又回一张一模一样的绑定引导卡 —— 用户看到的现象就是
"帮助按钮点了没反应"。

本文件独立于 test_engine_wiring.py：后者在基线分支上已有若干既有失败，把新用例混进去
会让"通过/失败"两张清单分不清归属。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from test_engine_wiring import (
    FakeApi, Recorder, _backfill, _drive, _legacy_tables, _msg,
)


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
    conn.close()
    return path


@pytest.mark.parametrize("mid,help_text", [
    ("om_help_a", "/help"),
    ("om_help_b", "/帮助"),
    ("om_help_c", "help"),
])
async def test_unbound_help_command_answers_with_help_text(db_path, mid, help_text):
    """未绑定发送者发帮助词 → 回帮助说明，不再回绑定引导卡，也不写身份行。"""
    rec = Recorder()
    api = FakeApi()
    engine, api = await _drive(db_path, rec, api,
                              msg=_msg(mid, help_text, sender="ou_unknown"))
    try:
        await asyncio.sleep(0.3)
        assert not any(c[0] in ("chat", "vod") for c in rec.calls)
        assert api.cards, "未绑定帮助词必须回一条消息"
        text = api.cards[-1][1]
        assert "绑定到本机器人" in text
        assert "绑定之后回复 /help" in text
        assert "请先绑定你的账号" not in text
        # 帮助不等于绑定：不得写 grape_t_channel_sender_identity
        conn = sqlite3.connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM grape_t_channel_sender_identity").fetchone()[0]
        conn.close()
        assert n == 0
    finally:
        await engine.stop()