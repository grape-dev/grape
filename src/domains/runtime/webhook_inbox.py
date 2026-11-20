"""Webhook 事件持久收件箱（Issue #3580/P3）。

设计要点：
- 入队与消费都在 Event 进程（同一 runtime 库），webhook 端点验签后**先持久化**再返回 200；
- 幂等键 = (repo_key, delivery_id)：delivery_id 取平台投递 ID 头，缺失时用 body sha256；
- claim_owner + lease_expires_at 提供租约；进程崩溃后租约过期，未完成投递可被重放消费；
- finish 失败按指数退避写 next_attempt_at，超过 max_attempts 置 dead（保留现场供排查）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from src.kernel.config import log
from src.kernel.session_manager import db, db_lock

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5

_DELIVERY_HEADERS = (
    "x-gitlab-event-uuid",
    "x-github-delivery",
    "x-gitcode-delivery",
    "x-request-id",
)

# 敏感头不落库：验签在入口已完成，worker 只需事件类型头。
_SENSITIVE_HEADERS = frozenset({
    "x-gitlab-token", "x-gitcode-token", "x-hub-signature",
    "x-hub-signature-256", "authorization", "cookie",
})


def _redacted_headers(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        key = str(k).lower()
        out[key] = "***" if key in _SENSITIVE_HEADERS else v
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def delivery_id_from(headers: dict, body: bytes) -> str:
    """平台投递 ID 头优先；缺失/空值 → body sha256（同 body 视为同投递）。"""
    _h = {str(k).lower(): v for k, v in (headers or {}).items()}
    for name in _DELIVERY_HEADERS:
        v = str(_h.get(name) or "").strip()
        if v:
            return v[:128]
    return "sha256:" + hashlib.sha256(body or b"").hexdigest()


def enqueue_webhook(*, repo_key: str, provider: str, event_header: str,
                    body: bytes, headers: dict,
                    delivery_id: str = "",
                    max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """持久化一条投递（幂等）。返回 {"id", "status", "duplicate"}。"""
    if not repo_key:
        raise ValueError("repo_key required")
    did = (delivery_id or delivery_id_from(headers, body))[:128]
    payload = (body or b"").decode("utf-8", errors="replace")
    headers_json = json.dumps(_redacted_headers(headers), ensure_ascii=False)
    now = _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, status FROM grape_t_webhook_inbox "
            "WHERE repo_key=? AND delivery_id=?", (repo_key, did))
        row = c.fetchone()
        if row:
            return {"id": row[0], "status": row[1], "duplicate": True}
        try:
            c.execute(
                "INSERT INTO grape_t_webhook_inbox "
                "(repo_key, provider, delivery_id, event_header, payload, headers, "
                " status, attempts, max_attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo_key, provider or "", did, event_header or "", payload,
                 headers_json, "pending", 0, int(max_attempts), now, now, now))
            db.commit()
            return {"id": c.lastrowid, "status": "pending", "duplicate": False}
        except Exception as e:
            # 并发同投递：唯一键冲突后读已有行（不静默吞掉其它错误）
            db.rollback()
            c.execute(
                "SELECT id, status FROM grape_t_webhook_inbox "
                "WHERE repo_key=? AND delivery_id=?", (repo_key, did))
            row = c.fetchone()
            if row:
                return {"id": row[0], "status": row[1], "duplicate": True}
            log.warning(f"webhook_inbox enqueue failed {repo_key}/{did}: {e}")
            raise


def claim_next(owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> "dict | None":
    """抢占一条可消费投递（pending/failed 且 next_attempt_at 到期，或无有效租约）。"""
    now = _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, provider, delivery_id, event_header, payload, headers, "
            "attempts, max_attempts FROM grape_t_webhook_inbox "
            "WHERE ((status IN ('pending','failed') AND next_attempt_at<=?) "
            "       OR (status='processing' AND lease_expires_at<? AND lease_expires_at!='')) "
            "AND (claim_owner='' OR lease_expires_at<?) "
            "ORDER BY id LIMIT 1", (now, now, now))
        row = c.fetchone()
        if not row:
            return None
        _id = row[0]
        lease = _future(lease_seconds)
        c.execute(
            "UPDATE grape_t_webhook_inbox SET status='processing', claim_owner=?, "
            "claimed_at=?, lease_expires_at=?, updated_at=? "
            "WHERE id=? AND ((status IN ('pending','failed') AND next_attempt_at<=?) "
            "                OR (status='processing' AND lease_expires_at<?)) "
            "AND (claim_owner='' OR lease_expires_at<?)",
            (owner, now, lease, now, _id, now, now, now))
        if c.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return {
            "id": _id,
            "repo_key": row[1],
            "provider": row[2],
            "delivery_id": row[3],
            "event_header": row[4],
            "payload": row[5],
            "headers": row[6],
            "attempts": row[7],
            "max_attempts": row[8],
        }


def renew_claim(item_id: int, owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
    """续租（长事件处理期间保活）。"""
    now = _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_webhook_inbox SET lease_expires_at=?, updated_at=? "
            "WHERE id=? AND claim_owner=? AND status='processing'",
            (_future(lease_seconds), now, int(item_id), owner))
        ok = c.rowcount == 1
        db.commit()
        return ok


def finish_claim(item_id: int, owner: str, *, ok: bool, error: str = "",
                 base_backoff: int = 5, max_backoff: int = 900) -> dict:
    """结束一次消费：成功→consumed；失败→退避重排/超限 dead。"""
    now = _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT attempts, max_attempts FROM grape_t_webhook_inbox "
            "WHERE id=? AND claim_owner=? AND status='processing'",
            (int(item_id), owner))
        row = c.fetchone()
        if not row:
            return {"id": int(item_id), "status": "lost"}
        attempts, max_attempts = int(row[0]), int(row[1])
        if ok:
            c.execute(
                "UPDATE grape_t_webhook_inbox SET status='consumed', finished_at=?, "
                "last_error='', claim_owner='', lease_expires_at='', updated_at=? "
                "WHERE id=? AND claim_owner=?", (now, now, int(item_id), owner))
            db.commit()
            return {"id": int(item_id), "status": "consumed", "attempts": attempts}
        attempts += 1
        status = "failed" if attempts < max_attempts else "dead"
        delay = min(max_backoff, base_backoff * (2 ** max(0, attempts - 1)))
        c.execute(
            "UPDATE grape_t_webhook_inbox SET status=?, attempts=?, last_error=?, "
            "claim_owner='', lease_expires_at='', next_attempt_at=?, updated_at=? "
            "WHERE id=? AND claim_owner=?",
            (status, attempts, (error or "")[:2000], _future(delay), now,
             int(item_id), owner))
        db.commit()
        return {"id": int(item_id), "status": status, "attempts": attempts}


def inbox_stats() -> dict:
    """按状态计数（运维/测试观测用）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, COUNT(*) FROM grape_t_webhook_inbox GROUP BY status")
        return {str(r[0]): int(r[1]) for r in c.fetchall()}


