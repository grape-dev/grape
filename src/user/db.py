"""用户系统数据库：users/credentials/vms/skills 表管理"""
import json, hashlib, time, os, threading
from src.session_manager import db, db_lock
from src.config import log

# OAuth state 存储（内存，不依赖 SQLite 跨线程事务）
_oauth_store = {}
_oauth_lock = threading.Lock()


# ====== 用户账户表（合并 users + bot_accounts） ======
_USER_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    gitcode_api_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    gitcode_oauth_client_id TEXT DEFAULT '',
    gitcode_oauth_client_secret TEXT DEFAULT '',
    gitcode_oauth_redirect_uri TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    password TEXT DEFAULT '',
    bot_usernames TEXT DEFAULT '',
    webhook_url TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_user(username: str, password: str, created_by: int = 0) -> dict | None:
    """创建普通用户，返回用户信息或 None"""
    pw_hash = _hash_password(password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO user_accounts (username, password_hash, password, created_by) VALUES (?,?,?,?)",
                      (username, pw_hash, password, created_by))
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
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


def get_user_by_username(username: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, password_hash, created_by, created_at FROM user_accounts WHERE username=?", (username,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "created_by": row[3], "created_at": row[4]}


def list_users() -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts ORDER BY id")
        return [{"id": r[0], "username": r[1], "created_by": r[2], "created_at": r[3]} for r in c.fetchall()]


def delete_user(user_id: int) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_accounts WHERE id=?", (user_id,))
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
            c.execute("UPDATE user_accounts SET password_hash=?, password=? WHERE id=?", (pw_hash, new_password, user_id))
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


# ====== GitCode OAuth 联邦登录 ======


def _cleanup_expired_oauth_states():
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            db.commit()
    except Exception:
        pass


def save_oauth_state(state: str, provider: str, data: dict | None = None):
    """保存 OAuth state（防 CSRF，10 分钟过期，内存存储）"""
    expires = time.time() + 600
    with _oauth_lock:
        _oauth_store[state] = {"provider": provider, "data": data or {}, "expires": expires}
    log.info(f"save_oauth_state: state={state[:20]}... saved OK, total={len(_oauth_store)}")


def validate_oauth_state(state: str, provider: str) -> dict | None:
    """验证并消耗 OAuth state（查 + 删），返回 data 或 None"""
    with _oauth_lock:
        entry = _oauth_store.pop(state, None)
    if entry is None:
        log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
        return None
    if entry["provider"] != provider:
        log.warning(f"validate_oauth_state: provider mismatch")
        return None
    if time.time() > entry["expires"]:
        log.warning(f"validate_oauth_state: state expired")
        return None
    return entry["data"]







def get_user_by_gitcode_id(gitcode_id: str) -> dict | None:
    """通过 GitCode 用户 ID 查找本地用户"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts WHERE gitcode_id=?", (gitcode_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


def link_gitcode_user(user_id: int, gitcode_id: str, access_token: str, refresh_token: str = "") -> bool:
    """绑定本地用户与 GitCode 用户，存储 access_token 到 user_credentials"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_accounts SET gitcode_id=? WHERE id=?", (gitcode_id, user_id))
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) VALUES (?,?,?,datetime('now'))",
                      (user_id, "gitcode_oauth", json.dumps({
                          "gitcode_id": gitcode_id,
                          "access_token": access_token,
                          "refresh_token": refresh_token
                      })))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Link GitCode user failed: {e}")
        return False


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
    status TEXT DEFAULT '',
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
    """获取用户凭据（从 user_accounts 表读取）"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT * FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    if not row:
        return []
    d = dict(zip(USER_ACCOUNT_FIELDS, row))
    result = []
    if d.get("gitcode_api_token"):
        result.append({"provider": "gitcode", "config": {"token": d["gitcode_api_token"]}})
    if d.get("feishu_app_id") or d.get("feishu_app_secret"):
        cfg = {}
        if d.get("feishu_app_id"): cfg["app_id"] = d["feishu_app_id"]
        if d.get("feishu_app_secret"): cfg["app_secret"] = d["feishu_app_secret"]
        result.append({"provider": "feishu", "config": cfg})
    if d.get("huawei_cloud_ak") or d.get("huawei_cloud_sk"):
        cfg = {}
        if d.get("huawei_cloud_ak"): cfg["ak"] = d["huawei_cloud_ak"]
        if d.get("huawei_cloud_sk"): cfg["sk"] = d["huawei_cloud_sk"]
        result.append({"provider": "huawei_cloud", "config": cfg})
    if d.get("CODEARTS_CLI_AK") or d.get("CODEARTS_CLI_SK"):
        cfg = {}
        if d.get("CODEARTS_CLI_AK"): cfg["ak"] = d["CODEARTS_CLI_AK"]
        if d.get("CODEARTS_CLI_SK"): cfg["sk"] = d["CODEARTS_CLI_SK"]
        result.append({"provider": "codearts", "config": cfg})
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
        c.execute("SELECT id, name, host, port, ssh_user, ssh_password, status FROM user_vms WHERE user_id=? ORDER BY id", (user_id,))
        return [{"id": r[0], "name": r[1], "host": r[2], "port": r[3], "ssh_user": r[4], "ssh_password": r[5], "status": r[6]} for r in c.fetchall()]


def update_vm_status(vm_id: int, status: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_vms SET status=? WHERE id=?", (status, vm_id))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update VM status failed: {e}")
        return False


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


def clear_vm_repos(vm_id: int) -> bool:
    """清除指定云主机的所有仓库映射"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_vm_repos WHERE vm_id=?", (vm_id,))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Clear VM repos failed: {e}")
        return False


