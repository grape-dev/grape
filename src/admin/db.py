"""admin_accounts 数据访问——与 user_accounts 完全独立。

- 面向 /api/admin/* 与 /admin 前端
- 复用餐桌 session_manager.db / db_lock
- 默认账号种子在 db_setup._seed_admin_accounts() 完成
"""
import hashlib
import threading

from src.session_manager import db, db_lock
from src.config import log


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_admin_by_username(username: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, password_hash, is_super, token_version, created_at, last_login_at "
                  "FROM admin_accounts WHERE username=?", (username,))
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "password_hash": row[2],
        "is_super": row[3], "token_version": row[4],
        "created_at": row[5], "last_login_at": row[6],
    }


def verify_admin(username: str, password: str) -> dict | None:
    """校验 admin 用户名密码，成功返回含 token_version 的信息"""
    admin = get_admin_by_username(username)
    if not admin:
        return None
    if admin["password_hash"] != _hash_password(password):
        return None
    return admin


def get_admin_token_version(admin_id: int) -> int:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM admin_accounts WHERE id=?", (admin_id,))
        row = c.fetchone()
    return row[0] if row else -1


def increment_admin_token_version(admin_id: int) -> int:
    """改密/logout 时 +1——旧 admin JWT 全部失效，返回新版本号"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE admin_accounts SET token_version=token_version+1 WHERE id=?", (admin_id,))
        c.execute("SELECT token_version FROM admin_accounts WHERE id=?", (admin_id,))
        new_ver = c.fetchone()[0]
        db.commit()
    return new_ver


def update_admin_password(admin_id: int, new_password: str) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE admin_accounts SET password_hash=? WHERE id=?",
                  (_hash_password(new_password), admin_id))
        db.commit()
    return True


def touch_admin_login(admin_id: int):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE admin_accounts SET last_login_at=datetime('now') WHERE id=?", (admin_id,))
        db.commit()