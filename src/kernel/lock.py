"""分布式锁——基于 distributed_locks 表（runtime 域基表）。

跨实例互斥，per-key 粒度，TTL 超时自动释放。
应用层逻辑统一（MySQL/SQLite 不分叉）：先 INSERT，冲突后查过期，过期才条件 UPDATE。
单进程内由 owner 域原语的 `db_lock`/事务保证原子，跨进程由 distributed_locks 表
PRIMARY KEY 保证互斥。

三库独立：`distributed_locks` 归 runtime 域（`configs/table_ownership.json`），
抢锁/续期/释放/清理四个协议全部由 `runtime.public` 承担，「任意拓扑」自适应连接
（自有 DSN 走 runtime 库，单库/测试拓扑回落进程库）——本模块不再持有任何 SQL，
也不再直连进程库（kernel 里查 runtime 表在三库拓扑下必然 1146）。

来源设计文档：docs/new-version3/knowledge-id-and-auth-design.md §2.6

访问路径：本模块不再函数内延迟 import `runtime.public`，而是经 kernel 端口 `pipeline`
调用那四个抢锁/续期/释放/清理原语（端口候选表已含 runtime.public —— 该端口承载 runtime/
mgmt 两域的基础设施原语，锁协议属同类基础设施）。符号名与签名不变，解析在调用点发生，
未绑定时由 PortNotBound 在调用处 fail-fast，kernel 侧不出现任何 domains import。
"""
import random
import socket
import secrets
import time

from src.kernel.ports import pipeline as _pipeline

_holder = f"{socket.gethostname()}-{secrets.token_hex(4)}"


def acquire_lock(lock_key: str, ttl: int = 60, holder: str = "") -> bool:
    """抢分布式锁，成功返回 True。

    ttl: 锁过期时间（秒），过期后可被其他实例抢占。
    holder: 可选，调用方自带的本把锁持有者标识（缺省用实例级标识）。
        传入 per-call 唯一值时，release_lock 只删本次调用自己的锁，
        避免同进程内不同调用方互相误删（QA 标注链路依赖这个语义）。
    """
    _h = (holder or "").strip() or _holder
    return _pipeline.acquire_distributed_lock(lock_key, _h, ttl, int(time.time()))


def release_lock(lock_key: str, holder: str = ""):
    """释放锁。只删自己的锁，不会误删别人的。

    holder: 与 acquire_lock 传入值一致时，只删该 holder 的锁（per-call 隔离）。
    """
    _h = (holder or "").strip() or _holder
    _pipeline.release_distributed_lock(lock_key, _h)


def renew_lock(lock_key: str, ttl: int = 60, holder: str = "") -> bool:
    """续期锁（只续自己的）。返回 True=续期成功，False=锁已被别人抢走。

    不依赖 cursor.rowcount（MySQL/pymysql 对 UPDATE 返回 rowcount=0 不可靠），
    改为 UPDATE + SELECT 验证 holder 是否还是自己（该判据在 owner 域原语内）。

    holder: 与 acquire_lock 传入值一致时，只续该 holder 的锁。branch_guard 的
        分支写租约按 ``task_id:进程holder`` 续期（Issue #2028），缺省仍用实例级标识。
    """
    _h = (holder or "").strip() or _holder
    return _pipeline.renew_distributed_lock(lock_key, _h, ttl, int(time.time()))


def repo_dispatch_lease_key(repo_key: str) -> str:
    """跨实例串行化仓库派发入队与组织解绑。"""
    return f"repo-dispatch:{repo_key or ''}"


def acquire_lock_with_retry(lock_key: str, ttl: int = 60, attempts: int = 20,
                            interval: float = 0.05,
                            wait_seconds: float | None = None,
                            jitter_ratio: float = 0.0) -> bool:
    """短等待抢锁；调用方负责在 finally 中 release。

    wait_seconds: 总等待预算（秒）。缺省 None → 保持旧行为（attempts×interval）；
        该原语被 runtime/engine 多处复用，默认路径语义不得变化（Issue #3963）。
        给定预算时按 deadline 收敛每次 sleep（不超过剩余预算），预算耗尽前末尾再补
        一次尝试；返回 False 表示预算内未抢到（调用方据此判 busy/排队失败）。
    jitter_ratio: 抖动比例（0=无抖动），仅预算路径生效——避免多实例在同一时刻齐抢。
    """
    attempts = max(1, int(attempts))
    if wait_seconds is None:
        for attempt in range(attempts):
            if acquire_lock(lock_key, ttl=ttl):
                return True
            if attempt < attempts - 1:
                time.sleep(max(0.0, float(interval)))
        return False

    budget = max(0.0, float(wait_seconds))
    base_interval = max(0.0, float(interval))
    deadline = time.monotonic() + budget
    while True:
        if acquire_lock(lock_key, ttl=ttl):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # 睡眠不超过剩余预算：预算尾段不做无意义的长等待，交给末尾补做的尝试
        pause = remaining if base_interval <= 0 else min(base_interval, remaining)
        if jitter_ratio > 0:
            pause *= 1.0 + random.uniform(-abs(float(jitter_ratio)), abs(float(jitter_ratio)))
        time.sleep(min(max(pause, 0.001), remaining))
    # 末尾补一次尝试：预算耗尽的边界（避免"最后一次睡眠跨过 deadline"吞掉一次机会）
    return acquire_lock(lock_key, ttl=ttl)


def cleanup_expired_locks():
    """清理过期锁（后台定时调用，5 分钟一次）。

    只删不属于当前实例的过期锁，避免误删正在续期的锁。
    清理任务是防表膨胀兜底，业务抢锁不依赖定时清理。
    """
    _pipeline.cleanup_expired_distributed_locks(int(time.time()), _holder)
