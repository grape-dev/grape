"""分布式锁——基于 distributed_locks 表。

跨实例互斥，per-key 粒度，TTL 超时自动释放。
应用层逻辑统一（MySQL/SQLite 不分叉）：先 INSERT，冲突后查过期，过期才条件 UPDATE。
db_lock（threading.RLock）保证单进程内原子，distributed_locks 表 PRIMARY KEY 保证跨进程互斥。

来源设计文档：docs/new-version3/knowledge-id-and-auth-design.md §2.6
"""
import socket
import secrets
import time

from src.config import log
from src.session_manager import db, db_lock

_holder = f"{socket.gethostname()}-{secrets.token_hex(4)}"


def acquire_lock(lock_key: str, ttl: int = 60) -> bool:
    """抢分布式锁，成功返回 True。

    ttl: 锁过期时间（秒），过期后可被其他实例抢占。
    """
    now = int(time.time())
    with db_lock:
        c = db.cursor()
        # 1. 先尝试 INSERT（主键冲突 = 已有锁）
        try:
            c.execute(
                "INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (lock_key, _holder, now, now + ttl, now, now))
            db.commit()
            return True  # INSERT 成功 = 拿到锁
        except Exception:
            db.rollback()  # INSERT 冲突后清理 cursor 状态（SQLite 需要）

        # 2. 查现有锁
        row = c.execute(
            "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
            (lock_key,)).fetchone()
        if not row:
            # 冲突后行不见了（被其他进程删了）→ 重试 INSERT
            try:
                c.execute(
                    "INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lock_key, _holder, now, now + ttl, now, now))
                db.commit()
                return True
            except Exception:
                return False

        # 3. 已过期 → 条件 UPDATE 覆盖（WHERE expires_at < now 保证原子）
        if int(row[1] or 0) < now:
            c.execute(
                "UPDATE distributed_locks SET holder=?, locked_at=?, "
                "expires_at=?, update_at=? "
                "WHERE lock_key=? AND expires_at < ?",
                (_holder, now, now + ttl, now, lock_key, now))
            db.commit()
            row2 = c.execute(
                "SELECT holder FROM distributed_locks WHERE lock_key=?",
                (lock_key,)).fetchone()
            return row2 and row2[0] == _holder

        # 4. 未过期 → 抢锁失败
        return False


def release_lock(lock_key: str):
    """释放锁。只删自己的锁，不会误删别人的。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM distributed_locks WHERE lock_key=? AND holder=?",
            (lock_key, _holder))
        db.commit()


def renew_lock(lock_key: str, ttl: int = 60) -> bool:
    """续期锁（只续自己的）。返回 True=续期成功，False=锁已被别人抢走。

    不依赖 cursor.rowcount（MySQL/pymysql 对 UPDATE 返回 rowcount=0 不可靠），
    改为 UPDATE + SELECT 验证 holder 是否还是自己。
    """
    now = int(time.time())
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE distributed_locks SET expires_at=?, update_at=? "
            "WHERE lock_key=? AND holder=?",
            (now + ttl, now, lock_key, _holder))
        db.commit()
        row = c.execute(
            "SELECT holder FROM distributed_locks WHERE lock_key=?",
            (lock_key,)).fetchone()
        return row and row[0] == _holder


def cleanup_expired_locks():
    """清理过期锁（后台定时调用，5 分钟一次）。

    只删不属于当前实例的过期锁，避免误删正在续期的锁。
    清理任务是防表膨胀兜底，业务抢锁不依赖定时清理。
    """
    now = int(time.time())
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM distributed_locks WHERE expires_at < ? AND holder != ?",
            (now, _holder))
        db.commit()
