"""IM 通知 outbox（Issue #2261 B1）—— 跨进程/跨主控投递的落库与消费。

背景：`schedule_notify` 是 fire-and-forget —— 通知由「处理请求的那个进程」发出：
  - B/C 进程（event/worker）没有引擎 → 恒 engine_down；
  - 多主控下租约分散（每个安装一个 lease_owner），非租约持有者的那台发不出去；
  - 失败不重试、不落库，错过即永久丢失。

本模块做的事：
  1. **入队**（enqueue）：notify_user 解析目标后逐渠道写入 `grape_t_channel_outbound_card`
     （复用既有表，004 迁移补列）。`out_track_id = sha1(uid|kind|repo|issue|channel|inst)[:40]`
     主键天然幂等 —— 同一事件在 A/B/C 三进程并发触发只产生 1 行。
  2. **消费**（OutboxConsumer）：A 进程（与单进程入口）lifespan 各起一份协程，1s 轮询：
     只处理 `lease_owner == self.instance_id` 的行（读 grape_t_channel_lease，**只读**——
     租约获取/续期语义归 #869，本模块不改 bot_lease.py），复用 notify._deliver() 投递。
     投递前置 `pending → sending`（claim_for_sending 原子认领）：行在发送期间持有唯一
     投递权，与 notify_user 内联投递互斥（QA #1314 M1：双发送竞态根除）。
  3. **重试**：失败 attempt_count+1，按 1/2/4/8s 四档退避写 next_retry_at
     （1 首发 + 4 次退避重试 = 5 次尝试，第 5 次失败终态）；
     attempt_count>=5 或超过 expires_at（入队 +10min）置 failed/expired，不再无限重试。
  4. **可观测**（counts/recent_undelivered/list_rows）：供 /api/im/status、/api/im/outbound。

时间口径：与 bot_lease 一致 —— 列里存 Python UTC 字符串（"%Y-%m-%d %H:%M:%S"），
不用 SQL NOW()（MySQL 服务器本地时间 CST 与 UTC 差 8h 会让 TTL 判断错位）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

_TABLE = "grape_t_channel_outbound_card"

POLL_INTERVAL = 1.0          # 消费者轮询周期（秒）—— ≤30s 送达验收留足余量
BATCH_LIMIT = 50             # 单批最多处理行数
MAX_ATTEMPTS = 5             # 超过即 failed（1 首发 + 4 退避重试）
RETRY_BACKOFF = (1, 2, 4, 8)      # 秒；第 n 次失败后取 RETRY_BACKOFF[n-1]（截断）
TTL_SECONDS = 600            # 入队 +10min 过期（expires_at）

# 保留期（20261113 性能整改）：sent/failed/expired 超过 N 天物理删除，防止表无限增长。
# 0 = 不清理（仅排障期临时关）。逐轮最多删 PURGE_LIMIT 行，避免长事务。
RETENTION_DAYS = int(os.environ.get("GRAPE_IM_OUTBOX_RETENTION_DAYS", "7") or 7)
PURGE_LIMIT = 500
PURGE_EVERY_ROUNDS = 60      # 每 60 轮（≈1 分钟）清一次，不给 1s 轮询加固定开销

_TRACK_ID_LEN = 40           # out_track_id 截断长度（sha1 前 40 位）
_ERROR_LEN = 500             # last_error 落库截断长度
_REASON_LEN = 200            # 异常 reason 截断长度


def new_expiry(seconds: float = TTL_SECONDS) -> str:
    """公开口径：N 秒后的 UTC 过期时间串（入队方/测试统一使用，不碰私有 _future_str）。"""
    return _future_str(seconds)


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _future_str(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))


def _parse_ts(value: str) -> float:
    """'YYYY-MM-DD HH:MM:SS'（UTC）→ epoch 秒；空/坏值 → 0（视为已过期由调用方处理）。"""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def out_track_id(user_id: int, kind: str, repo_key: str, issue_number: int,
                 channel_type: str, installation_id: str) -> str:
    """跨进程幂等单号：sha1(uid|kind|repo|issue|channel|inst)[:40]。"""
    import hashlib
    raw = f"{int(user_id or 0)}|{kind or ''}|{repo_key or ''}|{int(issue_number or 0)}" \
          f"|{channel_type or ''}|{installation_id or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:_TRACK_ID_LEN]


def _conn():
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path
    return _connect(default_db_path())


# ---------------------------------------------------------------- 仓储（同步，供线程内调用）

def enqueue(*, out_track_id: str, installation_id: str, channel_type: str,
            channel_chat_id: str, payload: dict, expires_at: str,
            status: str = "pending", last_error: str = "",
            replace: bool = False) -> bool:
    """写入一行 outbox（默认 INSERT OR IGNORE：同单号已存在 → 不动它，返回 False）。

    payload：kind/text/repo_key/issue_number/conv_id/user_id 等投递所需的全部字段。
    no_binding 场景直接传 status='failed', last_error='no_binding'（一步落终态，
    消除「入队 pending → 再置 failed」两步写之间的崩溃窗口）。
    replace=True（notify-test 自检杠杆）：INSERT OR REPLACE，重复点击允许重发
    （dedupe=False 时使用；正常业务路径恒为 INSERT OR IGNORE 防重复投递）。
    """
    now = _now_str()
    verb = "INSERT OR REPLACE INTO" if replace else "INSERT OR IGNORE INTO"
    conn = _conn()
    try:
        cur = conn.execute(
            f"{verb} {_TABLE} "
            "(out_track_id, installation_id, channel_type, channel_chat_id, chat_type, "
            " payload, status, attempt_count, next_retry_at, expires_at, last_error, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (out_track_id, installation_id, channel_type, channel_chat_id or "", "p2p",
             json.dumps(payload or {}, ensure_ascii=False), status,
             now, expires_at, last_error, now, now),
        )
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    return {
        "out_track_id": str(row[0] or ""),
        "installation_id": str(row[1] or ""),
        "channel_type": str(row[2] or ""),
        "channel_chat_id": str(row[3] or ""),
        "status": str(row[4] or ""),
        "attempt_count": int(row[5] or 0),
        "next_retry_at": str(row[6] or ""),
        "expires_at": str(row[7] or ""),
        "last_error": str(row[8] or ""),
        "payload": json.loads(row[9] or "{}") if row[9] else {},
        "updated_at": str(row[10] or ""),
        "created_at": str(row[11] or ""),
    }


_COLS = ("out_track_id, installation_id, channel_type, channel_chat_id, status, "
         "attempt_count, next_retry_at, expires_at, last_error, payload, "
         "updated_at, created_at")


def lease_owner_of(installation_id: str, channel_type: str = "") -> str:
    """该安装当前租约的 owner（grape_t_channel_lease，只读）。空 = 无租约。"""
    key = f"{channel_type}:{installation_id}" if channel_type else installation_id
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(lease_owner,'') FROM grape_t_channel_lease WHERE lease_key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row and row[0] else ""
    except Exception as e:
        log.warning("[outbox] lease owner read failed inst=%s: %s", installation_id, e)
        return ""


def claim_for_sending(out_track_id: str) -> bool:
    """原子认领一行（pending → sending）：发送期间的行级互斥（QA #1314 M1）。

    rowcount>0 = 认领成功，本实例获得该行的唯一投递权；
    失败 = 已被内联投递/其他轮次认领（sending）或已终态——调用方不得再投。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='sending', updated_at=? "
            f"WHERE out_track_id=? AND status='pending'",
            (_now_str(), out_track_id))
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0
    finally:
        conn.close()