# ====== 自定义技能表 ======
_SKILLS_DDL = """
CREATE TABLE IF NOT EXISTS user_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, skill_name)
)
"""

# ====== 仓库技能映射表 ======
_REPO_SKILL_DDL = """
CREATE TABLE IF NOT EXISTS repo_skill_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    repo_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    UNIQUE (user_id, repo_key, event_type)
)
"""

# ====== 托管仓库表 ======
_MANAGED_REPOS_DDL = """
CREATE TABLE IF NOT EXISTS managed_repos (
    user_id INTEGER NOT NULL,
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    webhook_id INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, repo_owner, repo_name)
)
"""

_OAUTH_STATES_DDL = """
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    data_json TEXT DEFAULT '{}',
    expires REAL NOT NULL
)
"""


def init_db():
    """初始化用户相关表（幂等）"""
    with db_lock:
        db.execute(_USER_ACCOUNTS_DDL)
        db.execute(_CREDENTIALS_DDL)
        db.execute(_VMS_DDL)
        db.execute(_VM_REPOS_DDL)
        db.execute(_SKILLS_DDL)
        db.execute(_REPO_SKILL_DDL)
        db.execute(_MANAGED_REPOS_DDL)
        db.execute(_OAUTH_STATES_DDL)
        # 从旧表迁移数据（幂等）
        try:
            cnt = db.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
        except Exception:
            cnt = 0
        if cnt == 0:
            try:
                db.execute("""
                    INSERT OR IGNORE INTO user_accounts
                        (id, username, password_hash, gitcode_id,
                         gitcode_api_token, gitcode_webhook_token,
                         gitcode_oauth_client_id, gitcode_oauth_client_secret,
                         gitcode_oauth_redirect_uri,
                         feishu_app_id, feishu_app_secret,
                         huawei_cloud_ak, huawei_cloud_sk,
                         CODEARTS_CLI_AK, CODEARTS_CLI_SK,
                         password, bot_usernames, webhook_url,
                         created_by, created_at)
                    SELECT
                        u.id, u.username, u.password_hash, u.gitcode_id,
                        COALESCE(b.gitcode_api_token, ''),
                        COALESCE(b.gitcode_webhook_token, ''),
                        COALESCE(b.gitcode_oauth_client_id, ''),
                        COALESCE(b.gitcode_oauth_client_secret, ''),
                        COALESCE(b.gitcode_oauth_redirect_uri, ''),
                        COALESCE(b.feishu_app_id, ''),
                        COALESCE(b.feishu_app_secret, ''),
                        COALESCE(b.huawei_cloud_ak, ''),
                        COALESCE(b.huawei_cloud_sk, ''),
                        COALESCE(b.CODEARTS_CLI_AK, ''),
                        COALESCE(b.CODEARTS_CLI_SK, ''),
                        COALESCE(b.password, ''),
                        COALESCE(b.bot_usernames, ''),
                        COALESCE(b.webhook_url, ''),
                        u.created_by, u.created_at
                    FROM users u
                    LEFT JOIN bot_accounts b ON u.username = b.name
                """)
                db.commit()
            except Exception:
                pass
        # 修复 user_credentials.user_id 与 user_accounts.id 不匹配（合并后 id 可能变化）
        try:
            db.execute("""
                UPDATE user_credentials
                SET user_id = (SELECT ua.id FROM user_accounts ua, users u WHERE ua.username = u.username AND u.id = user_credentials.user_id)
                WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = user_credentials.user_id)
                  AND EXISTS (SELECT 1 FROM user_accounts ua, users u WHERE ua.username = u.username AND u.id = user_credentials.user_id)
            """)
            db.commit()
        except Exception:
            pass
        # 清理重复用户（同一 username 只保留最小 id）
        try:
            db.execute("""
                DELETE FROM user_accounts WHERE id NOT IN (
                    SELECT MIN(id) FROM user_accounts GROUP BY username
                )
            """)
            # 清理 user_credentials 中指向已删除用户的记录
            db.execute("""
                DELETE FROM user_credentials WHERE user_id NOT IN (SELECT id FROM user_accounts)
            """)
            # 修复 managed_repos 中的旧 user_id（通过旧 users 表映射）
            try:
                db.execute("""
                    UPDATE managed_repos SET user_id = (
                        SELECT ua.id FROM user_accounts ua, users u WHERE ua.username = u.username AND u.id = managed_repos.user_id
                    ) WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = managed_repos.user_id)
                """)
            except Exception:
                pass
            db.commit()
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN is_admin INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE user_vms ADD COLUMN status TEXT DEFAULT ''")
        except Exception:
            pass
        db.commit()
    _cleanup_expired_oauth_states()


