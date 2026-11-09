"""obs_runtime_samples 采样全局租约（Issue #702）：多主单写收敛。

背景：四主控各自独立跑 60s 采样循环（`src/api/main.py _obs_sample_loop()`），无跨主控
互斥、`obs_runtime_samples` 又无唯一约束 → 同一分钟同 runtime 入库 N 条（N=主控数），
日增约 107 万行 = 设计容量（约 27 万行/天）的 4 倍，写放大与存储持续恶化。

方案（见 docs/issue-702/spec.md）：**全局单写者租约**——
- 锁 = 仓库既有 `distributed_locks` 表 TTL 租约（`src/lock.py`，SQLite/MySQL 统一语义，
  `db_setup` 已建表，后台循环既有惯例 autopilot_scheduler / table_maintenance 同款）；
- key = `obs_runtime_sampler`（全局单锁：采样是批量循环，无需 per-runtime）；
- TTL = 60s（一轮采样周期）：持有者每轮采样前 renew_lease 续期（UPDATE WHERE
  holder=self，只续自己的）；失联（进程崩溃/网络分区）无法续期 → lease 自然过期，
  其余主控**下一轮 acquire_lease 原子抢占**，接管时延 ≤ 60s + 一轮周期，远优于
  10min 要求；
- 清理职责与采样租约绑定（消除"多主各自清理"的 4 倍放大）：DELETE 留存清理只在
  持锁分支执行（`rollup.cleanup_expired_samples`，由 `sample_once_under_lease` 编排）。

不采用 MySQL GET_LOCK（Issue Spec 的备选 A）：本仓库有池化连接持命名锁泄漏、连接
以 Sleep 驻留池导致下次启动排队等满 timeout 的既有事故（src/mysql_compat.py
`dedicated_connection` 注释，实测 174/79 每次重启卡住需人工 KILL）；GET_LOCK 需池外
专用连接管理，复杂度更高，且测试后端强制 SQLite（tests/conftest.py）无法覆盖。

降级语义：锁表异常（DDL 损坏/DB 闪断）时 acquire 返回 False → 本轮采样跳过。
采样暂停不放大写入、读侧（runtime_history / SLO）不受影响——比"无锁继续写"安全。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.config import log

#: 采样全局租约 key（分布式锁表 distributed_locks 的 lock_key）
LEASE_KEY = "obs_runtime_sampler"

#: 租约 TTL（秒）＝ 一轮采样周期。失联后至多 TTL + 一轮周期内被其余主控接管。
LEASE_TTL_SEC = 60


def acquire_lease() -> bool:
    """抢采样租约：成功返回 True；他主持有未过期 / 锁表异常 → False（本轮跳过）。

    语义（复用 src/lock.py acquire_lock）：
    - 无锁 / 锁已过期 → INSERT/条件 UPDATE 原子抢到（失联接管走这条路径）；
    - 他主持有且未过期 → False；
    - 锁表异常 → False（fail-safe 降级：采样暂停，不放大写入，读侧不受影响）。
    """
    try:
        from src.lock import acquire_lock
        return bool(acquire_lock(LEASE_KEY, ttl=LEASE_TTL_SEC))
    except Exception as e:
        log.warning(f"obs sample lease acquire degraded (skip this tick): {e}")
        return False


def renew_lease() -> bool:
    """续期租约（只续自己持有的）：True=续期成功；False=锁已易主或不存在。

    持有者每个采样周期调用一次；失联（进程崩溃/网络分区）无法续期 → lease 过期
    后其余主控下一轮 acquire_lease() 原子抢占。TTL=60s（一轮周期），接管时延
    ≤ 60s + 一轮周期，满足"10min 内接管"的验收要求。
    """
    try:
        from src.lock import renew_lock
        return bool(renew_lock(LEASE_KEY, ttl=LEASE_TTL_SEC))
    except Exception as e:
        log.warning(f"obs sample lease renew degraded (lease may be taken over): {e}")
        return False


def release_lease() -> None:
    """释放采样租约（只删自己持有的行；异常静默——TTL 兜底自动过期）。"""
    try:
        from src.lock import release_lock
        release_lock(LEASE_KEY)
    except Exception as e:
        log.warning(f"obs sample lease release failed (TTL will expire it): {e}")


def lease_status() -> Optional[Dict[str, Any]]:
    """查看租约当前状态（排查/测试用）：holder 与到期时间戳（秒）；无锁返回 None。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
                (LEASE_KEY,)).fetchone()
        if not row:
            return None
        return {"holder": row[0], "expires_at": int(row[1] or 0)}
    except Exception:
        return None