def worker_owner() -> str:
    return f"{os.environ.get('HOSTNAME', 'event')}:{os.getpid()}"


async def webhook_inbox_loop(poll_interval: float = 1.0) -> None:
    """Event 进程消费循环：claim → run_pipeline → finish（可取消）。"""
    import asyncio

    owner = worker_owner()
    log.info(f"webhook inbox worker started (owner={owner})")
    while True:
        try:
            item = await asyncio.to_thread(claim_next, owner)
            if item is None:
                await asyncio.sleep(poll_interval)
                continue
            _headers = {}
            try:
                _headers = json.loads(item.get("headers") or "{}")
            except Exception:
                _headers = {}
            _body = (item.get("payload") or "").encode("utf-8")
            _account = item["repo_key"].split(":", 1)[-1]
            try:
                from src.domains.runtime.pipeline.event_pipeline import run_pipeline
                await asyncio.to_thread(
                    run_pipeline, _body, _headers,
                    account=_account, repo_key_override=item["repo_key"])
                await asyncio.to_thread(finish_claim, item["id"], owner, ok=True)
            except Exception as e:
                log.warning(f"webhook inbox consume failed id={item['id']}: {e}")
                await asyncio.to_thread(finish_claim, item["id"], owner, ok=False,
                                        error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"webhook inbox loop error: {e}")
            await asyncio.sleep(poll_interval)