# ====== 自定义技能 CRUD ======

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "custom")

def ensure_skill_dir(user_id: int, skill_name: str) -> str:
    """确保技能目录存在，返回路径"""
    path = os.path.join(SKILL_DIR, str(user_id), skill_name)
    os.makedirs(path, exist_ok=True)
    return path


def create_skill(user_id: int, skill_name: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR IGNORE INTO user_skills (user_id, skill_name) VALUES (?,?)",
                      (user_id, skill_name))
            db.commit()
            ensure_skill_dir(user_id, skill_name)
            return True
    except Exception as e:
        log.warning(f"Create skill failed: {e}")
        return False


def delete_skill(user_id: int, skill_name: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_skills WHERE user_id=? AND skill_name=?", (user_id, skill_name))
            c.execute("DELETE FROM repo_skill_mapping WHERE user_id=? AND skill_name=?", (user_id, skill_name))
            db.commit()
            # 删除技能目录
            import shutil
            path = os.path.join(SKILL_DIR, str(user_id), skill_name)
            if os.path.exists(path):
                shutil.rmtree(path)
            return True
    except Exception as e:
        log.warning(f"Delete skill failed: {e}")
        return False


def list_skills(user_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, skill_name, created_at FROM user_skills WHERE user_id=? ORDER BY id", (user_id,))
        return [{"id": r[0], "skill_name": r[1], "created_at": r[2]} for r in c.fetchall()]


def save_skill_md(user_id: int, skill_name: str, content: str) -> bool:
    """保存 SKILL.md 内容"""
    try:
        path = os.path.join(ensure_skill_dir(user_id, skill_name), "SKILL.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        log.warning(f"Save SKILL.md failed: {e}")
        return False


def get_skill_md(user_id: int, skill_name: str) -> str | None:
    """读取 SKILL.md 内容（大小写容错）"""
    base = os.path.join(SKILL_DIR, str(user_id), skill_name)
    for name in ("SKILL.md", "skill.md", "Skill.md"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


# ====== 仓库技能映射 CRUD ======

def set_repo_mapping(user_id: int, repo_key: str, event_type: str, skill_name: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO repo_skill_mapping (user_id, repo_key, event_type, skill_name) VALUES (?,?,?,?)",
                      (user_id, repo_key, event_type, skill_name))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Set repo mapping failed: {e}")
        return False


def get_skill_mappings(user_id: int, skill_name: str) -> list[dict]:
    """获取指定技能的所有映射"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, event_type FROM repo_skill_mapping WHERE user_id=? AND skill_name=? ORDER BY repo_key, event_type", (user_id, skill_name))
        return [{"repo_key": r[0], "event_type": r[1]} for r in c.fetchall()]


def clear_skill_mappings(user_id: int, skill_name: str) -> bool:
    """清除指定技能的所有映射"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM repo_skill_mapping WHERE user_id=? AND skill_name=?", (user_id, skill_name))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Clear skill mappings failed: {e}")
        return False


def delete_repo_mapping(user_id: int, repo_key: str, event_type: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM repo_skill_mapping WHERE user_id=? AND repo_key=? AND event_type=?", (user_id, repo_key, event_type))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete repo mapping failed: {e}")
        return False


def get_repo_mappings(user_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, event_type, skill_name FROM repo_skill_mapping WHERE user_id=? ORDER BY repo_key", (user_id,))
        return [{"repo_key": r[0], "event_type": r[1], "skill_name": r[2]} for r in c.fetchall()]


def get_skill_for_event(user_id: int, repo_key: str, event_type: str) -> str | None:
    """路由用：根据 user_id + repo + event 查找自定义技能名称"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name FROM repo_skill_mapping WHERE user_id=? AND repo_key=? AND event_type=?",
                  (user_id, repo_key, event_type))
        row = c.fetchone()
    return row[0] if row else None


# ====== 托管仓库 CRUD ======

def add_managed_repo(user_id: int, repo_owner: str, repo_name: str, full_name: str, webhook_id: int = 0, is_admin: bool = True) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO managed_repos (user_id, repo_owner, repo_name, full_name, webhook_id, is_admin) VALUES (?,?,?,?,?,?)",
                      (user_id, repo_owner, repo_name, full_name, webhook_id, int(is_admin)))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Add managed repo failed: {e}")
        return False


def get_managed_repos(user_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_owner, repo_name, full_name, webhook_id, is_admin, created_at FROM managed_repos WHERE user_id=? ORDER BY full_name", (user_id,))
        return [{"repo_owner": r[0], "repo_name": r[1], "full_name": r[2], "webhook_id": r[3], "is_admin": bool(r[4]), "created_at": r[5]} for r in c.fetchall()]


def update_managed_repo_admin(user_id: int, repo_owner: str, repo_name: str, is_admin: bool) -> bool:
    """更新托管仓库的 is_admin 标志"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET is_admin=? WHERE user_id=? AND repo_owner=? AND repo_name=?", (int(is_admin), user_id, repo_owner, repo_name))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update managed repo admin failed: {e}")
        return False


def remove_managed_repo(user_id: int, repo_owner: str, repo_name: str) -> dict | None:
    """删除托管仓库记录，返回被删除的记录（含 webhook_id）或 None"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT full_name, webhook_id FROM managed_repos WHERE user_id=? AND repo_owner=? AND repo_name=?", (user_id, repo_owner, repo_name))
            row = c.fetchone()
            if not row:
                return None
            c.execute("DELETE FROM managed_repos WHERE user_id=? AND repo_owner=? AND repo_name=?", (user_id, repo_owner, repo_name))
            db.commit()
            return {"full_name": row[0], "webhook_id": row[1]}
    except Exception as e:
        log.warning(f"Remove managed repo failed: {e}")
        return None


def get_all_managed_repos() -> list[dict]:
    """Admin 用：获取所有托管记录"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, repo_owner, repo_name, full_name, webhook_id, is_admin, created_at FROM managed_repos ORDER BY full_name")
        return [{"user_id": r[0], "repo_owner": r[1], "repo_name": r[2], "full_name": r[3], "webhook_id": r[4], "is_admin": bool(r[5]), "created_at": r[6]} for r in c.fetchall()]


# ====== 用户账户 CRUD ======

USER_ACCOUNT_FIELDS = [
    "id", "username", "password_hash", "gitcode_id",
    "gitcode_api_token", "gitcode_webhook_token",
    "gitcode_oauth_client_id", "gitcode_oauth_client_secret",
    "gitcode_oauth_redirect_uri",
    "feishu_app_id", "feishu_app_secret",
    "huawei_cloud_ak", "huawei_cloud_sk",
    "CODEARTS_CLI_AK", "CODEARTS_CLI_SK",
    "password", "bot_usernames", "webhook_url",
    "created_by", "created_at",
]


def get_all_user_accounts() -> list[dict]:
    """获取所有用户账户"""
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {','.join(USER_ACCOUNT_FIELDS)} FROM user_accounts ORDER BY username")
        rows = c.fetchall()
    result = []
    for r in rows:
        d = dict(zip(USER_ACCOUNT_FIELDS, r))
        d["name"] = d["username"]  # 向后兼容
        try:
            d["bot_usernames"] = json.loads(d.get("bot_usernames", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["bot_usernames"] = [s.strip() for s in d.get("bot_usernames", "").split(",") if s.strip()]
        result.append(d)
    return result


def get_user_account(username: str) -> dict | None:
    """获取单个用户账户"""
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {','.join(USER_ACCOUNT_FIELDS)} FROM user_accounts WHERE username=?", (username,))
        row = c.fetchone()
    if not row:
        return None
    d = dict(zip(USER_ACCOUNT_FIELDS, row))
    d["name"] = d["username"]  # 向后兼容
    try:
        d["bot_usernames"] = json.loads(d.get("bot_usernames", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["bot_usernames"] = [s.strip() for s in d.get("bot_usernames", "").split(",") if s.strip()]
    return d


def save_user_account(username: str, data: dict):
    """创建或更新用户账户（保留 id，不会因 INSERT OR REPLACE 产生新 id）"""
    bu = data.get("bot_usernames", [])
    if isinstance(bu, list):
        bu = json.dumps(bu, ensure_ascii=False)
    elif isinstance(bu, str):
        bu = json.dumps([s.strip() for s in bu.split(",") if s.strip()], ensure_ascii=False)
    else:
        bu = "[]"
    fields = {k: data.get(k, "") for k in USER_ACCOUNT_FIELDS if k not in ("id", "username", "created_at")}
    fields["bot_usernames"] = bu
    set_clause = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with db_lock:
        c = db.cursor()
        existing = c.execute("SELECT id FROM user_accounts WHERE username=?", (username,)).fetchone()
        if existing:
            c.execute(f"UPDATE user_accounts SET {set_clause} WHERE username=?", vals + [username])
        else:
            c.execute(f"INSERT INTO user_accounts ({','.join(fields.keys())},username) VALUES ({','.join('?' for _ in fields)},?)", vals + [username])
        db.commit()


def delete_user_account(username: str) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username=?", (username,))
        db.commit()
        return c.rowcount > 0


def resolve_user_account_by_webhook_token(token: str) -> str | None:
    """通过 webhook token 查找用户名"""
    if not token:
        return None
    with db_lock:
        c = db.cursor()
        c.execute("SELECT username FROM user_accounts WHERE gitcode_webhook_token=?", (token,))
        row = c.fetchone()
    return row[0] if row else None


def migrate_accounts_from_json():
    """从 accounts.json 迁移数据到 user_accounts 表（仅首次）"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs", "accounts.json")
    if not os.path.exists(path):
        return
    with db_lock:
        c = db.cursor()
        cnt = c.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
    if cnt > 0:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for a in data.get("accounts", []):
            name = a.get("name", "default")
            save_user_account(name, a)
            pwd = a.get("password", "")
            if pwd and name:
                user = get_user_by_username(name)
                if user:
                    update_password(user["id"], pwd)
                else:
                    create_user(name, pwd, 0)
        log.info(f"Migrated {len(data.get('accounts',[]))} accounts from accounts.json to DB")
    except Exception as e:
        log.warning(f"Migration from accounts.json failed: {e}")


# 启动时初始化 + 迁移
init_db()
migrate_accounts_from_json()

# 迁移旧路径 skills 到新路径
_OLD_SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "custom")
if os.path.isdir(_OLD_SKILL_DIR) and _OLD_SKILL_DIR != SKILL_DIR:
    try:
        import shutil
        for uid_dir in os.listdir(_OLD_SKILL_DIR):
            src = os.path.join(_OLD_SKILL_DIR, uid_dir)
            if os.path.isdir(src):
                dst = os.path.join(SKILL_DIR, uid_dir)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
                    log.info(f"Moved skill dir: {src} -> {dst}")
        if os.path.isdir(_OLD_SKILL_DIR) and not os.listdir(_OLD_SKILL_DIR):
            os.rmdir(_OLD_SKILL_DIR)
    except Exception as e:
        log.warning(f"Skill dir migration failed: {e}")
