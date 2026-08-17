"""VOD 命令卡片化测试（vod-navigation-commands-design.md V01-V10）。"""
import os
import sys
import asyncio
from unittest.mock import AsyncMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.feishu_async import BotContext
from src.session_manager import db, db_lock
from src.im.vod_handler_async import process_vod_async

CHAT = "vod_chat"
APP = "cli_vod"


def _ctx():
    return BotContext(app_id=APP, app_secret="s", bot_type="vod_bot", name="vod-test")


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM vod_conversations WHERE im_chat_id=?", (CHAT,))
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-vod-%' OR repo_key LIKE 'gitcode:owner/vod-repo-%'")
        db.commit()
    from src.im.handler_async import _clear_pending_selection
    _clear_pending_selection(CHAT)


def _seed(repos):
    _clean()
    with db_lock:
        c = db.cursor()
        for rk in repos:
            sid = "sess-vod-" + rk.split("/")[-1]
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, im_app_id, repo_gitcode_token, skill, status) "
                "VALUES (?,?,'vod',?, '','vod','idle')",
                (sid, rk, APP))
        db.commit()


def _run(text):
    api = AsyncMock()
    parsed = {"type": "message", "text": text, "chat_id": CHAT, "message_id": "m1",
              "sender_open_id": "ou_v", "sender_id": "u_v"}
    asyncio.run(process_vod_async(_ctx(), api, parsed))
    return api


def _card(api):
    assert api.reply_card.called, "应发卡片"
    return api.reply_card.call_args[0][2]


def _pending():
    from src.im.handler_async import _get_pending_selection
    return _get_pending_selection(CHAT)


def test_v01_repo_lists_vod_repos():
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    api = _run("/repo")
    text = _card(api)
    assert "vod-repo-a" in text and "vod-repo-b" in text
    assert _pending() is not None and _pending()["action"] == "vod_repo"
    _clean()


def test_v02_number_select_binds():
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    _run("/repo")
    api = _run("2")
    with db_lock:
        row = db.cursor().execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id=?", (CHAT,)).fetchone()
    assert row and "vod-repo-b" in (row[0] or "")
    assert _pending() is None
    assert "已绑定" in _card(api)
    _clean()


def test_v03_single_repo_auto_bind():
    _seed(["gitcode:owner/vod-repo-a"])
    api = _run("你好")
    texts = "".join(a[0][2] for a in api.reply_card.call_args_list)
    assert "自动绑定" in texts
    with db_lock:
        row = db.cursor().execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id=?", (CHAT,)).fetchone()
    assert row
    _clean()


def test_v04_multi_repo_auto_repo_card():
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    api = _run("你好")
    text = _card(api)
    assert "vod-repo-a" in text and "vod-repo-b" in text
    assert _pending() is not None and _pending()["action"] == "vod_repo"
    _clean()


def test_v05_zero_repo_prompt():
    _seed([])
    api = _run("你好")
    assert "未绑定仓库" in _card(api)
    _clean()


def test_v06_reply_zero_cancels():
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    _run("/repo")
    api = _run("0")
    assert _pending() is None
    assert "取消" in _card(api)
    _clean()


def test_v07_where_card():
    _seed(["gitcode:owner/vod-repo-a"])
    _run("/repo")
    _run("1")
    api = _run("/where")
    assert "vod-repo-a" in _card(api)
    _clean()


def test_v08_help_card():
    _seed(["gitcode:owner/vod-repo-a"])
    api = _run("/help")
    text = _card(api)
    assert "/repo" in text and "/where" in text and "/help" in text
    _clean()


def test_v09_clean_no_longer_special():
    _seed(["gitcode:owner/vod-repo-a"])
    api = _run("/clean")
    assert not api.reply_text.called        # 不再有旧文本回复
    assert api.reply_card.called            # 走普通流程（自动绑定卡片或提示）
    _clean()


def test_v10_chat_vod_pending_independent():
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    from src.im.handler_async import _set_pending_selection
    _set_pending_selection("other_chat", {"action": "chat", "items": [{"id": "x"}], "page": 1})
    _run("/repo")
    assert _pending() is not None and _pending()["action"] == "vod_repo"
    from src.im.handler_async import _get_pending_selection as _g
    assert _g("other_chat")["action"] == "chat"
    _clean()


def test_v11_rebind_updates_session_and_clears_runtime():
    """V11：已绑定群 /repo 重选仓库 → agent_session_id 更新 + runtime_session_id 清空（防上下文污染）"""
    _seed(["gitcode:owner/vod-repo-a", "gitcode:owner/vod-repo-b"])
    _run("/repo")
    _run("1")   # 绑 repo-a
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id=?", (CHAT,)).fetchone()
        assert row and "vod-repo-a" in row[0]
        c.execute("UPDATE vod_conversations SET runtime_session_id='ses_old_repo_a' WHERE im_chat_id=?", (CHAT,))
        db.commit()
    _run("/repo")
    api = _run("2")   # 换绑 repo-b
    with db_lock:
        row = c.execute("SELECT agent_session_id, runtime_session_id FROM vod_conversations WHERE im_chat_id=?", (CHAT,)).fetchone()
    assert row and "vod-repo-b" in row[0]
    assert row[1] == ""          # runtime_session_id 被清空
    assert "已绑定" in _card(api)
    _clean()


def test_v12_immediate_ok_reaction():
    """V12：进处理前加 add_reaction('OK') 即时反馈"""
    _seed(["gitcode:owner/vod-repo-a"])
    api = _run("你好")
    api.add_reaction.assert_called()
    assert api.add_reaction.call_args[0][2] == "OK"
    _clean()


def test_v13_no_engine_explicit_message():
    """V13：无引擎时给明确提示（非静默无回应）"""
    _seed(["gitcode:owner/vod-repo-a"])
    api = _run("你好")
    texts = "".join(a[0][2] for a in api.reply_card.call_args_list)
    assert "无可用引擎" in texts
    _clean()
