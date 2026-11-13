"""跨域写基础设施：幂等键 + 补偿队列 + 重试/死信（P3 地基）。

设计（与方案 §D4 一致：最终一致 + 补偿，不做 2PC）：

1. 调用方在**自己的库**写一条 `grape_xwrite_log` 意图行（status=pending），
   与业务写同库，天然原子；不产生新的跨域写。
2. 通过目标域 lib 执行写操作；成功 → applied；抛错 → failed（记 last_error、attempts+1）。
3. `drain()` 由任意后台循环周期调用：重试 failed/pending，超过 max_attempts → dead（死信）。
4. `apply_fn` 必须自身幂等（UPSERT / 条件 UPDATE / 带 idempotency_key 的 INSERT）。

日志表在每个域库各一份（同名不同库），因此登记为"已知多份基表"（有意为之）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from src.kernel import db_registry
from src.kernel.session_manager import db as _process_db

LOG_TABLE = "grape_xwrite_log"
MAX_ATTEMPTS = 5
RETRY_BACKOFF_S = 30


def _ddl() -> str:
    import os

    backend = (os.environ.get("DEVOPS_DB_BACKEND") or "sqlite").lower()
    if backend == "mysql":
        return f"""
CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  idempotency_key VARCHAR(191) NOT NULL,
  target_domain VARCHAR(32) NOT NULL,
  action VARCHAR(64) NOT NULL,
  payload_json MEDIUMTEXT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT NOT NULL DEFAULT 0,
  last_error TEXT,
  next_retry_at BIGINT NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_xwrite_key (idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
    return f"""
CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  target_domain TEXT NOT NULL,
  action TEXT NOT NULL,
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_retry_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
)
"""


def ensure_log_table() -> None:
    """在当前进程库建日志表（幂等）。"""
    c = _process_db.cursor()
    try:
        c.execute(_ddl())
    finally:
        c.close()


def _now() -> int:
    return int(time.time())


def submit(target_domain: str, action: str, idempotency_key: str, payload: Any,
           apply_fn: Callable[[], Any]) -> dict[str, Any]:
    """登记意图并立即尝试执行一次。

    返回 {"status": applied|failed|duplicate, "key": ...}；
    同一 idempotency_key 重复提交不会重复执行（duplicate）。
    """
    if not idempotency_key:
        raise ValueError("idempotency_key 必填")
    ensure_log_table()
    c = _process_db.cursor()
    now = _now()
    try:
        try:
            c.execute(
                f"INSERT INTO {LOG_TABLE} (idempotency_key, target_domain, action, payload_json, "
                "status, attempts, next_retry_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)",
                (idempotency_key, target_domain, action,
                 json.dumps(payload, ensure_ascii=False, default=str), now, now),
            )
        except Exception:
            # 唯一键冲突 = 已提交过，直接返回 duplicate（不重复执行）
            c.execute(f"SELECT status FROM {LOG_TABLE} WHERE idempotency_key=?", (idempotency_key,))
            row = c.fetchone()
            return {"status": "duplicate", "key": idempotency_key,
                    "previous": (row[0] if row else "unknown")}

        try:
            apply_fn()
            c.execute(f"UPDATE {LOG_TABLE} SET status='applied', attempts=attempts+1, "
                      "updated_at=? WHERE idempotency_key=?", (now, idempotency_key))
            return {"status": "applied", "key": idempotency_key}
        except Exception as exc:  # noqa: BLE001
            c.execute(f"UPDATE {LOG_TABLE} SET status='failed', attempts=attempts+1, "
                      "last_error=?, next_retry_at=?, updated_at=? WHERE idempotency_key=?",
                      (str(exc)[:500], now + RETRY_BACKOFF_S, now, idempotency_key))
            return {"status": "failed", "key": idempotency_key, "error": str(exc)[:200]}
    finally:
        c.close()


def submit_deferred(target_domain: str, action: str, idempotency_key: str, payload: Any) -> dict[str, Any]:
    """只登记意图，不立即执行（供"必须与本地事务一起提交"的场景，如新建引擎+默认能力）。

    意图行与调用方业务写同库同事务，提交后由 drain() 落地 —— 不会出现
    "能力行已写、引擎行回滚"的孤儿写；代价是短暂最终一致。
    """
    if not idempotency_key:
        raise ValueError("idempotency_key 必填")
    ensure_log_table()
    now = _now()
    c = _process_db.cursor()
    try:
        try:
            c.execute(
                f"INSERT INTO {LOG_TABLE} (idempotency_key, target_domain, action, payload_json, "
                "status, attempts, next_retry_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)",
                (idempotency_key, target_domain, action,
                 json.dumps(payload, ensure_ascii=False, default=str), now, now),
            )
            return {"status": "deferred", "key": idempotency_key}
        except Exception:
            return {"status": "duplicate", "key": idempotency_key}
    finally:
        c.close()


def drain(apply_fns: dict[str, Callable[[dict[str, Any]], Any]], limit: int = 50) -> dict[str, int]:
    """重试待补偿行。apply_fns: action → 可调用（接收 payload dict）。

    超过 MAX_ATTEMPTS 标记为 dead（死信，需人工/对账处理）。
    """
    ensure_log_table()
    c = _process_db.cursor()
    now = _now()
    out = {"retried": 0, "applied": 0, "failed": 0, "dead": 0}
    try:
        c.execute(
            f"SELECT id, idempotency_key, action, payload_json, attempts FROM {LOG_TABLE} "
            "WHERE status IN ('pending','failed') AND (next_retry_at=0 OR next_retry_at<=?) "
            "ORDER BY id LIMIT ?", (now, int(limit)),
        )
        rows = c.fetchall()
        for row_id, key, action, payload_json, attempts in rows:
            if attempts >= MAX_ATTEMPTS:
                c.execute(f"UPDATE {LOG_TABLE} SET status='dead', updated_at=? WHERE id=?",
                          (now, row_id))
                out["dead"] += 1
                continue
            fn = apply_fns.get(action)
            if fn is None:
                c.execute(f"UPDATE {LOG_TABLE} SET last_error=?, next_retry_at=?, updated_at=? "
                          "WHERE id=?", ("no handler for action", now + RETRY_BACKOFF_S, now, row_id))
                out["failed"] += 1
                continue
            out["retried"] += 1
            try:
                fn(json.loads(payload_json or "{}"))
                c.execute(f"UPDATE {LOG_TABLE} SET status='applied', attempts=attempts+1, "
                          "updated_at=? WHERE id=?", (now, row_id))
                out["applied"] += 1
            except Exception as exc:  # noqa: BLE001
                new_attempts = attempts + 1
                status = "dead" if new_attempts >= MAX_ATTEMPTS else "failed"
                c.execute(f"UPDATE {LOG_TABLE} SET status=?, attempts=?, last_error=?, "
                          "next_retry_at=?, updated_at=? WHERE id=?",
                          (status, new_attempts, str(exc)[:500], now + RETRY_BACKOFF_S, now, row_id))
                out["dead" if status == "dead" else "failed"] += 1
        return out
    finally:
        c.close()


def stats() -> dict[str, int]:
    ensure_log_table()
    c = _process_db.cursor()
    try:
        c.execute(f"SELECT status, COUNT(*) FROM {LOG_TABLE} GROUP BY status")
        return {r[0]: r[1] for r in c.fetchall()}
    finally:
        c.close()


if __name__ == "__main__":  # pragma: no cover - 运维入口
    import sys

    from src.domains.runtime import public as _rt

    _handlers = {
        "engine_cap_upsert": lambda p: _rt.upsert_engine_capability(
            p["rid"], p["tag"], priority=p.get("priority", 50)),
        "engine_cap_update": lambda p: _rt.update_engine_capability(
            p["rid"], p["tag"], p.get("priority", 50), p.get("enabled", 1)),
        "engine_cap_delete": lambda p: _rt.delete_engine_capability(p["rid"], p.get("tag")),
        "engine_cap_delete_all": lambda p: _rt.delete_engine_capability(p["rid"]),
    }
    cmd = sys.argv[1] if len(sys.argv) > 1 else "drain"
    if cmd == "stats":
        print(stats())
    elif cmd == "drain":
        print(drain(_handlers))
    else:
        print("usage: python -m src.kernel.cross_write [drain|stats]")

