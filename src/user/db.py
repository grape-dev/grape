"""用户系统数据库：users/credentials/vms/skills 表管理"""
import json, hashlib, time
from src.session_manager import db, db_lock
from src.config import log


# ====== 用户表 ======
_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def init_db():
    """初始化用户相关表（幂等）"""
    with db_lock:
        db.execute(_USERS_DDL)
        try:
            db.execute("ALTER TABLE users ADD COLUMN created_by INTEGER DEFAULT 0")
        except Exception:
            pass
        db.commit()


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_user(username: str, password: str, created_by: int = 0) -> dict | None:
    """创建普通用户，返回用户信息或 None"""
    pw_hash = _hash_password(password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO users (username, password_hash, created_by) VALUES (?,?,?)",
                      (username, pw_hash, created_by))
            db.commit()
            uid = c.lastrowid
            log.info(f"User created: {username} (id={uid}, by={created_by})")
            return {"id": uid, "username": username}
    except Exception as e:
        log.warning(f"Create user failed: {e}")
        return None


def get_user(user_id: int) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM users WHERE id=?", (user_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


def get_user_by_username(username: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, password_hash, created_by, created_at FROM users WHERE username=?", (username,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "created_by": row[3], "created_at": row[4]}


def list_users() -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM users ORDER BY id")
        return [{"id": r[0], "username": r[1], "created_by": r[2], "created_at": r[3]} for r in c.fetchall()]


def delete_user(user_id: int) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM users WHERE id=?", (user_id,))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete user failed: {e}")
        return False


def update_password(user_id: int, new_password: str) -> bool:
    try:
        pw_hash = _hash_password(new_password)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update password failed: {e}")
        return False


def verify_user(username: str, password: str) -> dict | None:
    """验证用户名密码，成功返回用户信息，失败返回 None"""
    user = get_user_by_username(username)
    if not user:
        return None
    if user["password_hash"] != _hash_password(password):
        return None
    return {"id": user["id"], "username": user["username"], "created_by": user["created_by"], "created_at": user["created_at"]}


# ====== Token 管理 ======
# 用户 session token 与 admin token 分开管理
_user_tokens: dict[str, float] = {}
_user_token_map: dict[str, int] = {}  # token → user_id


def create_session_token(user_id: int) -> str:
    token = hashlib.sha256(f"user:{user_id}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
    _user_tokens[token] = time.time() + 86400
    _user_token_map[token] = user_id
    return token


def validate_token(token: str) -> int | None:
    """验证 token，返回 user_id 或 None"""
    if token in _user_tokens and time.time() < _user_tokens[token]:
        return _user_token_map.get(token)
    return None


def clear_token(token: str):
    _user_tokens.pop(token, None)
    _user_token_map.pop(token, None)


# 启动时初始化
init_db()
