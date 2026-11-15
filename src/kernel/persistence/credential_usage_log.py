"""credential_usage_log——凭据使用审计表（D90 P-5 硬前置；D100 值域收窄）。

定位（契约决议 P-5，docs/weekly-09/oauth-fill-contract-review.md 第 5 节）：
- **P-3 并存形态的准入门槛**（非加分项）：归属者语义（P-5a）+ 并存（P-3）下，
  没有本表 = "A 的凭据被 B 使用但审计只显示 A"的系统性归因错误。
- executor/initiator 分列（业界硬要求）：复用一个字段表达不同 actor 语义被
  明确禁止（Zenodo actor governance 结论）。

表设计（决议 + 表设计铁律）：
- id TEXT PRIMARY KEY（UUID，铁律 1）；created_at（审计行只增不改，无 updated_at
  ——铁律 2 的审计特例：变更即篡改）
- credential_id（软引用：**各凭据表行 id**（ei/uc/rc/sc），**无外键级联**——
  凭据删除必须保留审计历史，铁律 6）
- credential_kind 值域（D100 收窄）：{'external_identities' |
  'user_credentials' | 'grape_t_repo_credentials' | 'grape_t_system_credentials'}——
  'user_platform_credentials' 随 upc 废弃退役（D100），存量行 P4=保留
  （审计只增不改），新写入零产生
- initiating_actor_id（触发者）/ executor_context（执行上下文）分列——
  executor/initiator 分离

写入通道（铁律 9）：高频审计写入 → Core 风格参数化 insert（豁免场景），
SQLAlchemy Core table 构造保证列名/类型安全，禁字符串拼接。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid

from sqlalchemy import Column, Table, Text
from sqlalchemy import insert, select

from src.kernel.persistence.base import Base
from src.kernel.persistence.masking import masked_repr
from src.kernel.id_gen import uuid7_str  # D105 第二批：DB 主键 UUIDv7


_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_credential_usage_log (
    id TEXT PRIMARY KEY,               -- UUID（铁律 1）
    credential_id TEXT NOT NULL,       -- 软引用（无外键级联——铁律 6）
    credential_kind TEXT NOT NULL DEFAULT 'repo_credentials',
                                       -- D100 值域：external_identities |
                                       -- user_credentials | grape_t_repo_credentials |
                                       -- grape_t_system_credentials（upc kind 已退役，
                                       -- 存量行保留）
    user_id TEXT NOT NULL,             -- 凭据归属者（P-5a 归属者语义；D105 第二批 UUIDv7/TEXT）
    initiating_actor_id TEXT DEFAULT '',  -- 触发者（''=系统/定时，非用户触发；UUID TEXT）
    executor_context TEXT DEFAULT '',  -- 执行上下文：webhook | task | chat | manual | use | resolve
    operation TEXT DEFAULT '',         -- 操作名（可选补充语义）
    created_at TEXT DEFAULT ''         -- 只增不改（审计行禁 update）
)
"""

# SQLAlchemy Core 表构造（列名/类型安全；insert 参数化——铁律 9 安全底线）
core_table = Table(
    "grape_t_credential_usage_log", Base.metadata,
    Column("id", Text, primary_key=True),
    Column("credential_id", Text, nullable=False),
    Column("credential_kind", Text, nullable=False, default="repo_credentials"),
    Column("user_id", Text),
    Column("repo_key", Text, default=""),
    Column("initiating_actor_id", Text, default=""),
    Column("executor_context", Text, default=""),
    Column("operation", Text, default=""),
    Column("created_at", Text, default=""),
    extend_existing=True,
)


def ensure_table() -> None:
    """幂等建表（init_db 启动链路调用）。"""
    from src.kernel.owner_db import owner_db as _owner_db
    db = _owner_db("runtime")
    db.execute(_DDL)


