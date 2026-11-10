"""_resolve_user_chat_bot 测试：notify 发送须用用户本人 chat bot（app 级 open_id）。

2026-08-17 静默失败修复的回归点保留；A2 M2 后凭证来源从 legacy BotManager 内存表
改为 grape_t_channel_installation（active 安装 + config 里的 app_id/app_secret 快照）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.routes.tasks import _resolve_user_chat_bot


def _seed_install(user_id: int, app_id: str) -> None:
    """种一条 active 的 user 轨飞书安装（与真实绑定态同构）。"""
    from src.im.engine_wiring import default_db_path, run_startup_migration, sync_installation
    run_startup_migration(default_db_path())
    assert sync_installation("user", owner_user_id=str(user_id), app_id=app_id, app_secret="sec")


def _clean_install(app_id: str) -> None:
    from src.im.engine_wiring import _connect, default_db_path
    conn = _connect(default_db_path())
    try:
        conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
        conn.commit()
    finally:
        conn.close()


def test_resolve_uses_user_own_bot():
    """用户有自己的 chat bot → 用本人 bot 发送（而非第一个 chat_bot）"""
    _seed_install(9001, "cli_own")
    try:
        ctx = _resolve_user_chat_bot(None, 9001)
        assert ctx is not None and ctx.app_id == "cli_own"
    finally:
        _clean_install("cli_own")


def test_resolve_no_own_bot_returns_none():
    """用户无本人 chat bot 安装 → 返回 None（不通知，不回落其它 bot）"""
    _seed_install(9002, "cli_other")
    try:
        assert _resolve_user_chat_bot(None, 9999) is None
    finally:
        _clean_install("cli_other")


def test_resolve_no_install_returns_none():
    """库里没有任何安装 → None（旧入参 bm 已废弃，传 None 也照新路径解析）"""
    assert _resolve_user_chat_bot(None, 1) is None