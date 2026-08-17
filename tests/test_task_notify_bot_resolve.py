"""_resolve_user_chat_bot 测试：notify 发送须用用户本人 chat bot（app 级 open_id，2026-08-17 静默失败修复）。"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.api.routes.tasks import _resolve_user_chat_bot


def _mk_bot(app_id, bot_type="chat_bot"):
    return SimpleNamespace(app_id=app_id, bot_type=bot_type)


class _FakeBM:
    def __init__(self, bots):
        self._bots = bots

    def get_all_bots(self):
        return dict(self._bots)


def _seed_user_chat(user_id, app_id):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES (?,?,?)",
                  (f"sess-cfg-{user_id}", user_id, app_id))
        db.commit()


def _clean(user_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE id=?", (f"sess-cfg-{user_id}",))
        db.commit()


def test_resolve_uses_user_own_bot():
    """用户有自己的 chat bot → 用本人 bot 发送（而非第一个 chat_bot）"""
    _seed_user_chat(9001, "cli_own")
    bm = _FakeBM({"cli_first": _mk_bot("cli_first"), "cli_own": _mk_bot("cli_own")})
    ctx = _resolve_user_chat_bot(bm, 9001)
    assert ctx.app_id == "cli_own"
    _clean(9001)


def test_resolve_no_own_bot_returns_none():
    """用户无本人 chat bot 配置 → 返回 None（不通知，不回落其它 bot）"""
    bm = _FakeBM({"cli_first": _mk_bot("cli_first"), "cli_own": _mk_bot("cli_own")})
    assert _resolve_user_chat_bot(bm, 9999) is None
    _clean(9999)


def test_resolve_none_bm():
    assert _resolve_user_chat_bot(None, 1) is None