def claim_batch(instance_id: str, *, now: Optional[str] = None, limit: int = BATCH_LIMIT) -> list[dict]:
    """取本实例可消费的一批行（status='pending' AND 到期 AND 未过期）。

    归属判定：行的 (installation_id, channel_type) 对应租约 owner == instance_id。
    无租约的行**跳过**（租约可能在别的实例手里；本实例不抢——防重复发送）。
    返回的行已逐行置 sending（认领失败者剔除），
    投递方用完必须 mark_sent / mark_failed 收尾，否则由过期清扫置 expired。

    20261113 性能整改：整批共用一条连接、一次 commit（原先逐行开连接+commit，
    50 行实测 95ms → 现在 ~2ms）。原子性不变：仍是逐行 `UPDATE ... WHERE status='pending'`，
    以 rowcount 判定认领成功。
    """
    now = now or _now_str()
    rows = _pending_rows(now, limit)
    if not rows:
        return []
    out: list[dict] = []
    owner_cache: dict[str, str] = {}
    conn = _conn()
    try:
        for row in rows:
            inst, ctype = row["installation_id"], row["channel_type"]
            lkey = f"{inst}:{ctype}"
            owner = owner_cache.get(lkey)
            if owner is None:
                owner = lease_owner_of(inst, ctype)
                owner_cache[lkey] = owner
            if owner != instance_id:
                continue
            if _parse_ts(row["expires_at"]) <= time.time():
                conn.execute(
                    f"UPDATE {_TABLE} SET status='expired', updated_at=? "
                    "WHERE out_track_id=? AND status IN ('pending','sending')",
                    (now, row["out_track_id"]))
                continue
            cur = conn.execute(
                f"UPDATE {_TABLE} SET status='sending', updated_at=? "
                "WHERE out_track_id=? AND status='pending'",
                (now, row["out_track_id"]))
            if (getattr(cur, "rowcount", 0) or 0) == 0:
                continue          # 已被内联投递/其他轮次认领
            out.append(row)
            if len(out) >= limit:
                break
        conn.commit()
    finally:
        conn.close()
    return out


