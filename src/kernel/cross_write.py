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

LOG_TABLE = "grape_t_xwrite_log"
MAX_ATTEMPTS = 5  # 补偿重试上限：5 次约覆盖 2.5 分钟退避窗口，与巡检周期同量级
RETRY_BACKOFF_S = 30  # 重试退避 30s：与 drain 定时器 5 分钟周期错开，避免同拍重试


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


def submit_if_lib(lib, target_domain: str, action: str, key_base: str, payload: Any,
                  apply_fn: Callable[[], Any], log=None) -> bool:
    """lib 可用则提交一条幂等意图并返回 True；lib 为 None 或提交异常返回 False。

    统一封装「跨域写四段式」的公共部分，供 mgmt→runtime（P3-5g/5h）等写点复用：
    调用方拿到 False 时执行**原来的本地 SQL**，保证单机/sqlite 拓扑零回归。
    """
    if lib is None:
        return False
    try:
        res = submit(target_domain, action, unique_key(key_base), payload, apply_fn)
        if res.get("status") == "failed" and log is not None:
            log.warning(f"cross write {action} queued for retry: {res.get('error')}")
        return True
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log.warning(f"cross write {action} via lib failed, fallback to local SQL: {exc}")
        return False


def unique_key(base: str) -> str:
    """为「可反复发生的状态变更」生成意图键：可读内容前缀 + 唯一后缀（**不跨调用去重**）。

    为什么不能只用内容哈希做键（实测踩坑）：内容哈希键是**永久去重**的，遇到
    「置 A → 清空 → 再置 A」这类状态往复会静默跳过第二次写入 —— 现场表现是
    "飞书解绑后重新绑定同一个 app 不生效"。同理还有 token 状态 ok↔expired、
    deploy_url A→B→A、知识内容回退、行删除后重建。
    这些写原语自身都是幂等的（同值 0 行变更 / INSERT IGNORE / 条件 UPDATE /
    DELETE+INSERT 重建），重复执行无副作用，所以用唯一后缀保证"每次真实调用都被提交一次"。
    代价是日志行按调用数增长，去重职责从"键"移交给"原语幂等性"。
    """
    import uuid

    return f"{base}:{uuid.uuid4().hex[:12]}"


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

        # 意图行先落盘再执行：apply_fn 走的是**另一个域的独立连接**（自带 autocommit），
        # 日志行若留待调用方事务提交，会出现
        #   ① 调用方回滚 → 跨域写已生效却没有补偿记录（不可对账）；
        #   ② 调用方迟迟不提交 → 意图行对其他进程不可见、且持行锁阻塞 drain（实测 1205）。
        _process_db.commit()

        try:
            apply_fn()
            c.execute(f"UPDATE {LOG_TABLE} SET status='applied', attempts=attempts+1, "
                      "updated_at=? WHERE idempotency_key=?", (now, idempotency_key))
            _process_db.commit()
            return {"status": "applied", "key": idempotency_key}
        except Exception as exc:  # noqa: BLE001
            c.execute(f"UPDATE {LOG_TABLE} SET status='failed', attempts=attempts+1, "
                      "last_error=?, next_retry_at=?, updated_at=? WHERE idempotency_key=?",
                      (str(exc)[:500], now + RETRY_BACKOFF_S, now, idempotency_key))
            _process_db.commit()
            return {"status": "failed", "key": idempotency_key, "error": str(exc)[:200]}
    finally:
        c.close()


def submit_deferred(target_domain: str, action: str, idempotency_key: str, payload: Any) -> dict[str, Any]:
    """只登记意图，不立即执行（供"必须与本地事务一起提交"的场景，如新建引擎+默认能力）。

    意图行与调用方业务写同库同事务，提交后由 drain() 落地 —— 不会出现
    "能力行已写、引擎行回滚"的孤儿写；代价是短暂最终一致。

    **注意**：MySQL 后端 `autocommit=False`，本函数刻意不 commit（语义就是把意图行
    交给调用方事务）。调用方必须在同一事务里 commit，否则意图行随回滚丢失，
    等于登记了一条不会落地的写。
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

    limit=50：单轮最多处理 50 条，按 5 分钟周期即上限 600 条/小时，
    对应本平台跨域写峰值（历史 <20 条/小时）的 30 倍余量。

    超过 MAX_ATTEMPTS 标记为 dead（死信，需人工/对账处理）。
    """
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
                _process_db.commit()   # 逐行落盘：中途异常也不丢已完成的行
                out["dead"] += 1
                continue
            fn = apply_fns.get(action)
            if fn is None:
                c.execute(f"UPDATE {LOG_TABLE} SET last_error=?, next_retry_at=?, updated_at=? "
                          "WHERE id=?", ("no handler for action", now + RETRY_BACKOFF_S, now, row_id))
                _process_db.commit()
                out["failed"] += 1
                continue
            out["retried"] += 1
            try:
                fn(json.loads(payload_json or "{}"))
                c.execute(f"UPDATE {LOG_TABLE} SET status='applied', attempts=attempts+1, "
                          "updated_at=? WHERE id=?", (now, row_id))
                _process_db.commit()
                out["applied"] += 1
            except Exception as exc:  # noqa: BLE001
                new_attempts = attempts + 1
                status = "dead" if new_attempts >= MAX_ATTEMPTS else "failed"
                c.execute(f"UPDATE {LOG_TABLE} SET status=?, attempts=?, last_error=?, "
                          "next_retry_at=?, updated_at=? WHERE id=?",
                          (status, new_attempts, str(exc)[:500], now + RETRY_BACKOFF_S, now, row_id))
                _process_db.commit()
                out["dead" if status == "dead" else "failed"] += 1
        _process_db.commit()
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
