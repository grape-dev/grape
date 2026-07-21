"""用户系统数据库：users/credentials/vms/skills 表管理"""
import json, hashlib, time, os
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


# ====== 凭据表 ======
_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,       -- 'gitcode','feishu','huawei','codearts'
    config_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, provider)
)
"""

# ====== 云主机表 ======
_VMS_DDL = """
CREATE TABLE IF NOT EXISTS user_vms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 22,
    ssh_user TEXT NOT NULL,
    ssh_password TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, name)
)
"""

_VM_REPOS_DDL = """
CREATE TABLE IF NOT EXISTS user_vm_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id INTEGER NOT NULL,
    repo_key TEXT NOT NULL,
    deploy_dir TEXT DEFAULT '',
    service_name TEXT DEFAULT '',
    branch TEXT DEFAULT 'master',
    UNIQUE (vm_id, repo_key)
)
"""


def init_db():
    """初始化用户相关表（幂等）"""
    with db_lock:
        db.execute(_USERS_DDL)
        db.execute(_CREDENTIALS_DDL)
        db.execute(_VMS_DDL)
        db.execute(_VM_REPOS_DDL)
        for col in ["created_by"]:
            try:
                db.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        db.commit()


# ====== 凭据 CRUD ======

def set_credential(user_id: int, provider: str, config: dict) -> bool:
    """保存凭据"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) VALUES (?,?,?,datetime('now'))",
                      (user_id, provider, json.dumps(config, ensure_ascii=False)))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Set credential failed: {e}")
        return False


def get_credentials(user_id: int) -> list[dict]:
    """获取用户所有凭据"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider, config_json, updated_at FROM user_credentials WHERE user_id=?", (user_id,))
        rows = c.fetchall()
    result = []
    for r in rows:
        try:
            cfg = json.loads(r[1])
        except Exception:
            cfg = {}
        result.append({"provider": r[0], "config": cfg, "updated_at": r[2]})
    return result


def get_credential(user_id: int, provider: str) -> dict | None:
    """获取指定凭据"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT config_json FROM user_credentials WHERE user_id=? AND provider=?", (user_id, provider))
        row = c.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def delete_credential(user_id: int, provider: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_credentials WHERE user_id=? AND provider=?", (user_id, provider))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete credential failed: {e}")
        return False


# ====== 云主机 CRUD ======

def add_vm(user_id: int, name: str, host: str, ssh_user: str, ssh_password: str, port: int = 22) -> int | None:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO user_vms (user_id, name, host, port, ssh_user, ssh_password) VALUES (?,?,?,?,?,?)",
                      (user_id, name, host, port, ssh_user, ssh_password))
            db.commit()
            return c.lastrowid
    except Exception as e:
        log.warning(f"Add VM failed: {e}")
        return None


def get_vms(user_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, host, port, ssh_user FROM user_vms WHERE user_id=? ORDER BY id", (user_id,))
        return [{"id": r[0], "name": r[1], "host": r[2], "port": r[3], "ssh_user": r[4]} for r in c.fetchall()]


def delete_vm(vm_id: int) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_vm_repos WHERE vm_id=?", (vm_id,))
            c.execute("DELETE FROM user_vms WHERE id=?", (vm_id,))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Delete VM failed: {e}")
        return False


def add_vm_repo(vm_id: int, repo_key: str, deploy_dir: str = "", service_name: str = "", branch: str = "master") -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO user_vm_repos (vm_id, repo_key, deploy_dir, service_name, branch) VALUES (?,?,?,?,?)",
                      (vm_id, repo_key, deploy_dir, service_name, branch))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Add VM repo failed: {e}")
        return False


def get_vm_repos(vm_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, deploy_dir, service_name, branch FROM user_vm_repos WHERE vm_id=?", (vm_id,))
        return [{"repo_key": r[0], "deploy_dir": r[1], "service_name": r[2], "branch": r[3]} for r in c.fetchall()]


def delete_vm_repo(vm_id: int, repo_key: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_vm_repos WHERE vm_id=? AND repo_key=?", (vm_id, repo_key))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete VM repo failed: {e}")
        return False


# 启动时初始化
init_db()
