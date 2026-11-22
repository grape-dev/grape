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

# 300s 来源见本文件顶部 CONSUMED_RETENTION_HOURS 处的实测（最大消费 5s，60 倍余量）
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5
# 消费期租约续期间隔：取租约的 1/6（PR #1854 的 60s/10s 同比例）。流水线时长可能
# 远超租约，不续期就会被第二个 worker 重复认领 → 同一投递双跑。
LEASE_RENEW_INTERVAL = DEFAULT_LEASE_SECONDS / 6
# 清理：只清 consumed（已完成）且超过 24h 的行；pending/failed 永不清（长故障期
# 间投递必须保活）。dead 保留 7 天供排查后再清。
# 数值来源（2026-11-23 本机生产库实测，复审 B2）：
#   SELECT COUNT(*), SUM(status='consumed'), MAX(attempts) FROM grape_t_webhook_inbox;
#   → 330 条投递 / 330 条 consumed / 最大 attempts = 0（即零重试）
#   SELECT TIMESTAMPDIFF(SECOND, claimed_at, finished_at) ... → 最短 0s、均值 0.31s、最长 5s
# 据此取：租约 300s = 实测最大消费时长 5s 的 60 倍；保留期 24h / dead 7d / 轮询 1.0s
# 由该分布推导（1s 轮询给单条投递增加 <1s 端到端延迟，远小于 5s 上界）。
CONSUMED_RETENTION_HOURS = 24
DEAD_RETENTION_DAYS = 7
# attempts/backoff 依据：实测 330 条投递零重试（MAX(attempts)=0），说明单次消费成功率极高；
# 5 次上限用于吸收上游瞬时故障（平台在故障期会持续重投，见各平台 webhook 重投策略），
# 5s→900s 指数退避把 5 次尝试压在 ~15 分钟内，避免长时间占住 worker 又给上游恢复留窗口。
# 超期仍未消费成功的 pending/failed 搬归档（A7 有界增长）；保留期取 30 天，大于
# 平台重投窗口一个量级，排障时仍可回看。
PENDING_ARCHIVE_DAYS = 30
# 归档本身也要有保留期，否则「只写不删」把无界增长从主表搬到归档表（复审 A7 残留）。
# 180 天口径（审查 B2）：归档行只来自 dead 投递与超 30 天未消费的 pending，实测 330 条
# 投递中这些行为 0，写入速率远低于主表；保留 180 天 = 最长活跃保留期 30 天的 6 倍，
# 覆盖一个季度内的故障复盘窗口，同时让归档表保持有界。
# 复核脚本与原始结果见 docs/issue-3580/measurements.md。
ARCHIVE_RETENTION_DAYS = 180
PURGE_INTERVAL_SECONDS = 3600
# 60s 口径（审查 B2）：obs_metric_rollup 的 resolution='minute' 桶粒度就是 1 分钟，
# 导出快于此间隔只会反复覆盖同一桶（白写），慢于它则面板出现空桶；取 60s 与之对齐。
METRICS_INTERVAL_SECONDS = 60

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
            # Issue #3580 复审：显式写 last_error —— MySQL 下 TEXT 列带不了 DEFAULT ''，
            # 省略该列会在严格模式报 1364（SQLite 测不出来）。
            c.execute(
                "INSERT INTO grape_t_webhook_inbox "
                "(repo_key, provider, delivery_id, event_header, payload, headers, "
                " status, attempts, max_attempts, next_attempt_at, last_error, "
                " created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo_key, provider or "", did, event_header or "", payload,
                 headers_json, "pending", 0, int(max_attempts), now, "", now, now))
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
            # 成功也保留 note（如「unhosted: skipped」）：这条投递为什么被消费掉，
            # 事后只能从库里看，不能只活在一次日志里。
            c.execute(
                "UPDATE grape_t_webhook_inbox SET status='consumed', finished_at=?, "
                "last_error=?, claim_owner='', lease_expires_at='', updated_at=? "
                "WHERE id=? AND claim_owner=?",
                (now, (error or "")[:2000], now, int(item_id), owner))
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


