"""分布式锁——基于 distributed_locks 表。

跨实例互斥，per-key 粒度，TTL 超时自动释放。
MySQL 后端用 INSERT ... ON DUPLICATE KEY UPDATE（绕过 translate_sql 直接执行原生 SQL）。
SQLite 后端用 INSERT OR ABORT + 条件 UPDATE（db_lock 块内原子）。

来源设计文档：docs/new-version3/knowledge-id-and-auth-design.md §2.6
"""
import socket
import secrets
import time

from src.config import log
from src.session_manager import db, db_lock, _is_mysql_backend

_holder = f"{socket.gethostname()}-{secrets.token_hex(4)}"


def acquire_lock(lock_key: str, ttl: int = 60) -> bool:
    """抢分布式锁，成功返回 True。

    ttl: 锁过期时间（秒），过期后可被其他实例抢占。
    """
    if not _is_mysql_backend():
        # SQLite：INSERT OR ABORT + 条件 UPDATE（db_lock 块内原子）
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT OR ABORT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at, created_at) "
                    "VALUES (?, ?, strftime('%s','now'), strftime('%s','now')+?, strftime('%s','now'), strftime('%s','now'))",
                    (lock_key, _holder, ttl))
                db.commit()
                return True  # INSERT 成功 = 拿到锁
            except Exception:
                pass  # 主键冲突 → 查现有行

            row = c.execute(
                "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
                (lock_key,)).fetchone()
            if not row:
                return False
            # 已过期 → 覆盖
            if int(row[1] or 0) < int(time.time()):
                c.execute(
                    "UPDATE distributed_locks SET holder=?, locked_at=strftime('%s','now'), "
                    "expires_at=strftime('%s','now')+?, update_at=strftime('%s','now') "
                    "WHERE lock_key=? AND expires_at < strftime('%s','now')",
                    (_holder, ttl, lock_key))
                db.commit()
                row2 = c.execute(
                    "SELECT holder FROM distributed_locks WHERE lock_key=?",
                    (lock_key,)).fetchone()
                return row2 and row2[0] == _holder
            return False  # 未过期，抢锁失败

    # MySQL：绕过 translate_sql，直接执行原生 SQL
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at, created_at) "
            "VALUES (?, ?, UNIX_TIMESTAMP(), UNIX_TIMESTAMP()+?, UNIX_TIMESTAMP(), UNIX_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            "  holder=IF(expires_at < UNIX_TIMESTAMP(), VALUES(holder), holder), "
            "  locked_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(locked_at), locked_at), "
            "  expires_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(expires_at), expires_at), "
            "  update_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(update_at), update_at)",
            (lock_key, _holder, ttl))
        db.commit()
        row = c.execute(
            "SELECT holder FROM distributed_locks WHERE lock_key=?",
            (lock_key,)).fetchone()
        return row and row[0] == _holder


def release_lock(lock_key: str):
    """释放锁。只删自己的锁，不会误删别人的。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM distributed_locks WHERE lock_key=? AND holder=?",
            (lock_key, _holder))
        db.commit()


def cleanup_expired_locks():
    """清理过期锁（后台定时调用，5 分钟一次）。

    只删不属于当前实例的过期锁，避免误删正在续期的锁。
    清理任务是防表膨胀兜底，业务抢锁不依赖定时清理。
    """
    with db_lock:
        c = db.cursor()
        if _is_mysql_backend():
            c.execute(
                "DELETE FROM distributed_locks WHERE expires_at < UNIX_TIMESTAMP() AND holder != ?",
                (_holder,))
        else:
            c.execute(
                "DELETE FROM distributed_locks WHERE expires_at < strftime('%s','now') AND holder != ?",
                (_holder,))
        db.commit()