def _pending_rows(now: str, limit: int) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {_COLS} FROM {_TABLE} "
            "WHERE status='pending' AND (next_retry_at='' OR next_retry_at<=?) "
            "ORDER BY created_at LIMIT ?", (now, int(limit))).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows or []]


def mark_sent(out_track_id: str) -> None:
    """sending → sent（attempt_count 不动：那是失败计数，不随成功递增——QA #1314 m3）。

    单行版（内联投递路径用）；消费者一轮 50 行走 `mark_sent_many`（一次 commit）。
    """
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE {_TABLE} SET status='sent', last_error='', updated_at=? "
            f"WHERE out_track_id=? AND status='sending'",
            (_now_str(), out_track_id))
        conn.commit()
    finally:
        conn.close()


def mark_failed(out_track_id: str, reason: str) -> str:
    """失败：退避写 next_retry_at；attempt_count>=5 → failed。返回新 status。

    单行版（内联投递路径用）；消费者批量收尾用 `mark_failed_many`（同语义、单 commit）。

    乐观锁收窄写窗口（QA #1314 m6）：UPDATE 带 status IN ('pending','sending')，
    行已被并发方（API retry 置 pending/attempt=0、或已终态）改变时 rowcount=0 →
    放弃本次写入并返回 ''（调用方记日志，下一轮重算），不覆盖并发方的结果。
    """
    row = _get(out_track_id)
    if not row:
        return ""
    attempts = int(row.get("attempt_count", 0)) + 1
    new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    backoff = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)] if new_status == "pending" else 0
    next_retry = _future_str(backoff) if new_status == "pending" else ""
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status=?, attempt_count=?, next_retry_at=?, "
            f"last_error=?, updated_at=? WHERE out_track_id=? "
            f"AND status IN ('pending','sending') AND attempt_count=?",
            (new_status, attempts, next_retry, str(reason or "send_failed")[:_ERROR_LEN],
             _now_str(), out_track_id, int(row.get("attempt_count", 0))))
        conn.commit()
        if (getattr(cur, "rowcount", 0) or 0) == 0:
            return ""
        return new_status
    finally:
        conn.close()


