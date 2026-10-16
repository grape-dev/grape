"""admin_accounts 数据访问——与 user_accounts 完全独立。

- 面向 /api/admin/* 与 /admin 前端
- 复用餐桌 session_manager.db / db_lock
- 默认账号种子在 db_setup._seed_admin_accounts() 完成
"""
import threading

from src.session_manager import db, db_lock
from src.config import log


def _hash_password_bcrypt(password: str) -> str:
    """bcrypt 哈希（与 grape_accounts 对齐，rounds=12）。"""
    from src.auth.password import hash_password
    return hash_password(password)


def get_admin_by_username(username: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, password_hash, is_super, token_version, created_at, last_login_at "
                  "FROM admin_accounts WHERE LOWER(username)=LOWER(?)", (username,))
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "password_hash": row[2],
        "is_super": row[3], "token_version": row[4],
        "created_at": row[5], "last_login_at": row[6],
    }


def verify_admin(username: str, password: str) -> dict | None:
    """校验 admin 用户名密码，成功返回含 token_version 的信息。

    P1-1: 渐进迁移——支持 bcrypt 和旧 SHA-256 两种格式。
    旧 SHA-256 验证成功后透明升级为 bcrypt。
    """
    from src.auth.password import verify_password, needs_upgrade
    admin = get_admin_by_username(username)
    if not admin:
        return None
    if not verify_password(password, admin["password_hash"]):
        return None
    # 透明升级：旧 SHA-256 → bcrypt
    if needs_upgrade(admin["password_hash"]):
        try:
            new_hash = _hash_password_bcrypt(password)
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE admin_accounts SET password_hash=? WHERE id=?",
                          (new_hash, admin["id"]))
                db.commit()
            log.info(f"Admin password hash upgraded to bcrypt for user: {username}")
        except Exception as e:
            log.warning(f"Admin password hash upgrade failed (login still succeeds): {e}")
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
    """P1-1: 改用 bcrypt 哈希（与 grape_accounts 对齐）。"""
    new_hash = _hash_password_bcrypt(new_password)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE admin_accounts SET password_hash=? WHERE id=?",
                  (new_hash, admin_id))
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
                f"SELECT id, username, email, nickname, status, token_version, "
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
            {"id": r[0], "username": r[1], "email": r[2] or "", "nickname": r[3] or "",
             "status": r[4], "token_version": r[5], "last_login_at": r[6],
             "created_at": r[7], "updated_at": r[8]}
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


# ===== OAuth Login Whitelist CRUD (Issue #332) =====

def list_login_whitelist() -> list[dict]:
    """List OAuth login whitelist entries."""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, gitcode_username, github_username, remark, created_at "
                "FROM login_whitelist ORDER BY created_at DESC"
            )
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"List login whitelist failed: {e}")
        return []
    return [
        {"id": r[0], "gitcode_username": r[1] or "", "github_username": r[2] or "",
         "remark": r[3] or "", "created_at": r[4] or ""}
        for r in rows
    ]


def add_login_whitelist(provider: str, username: str, remark: str = "") -> bool:
    """Add OAuth login whitelist entry.

    provider: 'gitcode' or 'github'
    """
    col = "gitcode_username" if provider == "gitcode" else "github_username"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT OR IGNORE INTO login_whitelist ({col}, remark) VALUES (?, ?)",
                (username, remark),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Add login whitelist failed: {e}")
        return False


def delete_login_whitelist(item_id: int) -> bool:
    """Delete OAuth login whitelist entry."""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM login_whitelist WHERE id=?", (item_id,))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete login whitelist failed: {e}")
        return False


def get_login_whitelist_enabled() -> bool:
    """Check if login whitelist is enabled.

    Delegates to config._is_login_whitelist_enabled() to ensure the admin
    panel displays the same state that is_user_allowed() actually enforces.
    """
    try:
        from src.config import _is_login_whitelist_enabled
        return _is_login_whitelist_enabled()
    except Exception:
        return False


def set_login_whitelist_enabled(enabled: bool) -> bool:
    """Set login whitelist on/off and invalidate config cache."""
    try:
        from src.user.db import set_meta_value
        set_meta_value("login_whitelist_enabled", "true" if enabled else "false")
        # Invalidate config.py 60s cache so toggle takes effect immediately
        try:
            from src.config import invalidate_login_whitelist_cache
            invalidate_login_whitelist_cache()
        except Exception:
            pass  # cache invalidation is best-effort
        return True
    except Exception as e:
        log.warning(f"Set login whitelist enabled failed: {e}")
        return False


# ===== User CRUD (Issue #332) =====

def create_grape_user(username: str, email: str, password: str, nickname: str = "") -> dict | str | None:
    """Manually create a user in grape_accounts.

    Uses bcrypt hashing (auth.password.hash_password) for security parity
    with the registration flow.  email is normalised (lower+strip, empty→NULL)
    and email_source='user' to match grape_account_db convention.

    Returns:
      dict on success;
      "username_conflict" if username already exists;
      "email_conflict" if email already exists;
      None on other errors.
    """
    from src.auth.password import hash_password
    pw_hash = hash_password(password)
    email = (email or "").lower().strip() or None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts (username, email, email_source, password_hash, nickname, status, token_version, email_verified) "
                "VALUES (?,?, 'user',?,?,1,0,1)",  # P2-3: email_verified=1 (admin-created users are pre-verified)
                (username, email, pw_hash, nickname),
            )
            db.commit()
            uid = c.lastrowid
            return {"id": uid, "username": username, "email": email or "", "nickname": nickname}
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err or "1062" in err:
            # Distinguish username vs email conflict by error message content.
            # SQLite: "UNIQUE constraint failed: grape_accounts.email"
            # MySQL: "Duplicate entry 'xxx' for key 'email'"
            if "email" in err:
                return "email_conflict"
            return "username_conflict"
        log.warning(f"Create grape user failed: {e}")
        return None


def update_grape_user(user_id: int, email: str = None, nickname: str = None) -> bool | str:
    """Edit user info (email, nickname).

    email semantics:
      None  → don't update (field omitted from request)
      ""    → clear email (set to NULL)
      "x@y" → update email (normalised: lower+strip, empty→NULL)
    nickname semantics:
      None  → don't update
      str   → update (including "" to clear nickname)

    Returns True on success, "conflict" on email UNIQUE violation, False on other errors.
    """
    fields = []
    params = []
    if email is not None:
        # Normalise: lower+strip, empty string → NULL (clear email)
        norm = (email or "").lower().strip() or None
        fields.append("email=?")
        params.append(norm)
    if nickname is not None:
        fields.append("nickname=?")
        params.append(nickname)
    if not fields:
        return False
    fields.append("updated_at=datetime('now')")
    params.append(user_id)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"UPDATE grape_accounts SET {','.join(fields)} WHERE id=?", params)
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err or "1062" in err:
            log.warning(f"Update grape user email conflict: user_id={user_id}")
            return "conflict"
        log.warning(f"Update grape user failed: {e}")
        return False


def reset_grape_user_password(user_id: int, new_password: str) -> bool:
    """Reset user password and invalidate old tokens.

    Uses bcrypt hashing (auth.password.hash_password) for security parity
    with the registration flow.
    """
    from src.auth.password import hash_password
    pw_hash = hash_password(new_password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET password_hash=?, token_version=token_version+1, updated_at=datetime('now') WHERE id=?",
                (pw_hash, user_id),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Reset grape user password failed: {e}")
        return False