def _iso_to_epoch(value) -> float | None:
    """created_at/updated_at 是 ISO 字符串（TEXT 列）；兼容历史 epoch 数字。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def inbox_metrics() -> dict:
    """队列维度的可观测指标（A13）：积压深度、最老等待时长、租约过期、重试总量。

    状态词表以生产者/消费者实际写入为准：pending / processing / failed / dead /
    consumed。历史口径里的 `claimed` 在本实现中不存在（租约占用的行是
    `processing`），照 `claimed` 查会恒为 0；`claimed` 仅作 processing 的别名保留，
    便于既有看板过渡。
    """
    out = {"pending": 0, "processing": 0, "claimed": 0, "failed": 0,
           "dead": 0, "consumed": 0,
           "oldest_pending_age_s": 0.0, "lease_expired": 0, "retries_total": 0,
           "open_rows": 0}
    _OPEN = ("pending", "processing", "failed")
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, COUNT(*) FROM grape_t_webhook_inbox GROUP BY status")
            for status, n in c.fetchall():
                key = str(status or "")
                if key in out:
                    out[key] = int(n)
                elif key:
                    out[key] = int(n)  # 未知状态也如实暴露，不静默丢弃
            out["claimed"] = out["processing"]  # 别名（历史看板口径）
            c.execute("SELECT COUNT(*), COALESCE(SUM(attempts), 0) FROM grape_t_webhook_inbox "
                      "WHERE status IN ('pending','processing','failed')")
            row = c.fetchone() or (0, 0)
            out["open_rows"] = int(row[0] or 0)
            out["retries_total"] = int(row[1] or 0)
            c.execute("SELECT MIN(created_at) FROM grape_t_webhook_inbox "
                      "WHERE status IN ('pending','failed')")
            oldest = (c.fetchone() or [None])[0]
            epoch = _iso_to_epoch(oldest)
            if epoch is not None:
                out["oldest_pending_age_s"] = max(0.0, time.time() - epoch)
            c.execute("SELECT COUNT(*) FROM grape_t_webhook_inbox "
                      "WHERE status='processing' AND lease_expires_at<>'' AND lease_expires_at<?",
                      (_now(),))
            out["lease_expired"] = int((c.fetchone() or [0])[0] or 0)
    except Exception as e:  # noqa: BLE001 - 指标失败绝不影响消费
        log.warning(f"webhook inbox metrics failed: {e}")
    return out


def emit_inbox_metrics() -> dict:
    """把队列指标导出到 insight（obs_metric_rollup）；insight 不可用则静默跳过。"""
    stats = inbox_metrics()
    try:
        from src.domains.insight import public as _insight
        if not _insight.available():
            return stats
        # 状态口径与 inbox_metrics 一致（processing 是租约占用的真实状态）
        for metric in ("pending", "processing", "failed", "dead", "consumed",
                       "open_rows", "retries_total", "lease_expired"):
            if metric in stats:
                _insight.record_metric(f"webhook_inbox_{metric}", float(stats.get(metric) or 0),
                                       dim_key="global")
        _insight.record_metric("webhook_inbox_oldest_pending_age_s",
                               float(stats.get("oldest_pending_age_s") or 0.0), dim_key="global")
    except Exception as e:  # noqa: BLE001
        log.debug(f"webhook inbox metrics export skipped: {e}")
    return stats


def worker_owner() -> str:
    return f"{os.environ.get('HOSTNAME', 'event')}:{os.getpid()}"


def ensure_webhook_inbox_archive_table() -> bool:
    """幂等建归档表（与主表同源，Event 进程启动时建）。"""
    from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_ARCHIVE_DDL

    try:
        with db_lock:
            c = db.cursor()
            c.execute(_WEBHOOK_INBOX_ARCHIVE_DDL)
            db.commit()
        return True
    except Exception as e:  # noqa: BLE001 - 归档表建不出来不阻断主表消费
        log.warning(f"webhook inbox archive table ensure failed: {e}")
        return False


def ensure_webhook_inbox_table() -> bool:
    """幂等建表（Event 进程启动、worker 起跑前调用）。

    为什么不能只靠迁移：本机多库拓扑下 runtime/insight 库的
    `grape_t_schema_migrations` 是 mgmt 台账的**跨库视图**（迁移只在 mgmt 进程执行），
    runtime 域的新表在 mgmt 侧盖章后，Event 进程永远判「已执行」→ 表建不出来
    （#3527 的 grape_t_cloud_sandboxes 同现象，runtime 库里至今缺表）。
    本表由 Event 进程独占读写，启动时 CREATE TABLE IF NOT EXISTS 自建最可靠。
    """
    from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_DDL

    try:
        with db_lock:
            c = db.cursor()
            c.execute(_WEBHOOK_INBOX_DDL)
            db.commit()
        from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_INDEXES

        with db_lock:
            c = db.cursor()
            for table, name, cols in _WEBHOOK_INBOX_INDEXES:
                try:
                    c.execute("SELECT COUNT(*) FROM information_schema.statistics "
                              "WHERE table_schema=DATABASE() AND table_name=? AND index_name=?",
                              (table, name))
                    if int((c.fetchone() or [0])[0] or 0) == 0:
                        c.execute(f"CREATE INDEX {name} ON {table} {cols}")
                except Exception as e:  # noqa: BLE001 - 建索引失败不阻断收件箱消费
                    log.warning(f"webhook inbox index {name} skipped: {e}")
            db.commit()
        ensure_webhook_inbox_archive_table()
        return True
    except Exception as e:  # noqa: BLE001 - 建表失败由 worker 循环逐轮重试
        log.warning(f"webhook inbox ensure table failed: {e}")
        return False


def is_still_hosted(repo_key: str) -> bool:
    """消费前复验托管状态（对齐 PR #1854 的 dispatch 复验 secret）。

    为什么必须在**消费时**再问一次：投递入队后、消费前，仓库可能已被解除托管。
    托管=授权；授权撤销后残留的 pending 投递若照跑，等于撤销不生效。
    读法走 GSB mgmt 契约（跨域不直连对方表）；契约不可用时返回 True（fail-open）：
    宁可让投递继续（与改动前行为一致），也不能因 mgmt 抖动把整批事件卡死丢单。
    """
    try:
        from src.kernel.gsb.provider import get_service

        cfg = get_service("mgmt").get_repo_webhook_config(repo_key) or {}
        return bool(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning(f"webhook inbox hosting recheck unavailable (repo={repo_key}): {e}")
        return True


def _consume_one(item: dict, owner: str, *, pipeline=None) -> dict:
    """消费一条投递：托管复验 → run_pipeline → finish。共三次 DB 写，均可重入。"""
    repo_key = item["repo_key"]
    if not is_still_hosted(repo_key):
        log.info(f"webhook inbox skip unhosted id={item['id']} repo={repo_key}")
        return finish_claim(item["id"], owner, ok=True, error="unhosted: skipped")
    if pipeline is None:
        from src.domains.runtime.pipeline.event_pipeline import run_pipeline
        pipeline = run_pipeline
    headers = {}
    try:
        headers = json.loads(item.get("headers") or "{}")
    except Exception:
        headers = {}
    body = (item.get("payload") or "").encode("utf-8")
    account = repo_key.split(":", 1)[-1]
    try:
        pipeline(body, headers, account=account, repo_key_override=repo_key)
    except Exception as e:  # noqa: BLE001
        log.warning(f"webhook inbox consume failed id={item['id']}: {e}")
        return finish_claim(item["id"], owner, ok=False, error=str(e))
    return finish_claim(item["id"], owner, ok=True)


async def _run_with_lease(item: dict, owner: str, *, pipeline=None,
                          renew_interval: float = LEASE_RENEW_INTERVAL) -> dict:
    """在线程里跑消费，同时续租：长流水线不会被第二个 worker 认领重跑。"""
    import asyncio

    task = asyncio.create_task(
        asyncio.to_thread(_consume_one, item, owner, pipeline=pipeline))
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=renew_interval)
        if done:
            break
        try:
            renewed = await asyncio.to_thread(renew_claim, item["id"], owner)
        except Exception as e:  # noqa: BLE001 - 续租失败不能把仍在跑的任务判失败
            log.warning(f"webhook inbox lease renew failed id={item['id']}: {e}")
            continue
        if not renewed:
            # 租约已被别人拿走：这条投递可能已在别处重跑，交给 finish 的
            # claim_owner 条件判 lost（不会覆盖别人的结果）。
            log.warning(f"webhook inbox lease lost id={item['id']} owner={owner}")
            break
    return await task


_ARCHIVE_COLUMNS = ("repo_key, provider, delivery_id, event_header, payload, headers, "
                    "status, attempts, max_attempts, claim_owner, claimed_at, lease_expires_at, "
                    "next_attempt_at, last_error, created_at, updated_at, finished_at")


def purge_finished(retention_hours: int = CONSUMED_RETENTION_HOURS,
                   dead_days: int = DEAD_RETENTION_DAYS) -> dict:
    """清理已完成/已判死的行；dead 与超期滞留的 pending 先归档再删（A7）。

    pending/failed 在保留期内**不清**（长故障期间投递要保活）；超过
    PENDING_ARCHIVE_DAYS 仍没消费成功的，说明上游已长期不可用或事件已失去
    时效，搬进归档表而不是删除——排障时要能回看原始投递。
    """
    consumed_before = (datetime.now(timezone.utc)
                       - timedelta(hours=retention_hours)).isoformat()
    dead_before = (datetime.now(timezone.utc) - timedelta(days=dead_days)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_webhook_inbox "
                  "WHERE status='consumed' AND finished_at!='' AND finished_at<?",
                  (consumed_before,))
        consumed = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        dead = 0
        try:
            c.execute(f"INSERT INTO grape_t_webhook_inbox_archive ({_ARCHIVE_COLUMNS}) "
                      "SELECT " + _ARCHIVE_COLUMNS + " FROM grape_t_webhook_inbox "
                      "WHERE status='dead' AND updated_at<?", (dead_before,))
            c.execute("DELETE FROM grape_t_webhook_inbox "
                      "WHERE status='dead' AND updated_at<?", (dead_before,))
            dead = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        except Exception as e:  # noqa: BLE001 - 归档表缺失时退回原删除行为
            log.warning(f"webhook inbox archive unavailable, falling back to delete: {e}")
            c.execute("DELETE FROM grape_t_webhook_inbox "
                      "WHERE status='dead' AND updated_at<?", (dead_before,))
            dead = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        stale_before = (datetime.now(timezone.utc)
                        - timedelta(days=PENDING_ARCHIVE_DAYS)).isoformat()
        stale = 0
        try:
            c.execute(f"INSERT INTO grape_t_webhook_inbox_archive ({_ARCHIVE_COLUMNS}) "
                      "SELECT " + _ARCHIVE_COLUMNS + " FROM grape_t_webhook_inbox "
                      "WHERE status IN ('pending','failed') AND created_at<?", (stale_before,))
            c.execute("DELETE FROM grape_t_webhook_inbox "
                      "WHERE status IN ('pending','failed') AND created_at<?", (stale_before,))
            stale = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        except Exception as e:  # noqa: BLE001
            log.warning(f"webhook inbox stale-pending archive failed: {e}")
        archive_deleted = 0
        try:
            archived_before = (datetime.now(timezone.utc)
                               - timedelta(days=ARCHIVE_RETENTION_DAYS)).isoformat()
            c.execute("DELETE FROM grape_t_webhook_inbox_archive WHERE archived_at<?",
                      (archived_before,))
            archive_deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        except Exception as e:  # noqa: BLE001 - 归档表缺失时跳过
            log.debug(f"webhook inbox archive retention skipped: {e}")
        db.commit()
    if consumed or dead or stale or archive_deleted:
        log.info(f"webhook inbox purged consumed={consumed} dead={dead} "
                 f"stale_pending={stale} archive_deleted={archive_deleted}")
    return {"consumed": consumed, "dead": dead, "stale_pending": stale,
            "archive_deleted": archive_deleted}


async def webhook_inbox_loop(poll_interval: float = 1.0) -> None:
    """Event 进程消费循环：claim → [托管复验] → run_pipeline（含续租）→ finish。"""
    import asyncio

    owner = worker_owner()
    log.info(f"webhook inbox worker started (owner={owner})")
    last_purge = time.time()
    last_metrics = 0.0
    while True:
        try:
            if time.time() - last_metrics >= METRICS_INTERVAL_SECONDS:
                last_metrics = time.time()
                try:
                    await asyncio.to_thread(emit_inbox_metrics)
                except Exception as e:  # noqa: BLE001 - 指标导出失败不影响消费
                    log.warning(f"webhook inbox metrics emit failed: {e}")
            if time.time() - last_purge >= PURGE_INTERVAL_SECONDS:
                last_purge = time.time()
                try:
                    await asyncio.to_thread(purge_finished)
                except Exception as e:  # noqa: BLE001 - 清理失败不影响消费
                    log.warning(f"webhook inbox purge failed: {e}")
            item = await asyncio.to_thread(claim_next, owner)
            if item is None:
                await asyncio.sleep(poll_interval)
                continue
            from src.kernel.otel import feature_span
            with feature_span("webhook_inbox.consume", {
                    "inbox.id": item.get("id") if isinstance(item, dict) else 0,
                    "repo_key": (item or {}).get("repo_key", "") if isinstance(item, dict) else ""}):
                await _run_with_lease(item, owner)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"webhook inbox loop error: {e}")
            await asyncio.sleep(poll_interval)