def mark_sent_many(tracks: list[str]) -> int:
    """批量 sending → sent（一条 SQL、一次 commit；消费者一轮收尾用）。

    20261113 性能整改：50 行逐行 commit 实测 94ms → 一条 IN 更新 ~2ms。
    返回实际变更行数。attempt_count 不动（失败计数语义不变）。
    """
    tracks = [t for t in (tracks or []) if t]
    if not tracks:
        return 0
    conn = _conn()
    try:
        q = ",".join("?" * len(tracks))
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='sent', last_error='', updated_at=? "
            f"WHERE out_track_id IN ({q}) AND status='sending'",
            (_now_str(), *tracks))
        conn.commit()
        return int(getattr(cur, "rowcount", 0) or 0)
    finally:
        conn.close()


def mark_failed_many(items: list[tuple[str, str]]) -> dict:
    """批量失败收尾：一次读 attempt_count、单连接单 commit，逐行算退避。

    语义与 mark_failed 一致（乐观锁 attempt_count 匹配、>=MAX_ATTEMPTS 落 failed）。
    items = [(out_track_id, reason)]。返回 {track: 新状态或 ''（未写入）}。
    """
    items = [(t, r) for t, r in (items or []) if t]
    if not items:
        return {}
    tracks = [t for t, _ in items]
    out: dict = {}
    conn = _conn()
    try:
        q = ",".join("?" * len(tracks))
        rows = conn.execute(
            f"SELECT out_track_id, attempt_count FROM {_TABLE} "
            f"WHERE out_track_id IN ({q})", tracks).fetchall()
        old = {str(r[0]): int(r[1] or 0) for r in rows or []}
        now = _now_str()
        for track, reason in items:
            if track not in old:
                out[track] = ""
                continue
            attempts = old[track] + 1
            new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            backoff = (RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
                       if new_status == "pending" else 0)
            next_retry = _future_str(backoff) if new_status == "pending" else ""
            cur = conn.execute(
                f"UPDATE {_TABLE} SET status=?, attempt_count=?, next_retry_at=?, "
                "last_error=?, updated_at=? WHERE out_track_id=? "
                "AND status IN ('pending','sending') AND attempt_count=?",
                (new_status, attempts, next_retry,
                 str(reason or "send_failed")[:_ERROR_LEN], now, track, old[track]))
            out[track] = new_status if (getattr(cur, "rowcount", 0) or 0) else ""
        conn.commit()
        return out
    finally:
        conn.close()


def purge_old_rows(retention_days: Optional[int] = None,
                   limit: int = PURGE_LIMIT) -> int:
    """删除超过保留期的终态行（sent/failed/expired），返回删除行数。

    20261113 性能整改（防线二）：此前 outbox 只标记终态、从不删除，
    万人级折算 30 天 1.8M / 90 天 5.4M 行 → 状态接口随行数线性变慢。
    保留期取 GRAPE_IM_OUTBOX_RETENTION_DAYS（默认 7 天，0=不清理）；
    每轮最多删 limit 行（先取 id 再按主键删，两方言通用），避免长事务锁表。
    pending/sending 永不删除（那是还要投的行）。
    """
    days = RETENTION_DAYS if retention_days is None else int(retention_days)
    if days <= 0 or limit <= 0:
        return 0
    cutoff = _future_str(-days * 86400)
    conn = _conn()
    try:
        ids = [r[0] for r in conn.execute(
            f"SELECT out_track_id FROM {_TABLE} "
            "WHERE status IN ('sent','failed','expired') "
            "AND updated_at<>'' AND updated_at<? "
            "ORDER BY updated_at LIMIT ?", (cutoff, int(limit))).fetchall()]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = conn.execute(f"DELETE FROM {_TABLE} WHERE out_track_id IN ({q})", ids)
        conn.commit()
        return int(getattr(cur, "rowcount", 0) or 0)
    finally:
        conn.close()