def log_usage(credential_id: str, user_id: "str | int",
              credential_kind: str = "repo_credentials",
              initiating_actor_id: "str | int" = "", executor_context: str = "",
              operation: str = "", repo_key: str = "",
              flush: bool = False) -> str:
    """记一条凭据使用审计（只增不改）。返回审计行 UUID。

    N4-p3：默认异步——入有界队列（10000），后台线程攒批（≤50）刷写；
    洪峰丢最旧（审计可容忍，主流程零阻塞）。flush=True 同步直写
    （测试/回填用）。失败不抛（审计失败不阻断业务）。
    """
    from src.kernel.config import log
    row_id = uuid7_str()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    item = {"id": row_id, "credential_id": credential_id,
            "credential_kind": credential_kind, "user_id": user_id,
            "repo_key": repo_key, "initiating_actor_id": initiating_actor_id,
            "executor_context": executor_context, "operation": operation,
            "created_at": now}
    if flush:
        try:
            _flush_batch([item])
            return row_id
        except Exception as e:
            log.warning(f"credential_usage_log insert failed (non-blocking): {e}")
            return ""
    try:
        _ensure_worker()
        q = _get_queue()
        try:
            q.put_nowait(item)
        except queue.Full:
            # 队列满：丢最旧一条再入队（审计可容忍）
            try:
                q.get_nowait()
                q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
        # N4-p2：惰性保留期检查（30 天节流，非窗口内即刻返回）
        maybe_cleanup_expired_retention()
        return row_id
    except Exception as e:
        log.warning(f"credential_usage_log enqueue failed (non-blocking): {e}")
        return ""


# ====== N4-p2 保留期控制（防无线增长；2026-11-07 用户拍板修正）======
#
# 两个时间常量语义分离（勿混用）：
# - RETENTION_DAYS=180：保留期。DELETE 的 cutoff 由它派生（wall clock，
#   与 created_at 同一时间源，字典序比较即时间序）。
# - CLEANUP_INTERVAL_DAYS=30：清理动作的执行周期（节流判断用，
#   time.monotonic() 进程内计时——跨重启无效是预期，由启动链兜底一次）。
# 混用的后果：monotonic 算 cutoff 会在 NTP 跳变/长运行下清错行；
# wall clock 存节流状态会在时间回拨后"该清不清"。

RETENTION_DAYS = 180        # 审计保留 180 天
CLEANUP_INTERVAL_DAYS = 30  # 清理动作每 30 天执行一次（节流周期）
_DELETE_BATCH = 1000        # 分批 DELETE：单批上限，控制锁持有时间

_cleanup_last_mono = 0.0    # 进程内节流状态（monotonic；per-process，不持久化）
_cleanup_lock = threading.Lock()


def cleanup_expired_retention(force: bool = False) -> int:
    """清理超保留期的审计行。清理周期 30 天（节流），保留期 180 天。

    节流：距上次清理 < CLEANUP_INTERVAL_DAYS 直接跳过（monotonic 进程内
    计时）；force=True 跳过节流（测试/启动链首跑用）。

    多进程/重启行为（预期，非故障）：节流状态是 per-process 的——N 个
    worker 重启后最多 N 次并发 DELETE 检查；DELETE 按幂等条件
    （created_at < cutoff），并发重复执行最多删 0 行，无需分布式锁。

    低频实例超期风险（已知权衡）：惰性触发挂在 log_usage 上，长期无写入
    的实例可能延迟清理——延迟清理 ≠ 不清理，下次写入会补上；启动链兜底
    一次（重启即对齐）。

    DELETE 分批：两步查删（SELECT id LIMIT → DELETE IN 具体列表）——
    MySQL 禁 IN 子查询带 LIMIT（错误 1235），翻译层不重写该形态（复审
    2026-11-07 实测）。节流状态不持久化（meta 落库暂不上——复杂度换不来
    收益，重启过频再评估）。
    """
    global _cleanup_last_mono
    import datetime
    now_mono = time.monotonic()
    if not force:
        with _cleanup_lock:
            if (now_mono - _cleanup_last_mono) < CLEANUP_INTERVAL_DAYS * 86400:
                return 0  # 节流窗口内：跳过
    # cutoff 用 wall clock（与 created_at 同源，标准 SQL 比较）
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    try:
        from src.kernel.owner_db import db_pair
        db, db_lock = db_pair("runtime")
        with db_lock:
            while True:
                # 两步查删（复审修正）：MySQL 对 IN 子查询带 LIMIT 报 1235——
                # 先 SELECT id 分批取出，再按具体 id 列表 DELETE（双方言安全）
                ids = [r[0] for r in db.execute(
                    "SELECT id FROM grape_t_credential_usage_log WHERE created_at < ? LIMIT ?",
                    (cutoff, _DELETE_BATCH)).fetchall()]
                if not ids:
                    break
                placeholders = ",".join("?" for _ in ids)
                cur = db.execute(
                    f"DELETE FROM grape_t_credential_usage_log WHERE id IN ({placeholders})",
                    ids)
                batch_n = cur.rowcount or 0
                total += batch_n
                db.commit()
                if batch_n < _DELETE_BATCH:
                    break  # 本批不满 = 已清完
        with _cleanup_lock:
            _cleanup_last_mono = time.monotonic()
        return total
    except Exception as e:
        from src.kernel.config import log
        log.warning(f"usage_log retention cleanup failed: {e}")
        return total


