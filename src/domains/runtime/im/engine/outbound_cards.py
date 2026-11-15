"""出站互动卡片台账（批 5）：outTrackId → 会话映射。

为什么需要它：钉钉互动卡片的按钮 params 是**模板里写死的固定值**，回调里也不带
conversationId。所以点按钮时只能拿 `outTrackId` 回来反查"这张卡是哪次发给哪个会话的"。

两层：
  - 进程内 dict（发送时写，命中率 100%，热路径不吃 DB）
  - DB 表 `grape_t_channel_outbound_card`（进程重启后回调仍能反查）
读取顺序：内存 → DB。写入只增不改（同一 outTrackId 不复用，见 cards.new_out_track_id）。

SQL 归属：该表归 **mgmt** 域（docs/weekly-11/table-owner.tsv），语句只在该域 public
留一份（`outbound_card_put_any` / `outbound_card_chat_row_any`）；本模块只提供「调用期
的进程库句柄」当执行上下文，不写 SQL、也不判断表归谁。
"""
from __future__ import annotations

import logging
import threading

from src.domains.mgmt import public as _mgmt_public

log = logging.getLogger(__name__)

# out_track_id -> {chat_id, chat_type, sender_id, installation_id}
_CACHE: dict[str, dict] = {}
_CACHE_MAX = 2000
_lock = threading.Lock()


def remember(out_track_id: str, *, chat_id: str, installation_id: str = "",
             chat_type: str = "p2p", sender_id: str = "",
             template_id: str = "") -> None:
    """发送成功后登记（进程内 + DB）。DB 失败只告警，不影响发送。"""
    if not out_track_id:
        return
    entry = {"chat_id": chat_id, "chat_type": chat_type, "sender_id": sender_id,
             "installation_id": installation_id, "template_id": template_id}
    with _lock:
        if len(_CACHE) >= _CACHE_MAX:
            for key in list(_CACHE)[: _CACHE_MAX // 2]:
                _CACHE.pop(key, None)
        _CACHE[out_track_id] = entry
    try:
        _save_sync(out_track_id, entry)
    except Exception as e:                     # 台账丢失只影响"重启后点旧卡"
        log.warning("[outbound-card] persist failed: %r", e)


def lookup_chat(out_track_id: str) -> str:
    """回调反查会话：内存优先，回落 DB。回调路径专用（调用方放线程里跑，勿堵事件循环）。"""
    if not out_track_id:
        return ""
    with _lock:
        hit = _CACHE.get(out_track_id)
    if hit:
        return str(hit.get("chat_id") or "")
    try:
        row = _load_sync(out_track_id)
    except Exception as e:
        log.warning("[outbound-card] lookup failed: %r", e)
        return ""
    if not row:
        return ""
    with _lock:
        _CACHE[out_track_id] = row
    return str(row.get("chat_id") or "")


# ---------------------------------------------------------------- DB 层

def _db_conn():
    """调用期进程库连接（`DEVOPS_DB_PATH` 每次调用现读），只当执行上下文用。

    连接口径与 IM 引擎一致（sqlite3 / MySQLCompatDB 双方言）；**不承载任何 SQL**——
    卡片台账归 mgmt，语句在该域 public。单库 / 测试拓扑下这条连接就是「本域库」，
    owner 域复用它执行并沿用调用方的 `commit()`；自有 DSN 拓扑下 owner 域忽略它、
    改走 mgmt 连接（忽略那一跳正是 1146 的修复）。
    `close()` 对共享单例是空操作，与改前逐字一致。
    """
    from src.domains.runtime.im.engine_wiring import default_db_path, _connect as _engine_connect
    return _engine_connect(default_db_path())


def _save_sync(out_track_id: str, entry: dict) -> None:
    conn = _db_conn()
    try:
        _mgmt_public.outbound_card_put_any(
            out_track_id,
            installation_id=entry.get("installation_id") or "",
            channel_type="dingtalk",
            channel_chat_id=entry.get("chat_id") or "",
            chat_type=entry.get("chat_type") or "p2p",
            sender_staff_id=entry.get("sender_id") or "",
            card_template_id=entry.get("template_id") or "",
            cursor=conn.cursor(),
        )
        conn.commit()
    finally:
        conn.close()


def _load_sync(out_track_id: str) -> dict | None:
    conn = _db_conn()
    try:
        row = _mgmt_public.outbound_card_chat_row_any(out_track_id, cursor=conn.cursor())
    finally:
        conn.close()
    if not row:
        return None
    return {"chat_id": row[0] or "", "chat_type": row[1] or "p2p",
            "sender_id": row[2] or "", "installation_id": row[3] or "",
            "template_id": row[4] or ""}


def _clear_cache() -> None:      # 测试用
    with _lock:
        _CACHE.clear()


__all__ = ["remember", "lookup_chat"]