def expire_overdue(now: Optional[str] = None) -> int:
    """把过期仍未 sent 的 pending/sending 行批量置 expired（消费者每轮顺手做）。

    比较用 <=（expires_at 等于当前秒即视为到期，避免整秒边界漏判）。
    """
    now = now or _now_str()
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='expired', updated_at=? "
            "WHERE status IN ('pending','sending') AND expires_at<>'' AND expires_at<=?",
            (now, now))
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0)
    finally:
        conn.close()


def mark_failed_no_attempt(out_track_id: str, reason: str) -> bool:
    """置 failed 但不计 attempt/退避（no_binding / offline 这类非发送失败的终态）。

    允许从 pending / sending 两态落终态（QA #1314 m2：去重路径回写不再被状态挡住）。
    返回是否实际写入。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='failed', last_error=?, updated_at=? "
            f"WHERE out_track_id=? AND status IN ('pending','sending')",
            (str(reason or "")[:_ERROR_LEN], _now_str(), out_track_id))
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0
    finally:
        conn.close()


def _get(out_track_id: str) -> Optional[dict]:
    conn = _conn()
    try:
        row = conn.execute(
            f"SELECT {_COLS} FROM {_TABLE} WHERE out_track_id=?",
            (out_track_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def get_row(out_track_id: str) -> Optional[dict]:
    """公开读一行（API /retry 与测试用）。"""
    return _get(out_track_id)


def release_sending(out_track_id: str) -> bool:
    """sending → pending（内联投递 offline 中止时把行还给租约持有实例）。

    幂等：行不在 sending（已终态/未被认领）时不写。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='pending', updated_at=? "
            f"WHERE out_track_id=? AND status='sending'",
            (_now_str(), out_track_id))
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0
    finally:
        conn.close()


def requeue_for_retry(out_track_id: str) -> bool:
    """failed/expired → pending（手动重发入口；重置退避与 TTL）。"""
    row = _get(out_track_id)
    if not row or row.get("status") not in ("failed", "expired"):
        return False
    now = _now_str()
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET status='pending', attempt_count=0, next_retry_at='', "
            f"expires_at=?, updated_at=? WHERE out_track_id=? "
            f"AND status IN ('failed','expired')",
            (_future_str(TTL_SECONDS), now, out_track_id))
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0
    finally:
        conn.close()


