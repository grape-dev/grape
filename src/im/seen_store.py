"""飞书（及多平台）入站去重的 seen 持久化（原 feishu_async.py 抽出）。

为什么独立成模块：旧 BotManager 已下线，但**新引擎**（FeishuChannel 的 seen 去重
持久化）和 BotContext 仍在用这三个函数。放在中立位置，避免"core 反向依赖 adapter"。

语义与实现均与原 feishu_async.py 保持一致（bot_seen_messages 表，TTL 2 天，载入上限 2000）。
"""
from __future__ import annotations

import time

from src.session_manager import db, db_lock

_SEEN_MSG_TTL = 86400 * 2
_SEEN_MSG_MAX_LOAD = 2000
_cutoff = None  # 兼容旧模块里的局部命名习惯（不使用）


def _load_seen_msg_ids(app_id: str) -> set:
    """载入该 bot 最近见过的 message_id（跨重启去重）。"""
    try:
        cutoff = time.time() - _SEEN_MSG_TTL
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT message_id FROM bot_seen_messages WHERE app_id=? AND seen_at>? "
                "ORDER BY seen_at DESC LIMIT ?",
                (app_id, cutoff, _SEEN_MSG_MAX_LOAD))
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()


def _persist_seen_msg_id(app_id: str, message_id: str) -> None:
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO bot_seen_messages (app_id, message_id, seen_at) VALUES (?,?,?)",
                (app_id, message_id, time.time()))
            db.commit()
    except Exception:
        pass


def _cleanup_seen_msg_ids(app_id: str) -> None:
    try:
        cutoff = time.time() - _SEEN_MSG_TTL
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM bot_seen_messages WHERE app_id=? AND seen_at<?", (app_id, cutoff))
            db.commit()
    except Exception:
        pass