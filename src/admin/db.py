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

# ===========================================================================
# D6: grape_accounts 用户管理查询 + 白名单 CRUD（需求 21）
# ===========================================================================

def list_grape_users(page: int = 1, per_page: int = 20, q: str = "") -> dict:
    """分页查询 grape_accounts（排除 status=2 已注销）。"""
    from src.session_manager import db, db_lock
    import time
    offset = (page - 1) * per_page
    try:
        with db_lock:
            c = db.cursor()
            where = "WHERE status != 2"
            params = []
            if q:
                where += " AND (username LIKE ? OR email LIKE ?)"
                params.extend([f"%{q}%", f"%{q}%"])
            c.execute(f"SELECT COUNT(*) FROM grape_accounts {where}", params)
            total = c.fetchone()[0]
            c.execute(
                f"SELECT id, username, email, status, token_version, "
                f"last_login_at, created_at, updated_at "
                f"FROM grape_accounts {where} "
                f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            )
            rows = c.fetchall()
    except Exception as e:
        from src.config import log
        log.warning(f"List grape users failed: {e}")
        return {"users": [], "total": 0, "page": page, "per_page": per_page}
    return {
        "users": [
            {"id": r[0], "username": r[1], "email": r[2] or "", "status": r[3],
             "token_version": r[4], "last_login_at": r[5], "created_at": r[6],
             "updated_at": r[7]}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page,
    }


def get_grape_user_detail(user_id: int) -> dict | None:
    """获取用户详情（含绑定信息和是否有密码）。"""
    from src.session_manager import db, db_lock
    from src.auth.grape_account_db import get_user_identities, has_password
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, username, email, status, token_version, "
                "last_login_at, created_at, updated_at "
                "FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
    except Exception as e:
        from src.config import log
        log.warning(f"Get grape user detail failed: {e}")
        return None
    if not row:
        return None
    identities = get_user_identities(user_id)
    return {
        "id": row[0], "username": row[1], "email": row[2] or "", "status": row[3],
        "token_version": row[4], "last_login_at": row[5], "created_at": row[6],
        "updated_at": row[7],
        "has_password": has_password(user_id),
        "identities": identities,
        "bindings": [i["provider"] for i in identities],
    }


def disable_grape_user(user_id: int, reason: str = "") -> bool:
    """禁用用户（status=0）。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET status=0, updated_at=datetime('now') "
                "WHERE id=? AND status=1",
                (user_id,),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        from src.config import log
        log.warning(f"Disable grape user failed: {e}")
        return False


def restore_grape_user(user_id: int) -> bool:
    """恢复用户（status=0 禁用 或 status=3 冷静期 → status=1）。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET status=1, token_version=token_version+1, "
                "updated_at=datetime('now') "
                "WHERE id=? AND status IN (0, 3)",
                (user_id,),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        from src.config import log
        log.warning(f"Restore grape user failed: {e}")
        return False


def list_signup_whitelist() -> list[dict]:
    """获取注册白名单列表。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, type, value, remark, created_at "
                "FROM signup_whitelist ORDER BY created_at DESC"
            )
            rows = c.fetchall()
    except Exception as e:
        from src.config import log
        log.warning(f"List signup whitelist failed: {e}")
        return []
    return [
        {"id": r[0], "type": r[1], "value": r[2], "remark": r[3], "created_at": r[4]}
        for r in rows
    ]


def add_signup_whitelist(wtype: str, value: str, remark: str = "") -> bool:
    """添加注册白名单。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO signup_whitelist (type, value, remark) VALUES (?,?,?)",
                (wtype, value, remark),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        from src.config import log
        log.warning(f"Add signup whitelist failed: {e}")
        return False


def delete_signup_whitelist(item_id: int) -> bool:
    """删除注册白名单。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM signup_whitelist WHERE id=?", (item_id,))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        from src.config import log
        log.warning(f"Delete signup whitelist failed: {e}")
        return False