def counts() -> dict:
    """按 status 计数（/api/im/status outbox 段）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM {_TABLE} GROUP BY status").fetchall()
    except Exception as e:
        log.warning("[outbox] counts failed: %s", e)
        return {}
    finally:
        conn.close()
    out = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "expired": 0}
    for status, n in rows or []:
        out[str(status)] = int(n)
    return out


def recent_undelivered(limit: int = 20) -> list[dict]:
    """最近 N 条未送达（倒序）—— /api/im/status 暴露给任务中心。

    20261113 性能整改：过滤条件由 `status<>'sent'` 收窄成显式四态 IN 列表。
    原写法在 sent 占 99% 的表上等于全表扫描（200k 行实测 22ms、30 天后外推 199ms）；
    收窄后命中 (status, updated_at) 索引，成本只随「未送达行数」走，与总行数无关。
    """
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {_COLS} FROM {_TABLE} WHERE status IN "
            "('pending','sending','failed','expired') "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception as e:
        log.warning("[outbox] recent_undelivered failed: %s", e)
        return []
    finally:
        conn.close()
    out = []
    for row in rows or []:
        d = _row_to_dict(row)
        p = d.pop("payload", {}) or {}
        out.append({
            "out_track_id": d["out_track_id"],
            "kind": str(p.get("kind", "")),
            "user_id": p.get("user_id", 0),
            "repo_key": str(p.get("repo_key", "")),
            "issue_number": p.get("issue_number", 0),
            "channel_type": d["channel_type"],
            "installation_id": d["installation_id"],
            "status": d["status"],
            "reason": d["last_error"],
            "attempt_count": d["attempt_count"],
            "next_retry_at": d["next_retry_at"],
            "expires_at": d["expires_at"],
            "updated_at": d["updated_at"],
        })
    return out


def list_rows(status: str = "", limit: int = 50) -> list[dict]:
    """出站明细查询（/api/im/outbound）。status 空 = 全部。"""
    limit = max(1, min(int(limit or 50), 200))
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                f"SELECT {_COLS} FROM {_TABLE} WHERE status=? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLS} FROM {_TABLE} "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (limit,)).fetchall()
    except Exception as e:
        log.warning("[outbox] list_rows failed: %s", e)
        return []
    finally:
        conn.close()
    out = []
    for row in rows or []:
        d = _row_to_dict(row)
        p = d.pop("payload", {}) or {}
        d["kind"] = str(p.get("kind", ""))
        d["user_id"] = p.get("user_id", 0)
        d["repo_key"] = str(p.get("repo_key", ""))
        d["issue_number"] = p.get("issue_number", 0)
        d["conv_id"] = str(p.get("conv_id", ""))
        d["text"] = str(p.get("text", ""))
        out.append(d)
    return out


# ---------------------------------------------------------------- 消费者

class OutboxConsumer:
    """A 进程 / 单进程入口的 outbox 消费协程。

    用法（lifespan）：

        consumer = OutboxConsumer(engine)
        task = asyncio.create_task(consumer.run())
        ...
        await consumer.stop()          # cancel + await

    引擎为 None（未启动/已停）→ 本轮跳过（不置失败：那不是行的问题）。
    """

    def __init__(self, engine, instance_id: str = "", *, poll_interval: float = POLL_INTERVAL):
        self._engine = engine
        self.instance_id = instance_id or getattr(engine, "instance_id", "") or ""
        self._poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    def set_engine(self, engine) -> None:
        """引擎句柄可替换（mode 切换重建引擎时消费者复用，instance_id 跟随新引擎）。"""
        self._engine = engine
        self.instance_id = getattr(engine, "instance_id", "") or self.instance_id

    async def run(self) -> None:
        log.info("[outbox] consumer started (instance=%s)", self.instance_id)
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[outbox] consumer round failed: %s", e)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
        log.info("[outbox] consumer stopped")

    async def run_once(self) -> int:
        """处理一轮（公开给测试：不依赖 loop 计时）。返回处理的行数。

        20261113 性能整改：投递结果先收集、收尾落库批量写（mark_sent_many /
        mark_failed_many），把 50 行 × 2 次 commit 压成 2 次；另每
        PURGE_EVERY_ROUNDS 轮清一次过期终态行（purge_old_rows）。
        """
        self._round = getattr(self, "_round", 0) + 1
        expire_overdue()
        if PURGE_EVERY_ROUNDS > 0 and self._round % PURGE_EVERY_ROUNDS == 0:
            try:
                deleted = await asyncio.to_thread(purge_old_rows)
                if deleted:
                    log.info("[outbox] purged %d delivered rows (retention=%dd)",
                             deleted, RETENTION_DAYS)
            except Exception as e:
                log.warning("[outbox] purge failed: %s", e)
        engine = self._engine
        if engine is None:
            # 引擎句柄失效自愈：每轮从全局注册点刷新（模式翻转重建引擎后，
            # 即使外部忘了 set_engine，消费者也能拿到新引擎+新 instance_id）。
            engine = _resolve_engine_lazy()
            if engine is None:
                return 0
            self._engine = engine
            self.instance_id = getattr(engine, "instance_id", "") or self.instance_id
        instance_id = self.instance_id or getattr(engine, "instance_id", "")
        if not instance_id:
            return 0
        rows = await asyncio.to_thread(claim_batch, instance_id)
        if not rows:
            return 0
        from src.domains.runtime.im.notify import _deliver, build_cards_from_payload
        sent: list[str] = []
        failed: list[tuple[str, str]] = []
        degraded_marks: list[dict] = []
        for row in rows:
            payload = row.get("payload") or {}
            chat_id = row.get("channel_chat_id") or ""
            target = {"installation_id": row["installation_id"],
                      "channel_type": row["channel_type"]}
            card, text_card = build_cards_from_payload(payload)
            try:
                ok, degraded, reason = await _deliver(engine, target, chat_id, card, text_card)
            except Exception as e:
                ok, degraded, reason = False, False, repr(e)[:_REASON_LEN]
            if ok:
                sent.append(row["out_track_id"])
                if degraded:
                    degraded_marks.append({"chat_id": chat_id,
                                           "conv": payload.get("conv_id") or "",
                                           "repo_key": payload.get("repo_key", ""),
                                           "issue_number": int(payload.get("issue_number") or 0)})
            else:
                failed.append((row["out_track_id"], reason or "send_failed"))

        # ---- 批量收尾（本批一次 commit，不再逐行）
        if sent:
            await asyncio.to_thread(mark_sent_many, sent)
        if failed:
            statuses = await asyncio.to_thread(mark_failed_many, failed)
            for track, reason in failed:
                log.info("[outbox] deliver failed track=%s reason=%s -> %s",
                         track, reason, statuses.get(track, ""))
        for m in degraded_marks:
            try:
                from src.domains.runtime.im.notify import _register_numbered_reply
                if m["conv"]:
                    await asyncio.to_thread(
                        _register_numbered_reply, m["chat_id"], m["conv"],
                        m["repo_key"], m["issue_number"])
            except Exception:
                pass
        return len(rows)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def spawn(self) -> asyncio.Task:
        """lifespan 用：起协程并登记（stop 时 cancel+await）。"""
        self._stopped.clear()
        self._task = asyncio.get_running_loop().create_task(self.run())
        return self._task


def _resolve_engine_lazy():
    try:
        from src.domains.runtime.im.notify import resolve_engine
        return resolve_engine()
    except Exception:
        return None


def start_outbox_consumer(engine) -> Optional[OutboxConsumer]:
    """入口助手：起一个消费者协程（无运行 loop → None，单测/脚本场景）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    consumer = OutboxConsumer(engine)
    consumer.spawn()
    return consumer