def maybe_cleanup_expired_retention() -> int:
    """惰性触发入口（挂 log_usage）：节流判断 + 清理，异常全吞（不阻断写入）。"""
    try:
        return cleanup_expired_retention()
    except Exception:
        return 0


# ====== N4-p3 异步写入（主流程零阻塞）======

_queue: "queue.Queue | None" = None
_queue_lock = threading.Lock()
_worker_started = False

_QUEUE_MAX = 10000  # 有界队列：审计洪峰时丢弃最旧的（审计可容忍，主流程不可阻塞）


def _get_queue() -> "queue.Queue":
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=_QUEUE_MAX)
        return _queue


def _drain_worker() -> None:
    """后台消费线程：批量刷队列（daemon——进程退出时丢尾部可容忍，审计非强一致）。"""
    q = _get_queue()
    while True:
        item = q.get()
        batch = [item]
        # 攒批：最多 50 条或队列空
        while len(batch) < 50:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        try:
            _flush_batch(batch)
        except Exception as e:
            from src.kernel.config import log
            log.warning(f"usage_log async flush failed: {e}")
        for _ in batch:
            q.task_done()


def _flush_batch(batch: list[dict]) -> None:
    # N4-p3 修正（真根因）：sqlite3 同连接跨线程**共享事务态**——后台线程的
    # INSERT 落在主线程的隐式事务里，主线程随后的 commit/rollback 会连带
    # 提交/滚掉后台行（偶发丢行根因）。修 = 后台线程独立连接（独立事务），
    # busy_timeout 承接与主连接的写锁竞争；参数化仍守铁律 9。
    # BLK-1（B1 双后端兼容）：不再裸连 sqlite3——MySQL 后端下裸连等于把审计
    # 行写进本地不存在的 sqlite 文件（静默丢审计）。按 backend 分派：
    #   - MySQL：走 session_manager.db（MySQLCompatDB 翻译层，连接池借还，
    #     executemany 原生支持）；借还粒度即连接粒度，无跨线程事务态共享。
    #   - SQLite：保留独立连接事务隔离语义（N4-p3），仍参数化 + busy_timeout。
    rows = [(b["id"], b["credential_id"], b["credential_kind"], b["user_id"],
             b.get("repo_key", ""), b["initiating_actor_id"],
             b["executor_context"], b["operation"], b["created_at"])
            for b in batch]
    insert_sql = ("INSERT INTO grape_t_credential_usage_log (id, credential_id, credential_kind, "
                  "user_id, repo_key, initiating_actor_id, executor_context, operation, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)")
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() == "mysql":
        from src.kernel.owner_db import owner_db as _owner_db
        _sdb = _owner_db("runtime")
        _sdb.executemany(insert_sql, rows)
        _sdb.commit()
        return
    import sqlite3
    from src.kernel.session_manager import _db_path
    conn = sqlite3.connect(_db_path, timeout=5, check_same_thread=False)
    try:
        conn.executemany(insert_sql, rows)
        conn.commit()
    finally:
        conn.close()


def _ensure_worker() -> None:
    global _worker_started
    with _queue_lock:
        if _worker_started:
            return
        threading.Thread(target=_drain_worker, daemon=True,
                         name="usage-log-async").start()
        _worker_started = True


def list_by_credential(credential_id: str, limit: int = 100) -> list[dict]:
    """按凭据查使用历史（审计查询面）。"""
    from sqlalchemy import select
    from src.kernel.owner_db import owner_db as _owner_db
    db = _owner_db("runtime")
    from src.kernel.persistence.engine import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(core_table).where(core_table.c.credential_id == credential_id)
            .limit(limit)).mappings().all()
    return [dict(r) for r in rows]