def attach_outbox_consumer(app, engine) -> Optional[OutboxConsumer]:
    """lifespan 公共接线（QA #1314 M2/S3：grape_server 与 main 双入口共用，不再各写 40 行）。

    注册 set_running_app + 在 app.state 挂消费者并 spawn；engine 为 None 也起
    （消费者 run_once 每轮自 resolve_engine 自愈）。失败只告警不阻断启动。
    """
    try:
        from src.domains.runtime.im.notify import set_running_app
        set_running_app(app)
        consumer = start_outbox_consumer(engine)
        if consumer is None:
            return None
        app.state.outbox_consumer = consumer
        log.info("[outbox] consumer started (instance=%s)", consumer.instance_id)
        return consumer
    except Exception as e:
        log.warning("IM outbox consumer start failed: %s", e)
        return None


def stop_outbox_consumer(app) -> None:
    """lifespan 公共停机：先 cancel+await 消费者再由调用方停引擎（防竞态发给死渠道）。"""
    consumer = getattr(app.state, "outbox_consumer", None) \
        if getattr(app, "state", None) is not None else None
    if consumer is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        asyncio.get_running_loop().create_task(_stop_consumer_safe(consumer))
    except Exception as e:
        log.warning("IM outbox consumer stop failed: %s", e)


async def _stop_consumer_safe(consumer) -> None:
    try:
        await consumer.stop()
    except Exception as e:
        log.warning("IM outbox consumer stop failed: %s", e)


__all__ = [
    "OutboxConsumer", "start_outbox_consumer",
    "attach_outbox_consumer", "stop_outbox_consumer",
    "new_expiry", "release_sending",
    "enqueue", "claim_batch", "claim_for_sending", "mark_sent", "mark_failed",
    "mark_failed_no_attempt", "requeue_for_retry",
    "expire_overdue", "counts", "recent_undelivered", "list_rows", "get_row",
    "out_track_id", "lease_owner_of",
    "POLL_INTERVAL", "BATCH_LIMIT", "MAX_ATTEMPTS", "TTL_SECONDS",
]
