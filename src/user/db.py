"""用户系统数据库：users/credentials/vms/skills 表管理"""
import json, hashlib, time, os
from src.session_manager import db, db_lock
from src.config import log

# OAuth state 存储（内存，不依赖 SQLite 跨线程事务）
# OAuth state 已改为 DB 共享存储（oauth_states 表），不再用进程内存


# ====== 用户账户表（合并 users + bot_accounts） ======
_USER_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    gitcode_api_token TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    gitcode_publish_repo TEXT DEFAULT '',
    github_publish_repo TEXT DEFAULT '',
    password TEXT DEFAULT '',
    token_version INTEGER DEFAULT 0,  -- JWT 撤销版本号——改密码/logout 时 +1
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    hw_iam_domain TEXT DEFAULT '',
    hw_iam_user TEXT DEFAULT '',
    hw_iam_password TEXT DEFAULT '',
    hw_iam_account_type TEXT DEFAULT 'iam',
    last_active_at TEXT DEFAULT NULL  -- 最后活跃时间（用户在线状态判定）
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
            c.execute("INSERT INTO user_accounts (username, password_hash, password, created_by, created_at) VALUES (?,?,?,?,datetime('now'))",
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
    """验证用户名密码，成功返回用户信息（含 token_version），失败返回 None"""
    user = get_user_by_username(username)
    if not user:
        return None
    if user["password_hash"] != _hash_password(password):
        return None
    token_version = get_token_version(user["id"])
    return {"id": user["id"], "username": user["username"], "created_by": user["created_by"], "created_at": user["created_at"], "token_version": token_version}


# ====== JWT token_version 管理 ======

def get_token_version(user_id: int) -> int:
    """获取用户的 token_version——JWT verify 时对比，不匹配则 token 失效"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    return (row[0] if row and row[0] is not None else 0)


def increment_token_version(user_id: int) -> None:
    """token_version+1——改密码/logout 时调用，使所有旧 token 失效"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_accounts SET token_version=token_version+1 WHERE id=?", (user_id,))
        db.commit()


def batch_update_last_active(uids: list) -> None:
    """批量更新用户最后活跃时间——定时任务调用，分批每 1000 个"""
    if not uids:
        return
    with db_lock:
        c = db.cursor()
        # 分批，每批 1000
        for i in range(0, len(uids), 1000):
            batch = uids[i:i+1000]
            placeholders = ",".join(["?"] * len(batch))
            c.execute(
                f"UPDATE user_accounts SET last_active_at=datetime('now') WHERE id IN ({placeholders})",
                batch,
            )
        db.commit()


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
    """保存 OAuth state（防 CSRF，10 分钟过期，DB 共享存储——集群多实例可用）。"""
    import json as _json
    expires = time.time() + 600
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO oauth_states (state, provider, data_json, expires) VALUES (?,?,?,?)",
                (state, provider, _json.dumps(data or {}), expires),
            )
            db.commit()
        log.info(f"save_oauth_state: state={state[:20]}... saved OK")
    except Exception as e:
        log.warning(f"save_oauth_state failed: {e}")


def validate_oauth_state(state: str, provider: str) -> dict | None:
    """验证并消耗 OAuth state（查 + 删），返回 data 或 None。"""
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT provider, data_json, expires FROM oauth_states WHERE state=?", (state,))
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
                db.commit()
        if not row:
            log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
            return None
        if row[0] != provider:
            log.warning(f"validate_oauth_state: provider mismatch")
            return None
        if time.time() > row[2]:
            log.warning(f"validate_oauth_state: state expired")
            return None
        return _json.loads(row[1] or "{}")
    except Exception as e:
        log.warning(f"validate_oauth_state failed: {e}")
        return None







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
    config_json MEDIUMTEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, provider)
)
"""

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


# ====== 托管仓库表 ======
_MANAGED_REPOS_DDL = """
CREATE TABLE IF NOT EXISTS managed_repos (
    repo_key TEXT PRIMARY KEY,             -- gitcode:owner/repo（跨平台全局唯一键）
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL,               -- owner/repo（无前缀，兼容/展示）
    webhook_id INTEGER DEFAULT 0,
    dev_scene TEXT DEFAULT '',      -- 开发场景（仓库已应用场景时写入 scene_id；默认置空=未配置，issue-39 方案 B）
    host_status TEXT DEFAULT '',     -- 托管状态（方案 F：''=完整 / bot_missing / webhook_missing / partial）
    bot_gitcode_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    bot_username TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_OAUTH_STATES_DDL = """
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    data_json MEDIUMTEXT DEFAULT '{}',
    expires REAL NOT NULL
)
"""


# ====== 用户仓库缓存表（user_repos）======
# 定义须在 _migrate_repo_key_tables 之前（init_db 会在模块底部调用迁移）
_USER_REPOS_DDL = """
CREATE TABLE IF NOT EXISTS user_repos (
    user_id INTEGER NOT NULL,
    repo_key TEXT NOT NULL,               -- gitcode:owner/repo（含平台前缀）
    owner TEXT DEFAULT '',
    name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    role TEXT DEFAULT '',
    is_admin INTEGER DEFAULT 0,
    is_join INTEGER DEFAULT 0,            -- 该用户是否已「加入」该仓库（用户级状态）
    created_at TEXT DEFAULT '',           -- GitCode 仓库创建时间（同级内按新建时间倒序用）
    synced_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, repo_key)
)
"""


def _migrate_repo_key_tables():
    """repo_key 统一整改迁移（幂等）。

    - managed_repos：旧结构含 user_id 列 → 重建为 repo_key 主键（值带 gitcode: 前缀），每仓库去重一行。
    - user_repos：旧结构含 full_name 列 → 重建为 repo_key + is_join（值带 gitcode: 前缀）。
    """
    with db_lock:
        c = db.cursor()
        # ===== managed_repos 迁移（独立异常，失败不影响 user_repos）=====
        try:
            mr_cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "user_id" in mr_cols and "repo_key" not in mr_cols:
                c.execute("ALTER TABLE managed_repos RENAME TO managed_repos_legacy")
                c.execute(_MANAGED_REPOS_DDL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE"))
                # 兼容不同旧表列：bot token / webhook token / bot_username 可能不存在
                def _agg_col(col):
                    return f"MAX(NULLIF({col},''))" if col in mr_cols else "''"
                bt = _agg_col("bot_gitcode_token")
                wt = _agg_col("gitcode_webhook_token")
                bu = _agg_col("bot_username")
                c.execute(
                    f"""INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name,
                               webhook_id, bot_gitcode_token, gitcode_webhook_token, bot_username, created_at)
                       SELECT 'gitcode:' || full_name, repo_owner, repo_name, full_name,
                              MAX(webhook_id), {bt}, {wt}, {bu}, MIN(created_at)
                       FROM managed_repos_legacy GROUP BY full_name""")
                legacy_pairs = c.execute(
                    "SELECT DISTINCT user_id, 'gitcode:' || full_name FROM managed_repos_legacy").fetchall()
                db.commit()
                try:
                    c.execute("DROP TABLE managed_repos_legacy")
                except Exception:
                    pass
                db.commit()
                if legacy_pairs:
                    try:
                        for uid, rk in legacy_pairs:
                            c.execute("UPDATE user_repos SET is_join=1 WHERE user_id=? AND repo_key=?", (uid, rk))
                        db.commit()
                    except Exception as e:
                        log.warning(f"Backfill user_repos.is_join failed: {e}")
        except Exception as e:
            log.warning(f"Migrate managed_repos failed: {e}")

        # ===== user_repos 迁移（独立异常）=====
        try:
            ur_cols = [r[1] for r in c.execute("PRAGMA table_info(user_repos)").fetchall()]
            if "full_name" in ur_cols and "repo_key" not in ur_cols:
                c.execute("ALTER TABLE user_repos RENAME TO user_repos_legacy")
                c.execute(_USER_REPOS_DDL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE"))
                def _ur_col(col):
                    return col if col in ur_cols else "''"
                _desc = _ur_col("description")
                _role = _ur_col("role")
                _isadmin = _ur_col("is_admin")
                _synced = _ur_col("synced_at")
                c.execute(
                    f"""INSERT INTO user_repos (user_id, repo_key, owner, name, description, role, is_admin, is_join, synced_at)
                       SELECT user_id, 'gitcode:' || full_name, owner, name, {_desc}, {_role}, {_isadmin}, 0, {_synced}
                       FROM user_repos_legacy""")
                try:
                    m_joined = c.execute(
                        "SELECT repo_key FROM managed_repos WHERE webhook_id>0 OR bot_username!=''").fetchall()
                    db.commit()
                    for (rk,) in m_joined:
                        c.execute("UPDATE user_repos SET is_join=1 WHERE repo_key=?", (rk,))
                    db.commit()
                except Exception:
                    pass
                try:
                    c.execute("DROP TABLE user_repos_legacy")
                except Exception:
                    pass
                db.commit()
            ur_cols2 = [r[1] for r in c.execute("PRAGMA table_info(user_repos)").fetchall()]
            if "is_join" not in ur_cols2:
                try:
                    c.execute("ALTER TABLE user_repos ADD COLUMN is_join INTEGER DEFAULT 0")
                    db.commit()
                except Exception:
                    pass
            if "created_at" not in ur_cols2:
                try:
                    c.execute("ALTER TABLE user_repos ADD COLUMN created_at TEXT DEFAULT ''")
                    db.commit()
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Migrate user_repos failed: {e}")

        # ===== 其余含 repo_key 的表：历史无前缀数据统一加 'gitcode:' 前缀（幂等）=====
        # 部署后所有查询均以带前缀 repo_key 进行；历史数据若无前缀将查不到。
        # 含 UNIQUE(repo_key) 的表逐行处理：加前缀后若与已有带前缀行冲突，删除旧无前缀行（保留最新带前缀会话）。
        _REPO_KEY_TABLES = (
            "events", "event_skipped", "devops_agent_session", "tasks",
            "chat_conversations", "chat_agent_session", "repo_im_group_binding",
        )
        for _tbl in _REPO_KEY_TABLES:
            try:
                _rk_cols = [r[1] for r in c.execute(f"PRAGMA table_info({_tbl})").fetchall()]
                if "repo_key" not in _rk_cols:
                    continue
                # 主键/唯一键列（用于删除冲突行）；无则退化为批量 UPDATE
                _pk = None
                for _ci in c.execute(f"PRAGMA table_info({_tbl})").fetchall():
                    if _ci[5] == 1:  # pk
                        _pk = _ci[1]
                        break
                _uniq = None
                for _idx in c.execute(f"PRAGMA index_list({_tbl})").fetchall():
                    if _idx[2] == 1:  # unique index
                        _cols = [x[2] for x in c.execute(f"PRAGMA index_info({_idx[1]})").fetchall()]
                        if _cols and _cols[0] == "repo_key":
                            _uniq = True
                            break
                if _pk and (_uniq or _pk == "repo_key"):
                    # 逐行处理 UNIQUE 表
                    _rows = c.execute(
                        f"SELECT {_pk}, repo_key FROM {_tbl} WHERE repo_key!='' AND repo_key NOT LIKE '%:%'").fetchall()
                    for _rid, _rk in _rows:
                        _newk = "gitcode:" + _rk
                        _conflict = c.execute(
                            f"SELECT 1 FROM {_tbl} WHERE repo_key=? LIMIT 1", (_newk,)).fetchone()
                        if _conflict:
                            c.execute(f"DELETE FROM {_tbl} WHERE {_pk}=?", (_rid,))
                        else:
                            c.execute(f"UPDATE {_tbl} SET repo_key=? WHERE {_pk}=?", (_newk, _rid))
                    db.commit()
                else:
                    c.execute(
                        f"UPDATE {_tbl} SET repo_key='gitcode:'||repo_key "
                        f"WHERE repo_key!='' AND repo_key NOT LIKE '%:%'"
                    )
                    db.commit()
            except Exception as e:
                log.warning(f"Migrate {_tbl} repo_key prefix failed: {e}")
        # skill 表 repo 级记录：owner_id 存 repo_key（scope='repo'）
        try:
            c.execute(
                "UPDATE skill SET owner_id='gitcode:'||owner_id "
                "WHERE scope='repo' AND owner_id!='' AND owner_id NOT LIKE '%:%'"
            )
            db.commit()
        except Exception as e:
            log.warning(f"Migrate skill repo_key prefix failed: {e}")

        # 幂等清理：迁移后遗留的 legacy 表删除（如 managed_repos_legacy / user_repos_legacy）
        try:
            for _leg in ("managed_repos_legacy", "user_repos_legacy"):
                _exists = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_leg,)).fetchone()
                if _exists:
                    c.execute(f"DROP TABLE IF EXISTS {_leg}")
                    db.commit()
                    log.info(f"Migrate repo_key: dropped leftover {_leg}")
        except Exception as e:
            log.warning(f"Cleanup legacy repo tables failed: {e}")

        # 幂等补充：managed_repos 已有表加 dev_scene 列（开发场景持久化）
        try:
            _mr_cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "dev_scene" not in _mr_cols:
                c.execute("ALTER TABLE managed_repos ADD COLUMN dev_scene TEXT DEFAULT 'default'")
                db.commit()
                log.info("Migrate repo_key: added managed_repos.dev_scene")
        except Exception as e:
            log.warning(f"Add managed_repos.dev_scene failed: {e}")


def init_db():
    """初始化用户相关表（幂等）"""
    with db_lock:
        db.execute(_USER_ACCOUNTS_DDL)
        # 兼容旧列迁移（幂等）：nickname 列
        for col in ["nickname"]:
            try:
                db.execute(f"ALTER TABLE user_accounts ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
        # 兼容旧列迁移（幂等）：last_active_at（用户在线状态判定）
        try:
            db.execute("ALTER TABLE user_accounts ADD COLUMN last_active_at TEXT DEFAULT NULL")
        except Exception:
            pass
        # 兼容旧列迁移（幂等）：华为云 IAM 列（存量库升级；CREATE TABLE 只影响新库）
        for col in ["hw_iam_domain", "hw_iam_user", "hw_iam_password", "hw_iam_account_type"]:
            try:
                db.execute(f"ALTER TABLE user_accounts ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
        db.execute(_CREDENTIALS_DDL)
        db.execute(_MANAGED_REPOS_DDL)
        db.execute(_OAUTH_STATES_DDL)
        # repo_key 统一整改迁移（managed_repos 去 user_id + user_repos 加前缀/is_join）
        _migrate_repo_key_tables()
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
                         gitcode_api_token,
                         feishu_app_id, feishu_app_secret,
                         huawei_cloud_ak, huawei_cloud_sk,
                         CODEARTS_CLI_AK, CODEARTS_CLI_SK,
                         password,
                         created_by, created_at)
                    SELECT
                        u.id, u.username, u.password_hash, u.gitcode_id,
                        COALESCE(b.gitcode_api_token, ''),
                        COALESCE(b.feishu_app_id, ''),
                        COALESCE(b.feishu_app_secret, ''),
                        COALESCE(b.huawei_cloud_ak, ''),
                        COALESCE(b.huawei_cloud_sk, ''),
                        COALESCE(b.CODEARTS_CLI_AK, ''),
                        COALESCE(b.CODEARTS_CLI_SK, ''),
                        COALESCE(b.password, ''),
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
            db.execute("ALTER TABLE managed_repos ADD COLUMN bot_gitcode_token TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN gitcode_webhook_token TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN bot_username TEXT DEFAULT ''")
        except Exception:
            pass
        # 迁移 user_accounts 废弃字段 → 正确位置（须在 DROP COLUMN 之前执行）
        try:
            c = db.cursor()
            ua_cols = [r[1] for r in c.execute("PRAGMA table_info(user_accounts)").fetchall()]
            # 1. webhook_url → meta 表（全局配置，取首个非空值）
            if "webhook_url" in ua_cols:
                c.execute("SELECT webhook_url FROM user_accounts WHERE webhook_url IS NOT NULL AND webhook_url!='' LIMIT 1")
                row = c.fetchone()
                if row and row[0]:
                    c.execute("SELECT COUNT(*) FROM meta WHERE key='webhook_url'")
                    if c.fetchone()[0] == 0:
                        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('webhook_url', ?)", (row[0],))
            # 2. OAuth 配置 → meta 表（default 账户清理时已完成，此处兜底）
            if "gitcode_oauth_client_id" in ua_cols:
                c.execute("SELECT gitcode_oauth_client_id, gitcode_oauth_client_secret, gitcode_oauth_redirect_uri FROM user_accounts WHERE gitcode_oauth_client_id IS NOT NULL AND gitcode_oauth_client_id!='' LIMIT 1")
                row = c.fetchone()
                if row:
                    for meta_key, val in zip(("oauth_gitcode_client_id", "oauth_gitcode_client_secret", "oauth_gitcode_redirect_uri"), row):
                        if val:
                            c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                            if c.fetchone()[0] == 0:
                                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (meta_key, val))
            # 3. bot_usernames → managed_repos.bot_username（对每个配置了 bot_usernames 的账户）
            if "bot_usernames" in ua_cols:
                c.execute("SELECT username, bot_usernames FROM user_accounts WHERE bot_usernames IS NOT NULL AND bot_usernames!=''")
                for uname, raw in c.fetchall():
                    try:
                        names = json.loads(raw)
                        if not isinstance(names, list):
                            names = []
                    except (json.JSONDecodeError, TypeError):
                        names = [s.strip() for s in raw.split(",") if s.strip()]
                    if not names:
                        continue
                    bot_name = names[0]
                    c.execute("UPDATE managed_repos SET bot_username=? WHERE full_name=? AND (bot_username='' OR bot_username IS NULL)", (bot_name, uname))
                    c.execute("UPDATE managed_repos SET bot_username=? WHERE (repo_owner=? OR full_name=?) AND (bot_username='' OR bot_username IS NULL)", (bot_name, uname, uname))
                # 兜底：managed_repos 有 bot_gitcode_token 但无 bot_username 时设置默认值
                c.execute("UPDATE managed_repos SET bot_username=COALESCE(NULLIF(bot_username,''), 'devops-bot') WHERE bot_username='' OR bot_username IS NULL")
            db.commit()
        except Exception as e:
            log.warning(f"Migrate user_accounts deprecated fields failed: {e}")
        # 收敛 user_accounts：移除废弃列（须在所有读取迁移之后执行）
        try:
            c = db.cursor()
            ua_cols = [r[1] for r in c.execute("PRAGMA table_info(user_accounts)").fetchall()]
            for col in ["gitcode_webhook_token", "gitcode_oauth_client_id", "gitcode_oauth_client_secret",
                        "gitcode_oauth_redirect_uri", "webhook_url", "bot_usernames"]:
                if col in ua_cols:
                    try:
                        db.execute(f"ALTER TABLE user_accounts DROP COLUMN {col}")
                    except Exception:
                        pass
            db.commit()
            log.info("Cleaned up user_accounts deprecated columns (webhook_token/oauth/webhook_url/bot_usernames)")
        except Exception as e:
            log.warning(f"Cleanup user_accounts columns failed: {e}")
        db.commit()
    _cleanup_expired_oauth_states()


# ====== 自定义技能 CRUD ======

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "custom")

def ensure_skill_dir(user_id: int, skill_name: str) -> str:
    """确保技能目录存在，返回路径（个人技能）"""
    path = os.path.join(SKILL_DIR, str(user_id), skill_name)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_repo_skill_dir(repo_key: str, skill_name: str) -> str:
    """确保仓库技能目录存在，返回路径（skills/repo/{owner}/{repo}/{skill}）"""
    from src.repo_key import from_repo_key
    path = os.path.join(os.path.dirname(SKILL_DIR), "repo", from_repo_key(repo_key), skill_name)
    os.makedirs(path, exist_ok=True)
    return path


def get_skill_by_id(skill_id) -> dict | None:
    """按 skill 表主键 id 查询 skill 信息（含 skill_name、skill_path、scope、agent_type、content_hash）"""
    if not skill_id:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, skill_name, display_name, description, agent_type, skill_path, file_path, scope, owner_id, content_hash FROM skill WHERE id=? AND is_active=1", (int(skill_id),))
            row = c.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "skill_name": row[1], "display_name": row[2] or row[1],
                "description": row[3] or "", "agent_type": row[4] or "",
                "skill_path": row[5] or row[1], "file_path": row[6] or "",
                "scope": row[7] or "official", "owner_id": row[8] or "",
                "content_hash": row[9] or "",
            }
    except Exception as e:
        log.warning(f"get_skill_by_id failed: {e}")
        return None


def get_skill_content_hash(skill_name: str, scope: str = "official") -> tuple[int, str, str]:
    """读取技能的主键 id、content_hash 和 file_path（变更检测用）。
    返回 (id, content_hash, file_path)，id=0 表示不存在。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, content_hash, file_path FROM skill WHERE skill_name=? AND scope=? LIMIT 1", (skill_name, scope))
            row = c.fetchone()
            if not row:
                return (0, "", "")
            return (row[0], row[1] or "", row[2] or "")
    except Exception as e:
        log.warning(f"get_skill_content_hash failed: {e}")
        return (0, "", "")


def update_skill_content_hash(skill_id: int, content_hash: str, file_path: str = "") -> bool:
    """按主键 id 更新技能的 content_hash 和 file_path（OBS 同步后调用）"""
    try:
        with db_lock:
            c = db.cursor()
            if file_path:
                c.execute("UPDATE skill SET content_hash=?, file_path=? WHERE id=?", (content_hash, file_path, skill_id))
            else:
                c.execute("UPDATE skill SET content_hash=? WHERE id=?", (content_hash, skill_id))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"update_skill_content_hash failed: {e}")
        return False


def resolve_skill_id(skill_name: str, owner_id: str = "") -> int:
    """按 skill_name 匹配 skill 表拿主键 id（official 优先，其次 user/repo 匹配 owner）"""
    if not skill_name:
        return 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='official' AND is_active=1 LIMIT 1", (skill_name,))
            row = c.fetchone()
            if row:
                return row[0]
            if owner_id:
                c.execute("SELECT id FROM skill WHERE skill_name=? AND (scope='user' OR scope='repo') AND owner_id=? AND is_active=1 LIMIT 1", (skill_name, owner_id))
                row = c.fetchone()
                if row:
                    return row[0]
    except Exception as e:
        log.warning(f"resolve_skill_id failed: {e}")
    return 0


def get_skill_path(skill_id) -> str:
    """按 skill_id 返回 skill 的实际 SKILL.md 绝对路径（official/user/repo scope 处理）"""
    skill = get_skill_by_id(skill_id)
    if not skill:
        return ""
    from src.config import SKILLS_DIR
    sp = skill.get("skill_path") or skill.get("skill_name") or ""
    if skill.get("scope") == "user":
        return os.path.join(SKILLS_DIR, "custom", skill.get("owner_id") or "", sp, "SKILL.md")
    if skill.get("scope") == "repo":
        from src.repo_key import from_repo_key
        return os.path.join(SKILLS_DIR, "repo", from_repo_key(skill.get("owner_id") or ""), sp, "SKILL.md")
    # 官方技能：优先按 configs/skill-description.json 的 path 解析（skills/<agent_type>/<skill>/SKILL.md），
    # 兼容旧平铺布局（skills/<skill>/SKILL.md）
    from src.config import resolve_official_skill_path
    resolved = resolve_official_skill_path(skill.get("skill_name") or sp)
    if resolved:
        return resolved
    return os.path.join(SKILLS_DIR, sp, "SKILL.md")


# ====== OBS 技能库辅助（obs-skill-storage-design.md §3 N9）======

def get_skill_object_key(skill_id) -> str:
    """按 skill_id 返回 OBS 对象键（v2.5：读 file_path——真实存储路径；无则空串）。"""
    skill = get_skill_by_id(skill_id)
    if not skill:
        return ""
    fp = (skill.get("file_path") or "").strip()
    # v2.5 字段职责分离：file_path 非空即 OBS 模式（不再特征判断 skill_path）
    if fp:
        return fp
    # 兼容 v2.4 存量（skill_path 曾存对象键）
    sp = (skill.get("skill_path") or "").strip()
    if "/skills/" in sp and sp.endswith(".zip"):
        return sp
    return ""


def get_skill_current_version(skill_id) -> int:
    """（v2.4 废弃保留：固定 key 模型无版本概念，返回 0 兼容旧调用。）"""
    return 0


def upsert_skill_obs(scope: str, owner: str, skill_name: str, agent_type: str,
                     description: str, object_key: str, created_by: str = "") -> bool:
    """OBS 模式技能记录 upsert（存在则更新固定 key，不存在则插入；v2.5 file_path + by 字段）。

    scope: user/repo；owner: user_id 或 repo_key。返回是否成功。
    """
    try:
        with db_lock:
            c = db.cursor()
            if scope == "repo":
                c.execute(
                    "SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?",
                    (skill_name, owner))
            else:
                c.execute(
                    "SELECT id FROM skill WHERE skill_name=? AND scope='user' AND owner_id=?",
                    (skill_name, owner))
            row = c.fetchone()
            if row:
                c.execute(
                    "UPDATE skill SET file_path=?, display_name=?, "
                    "agent_type=?, description=?, updated_by=?, is_active=1, updated_at=datetime('now') WHERE id=?",
                    (object_key, skill_name, agent_type or "", description or "",
                     created_by or "", row[0]))
            else:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                    "file_path, scope, owner_id, created_by, updated_by, is_active) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (skill_name, skill_name, description or "", agent_type or "",
                     object_key, scope, owner, created_by or "", created_by or ""))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"upsert_skill_obs failed: {e}")
        return False



def create_skill(user_id: int, skill_name: str, agent_type: str = "", description: str = "",
                 parent_agent_type: str = "", scope: str = "user", owner_id: str = "",
                 created_by: str = "") -> bool:
    try:
        with db_lock:
            c = db.cursor()
            cb = created_by or str(user_id)
            if scope == "repo":
                c.execute("INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, created_by, updated_by) VALUES (?,?,?,?,?,'repo',?,?,?)",
                          (skill_name, skill_name, description, agent_type, skill_name, owner_id or str(user_id), cb, cb))
            else:
                c.execute("INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, created_by, updated_by) VALUES (?,?,?,?,?,'user',?,?,?)",
                          (skill_name, skill_name, description, agent_type, skill_name, str(user_id), cb, cb))
            db.commit()
            if scope == "repo":
                ensure_repo_skill_dir(owner_id or str(user_id), skill_name)
            else:
                ensure_skill_dir(user_id, skill_name)
            return True
    except Exception as e:
        log.warning(f"Create skill failed: {e}")
        return False


def delete_skill(user_id: int, skill_name: str, scope: str = "user", owner_id: str = "") -> bool:
    try:
        with db_lock:
            c = db.cursor()
            if scope == "repo":
                c.execute("DELETE FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?",
                          (skill_name, owner_id or str(user_id)))
            else:
                c.execute("DELETE FROM skill WHERE skill_name=? AND scope='user' AND owner_id=?",
                          (skill_name, str(user_id)))
            db.commit()
            deleted = c.rowcount > 0
            # 删除技能目录
            import shutil
            if scope == "repo":
                from src.repo_key import from_repo_key
                path = os.path.join(os.path.dirname(SKILL_DIR), "repo", from_repo_key(owner_id or str(user_id)), skill_name)
            else:
                path = os.path.join(SKILL_DIR, str(user_id), skill_name)
            if os.path.exists(path):
                shutil.rmtree(path)
            return deleted
    except Exception as e:
        log.warning(f"Delete skill failed: {e}")
        return False


def list_skills(user_id: int) -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, skill_name, agent_type, description, scope, owner_id, file_path FROM skill WHERE scope='user' AND owner_id=? ORDER BY id", (str(user_id),))
        return [{"id": r[0], "skill_name": r[1], "agent_type": r[2] or "", "description": r[3] or "", "scope": r[4], "owner_id": r[5], "file_path": r[6] or "", "created_at": ""} for r in c.fetchall()]


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




# ====== 托管仓库 CRUD ======

def add_managed_repo(repo_key: str, repo_owner: str, repo_name: str, webhook_id: int = 0,
                     bot_gitcode_token: str = "", gitcode_webhook_token: str = "",
                     bot_username: str = "", dev_scene: str = "") -> bool:
    """写入仓库级托管配置（repo_key 带平台前缀，一仓库一行 upsert）。"""
    from src.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo_key)
    full_name = from_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) VALUES (?,?,?,?,?,?,?,?,?)",
                (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene or "", bot_gitcode_token, gitcode_webhook_token, bot_username))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Add managed repo failed: {e}")
        return False


def set_repo_scene(repo_key: str, scene_id: str):
    """写入仓库开发场景（managed_repos.dev_scene），幂等。

    issue-39 方案 B：无「default」场景，scene_id 为空即清空（仓库未配置场景）。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=?", (scene_id or "", repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Set repo scene failed: {e}")


def get_repo_scene(repo_key: str) -> str:
    """读取仓库开发场景（managed_repos.dev_scene），无记录或未设置返回空字符串。"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            return (row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"Get repo scene failed: {e}")
        return ""


def set_host_status(repo_key: str, status: str = ""):
    """写入仓库托管状态（managed_repos.host_status），幂等（方案 F）。

    空串=托管完整；bot_missing=bot 未成功加入；webhook_missing=webhook 未创建；partial=流程部分执行。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET host_status=? WHERE repo_key=?", (status or "", repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Set host status failed: {e}")


def get_host_status(repo_key: str) -> str:
    """读取仓库托管状态（managed_repos.host_status），无记录返回空串。"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT host_status FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            return (row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"Get host status failed: {e}")
        return ""


def _get_managed_webhook_id(repo_key: str) -> int:
    """读取仓库本地记录的 webhook_id（方案 X：以本地 >0 判定 webhook 存在，不远程查）。"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT webhook_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            return (row[0] or 0) if row else 0
    except Exception as e:
        log.warning(f"Get managed webhook id failed: {e}")
        return 0


def get_global_bot_credential() -> tuple[str, str]:
    """读取全局 bot 配置，返回 (bot_token, bot_username)。"""
    import json, os as _os
    _gbt_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
                              "configs", "global-bot-token.json")
    try:
        if _os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            tok = d.get("gitcode_api_token", "") or ""
            us = (d.get("bot_usernames") or [])
            user = us[0] if us else ""
            return tok, user
    except Exception as e:
        log.warning(f"get_global_bot_credential failed: {e}")
    return "", ""


def get_managed_repos(user_id: int) -> list[dict]:
    """返回该用户可见（JOIN user_repos）的托管仓库；对外 full_name 无前缀、repo_key 带前缀。"""
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                "COALESCE(u.is_join,0), COALESCE(u.is_admin,0) "
                "FROM managed_repos m "
                "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                "WHERE u.repo_key IS NOT NULL "
                "ORDER BY m.full_name", (user_id,))
        except Exception as e:
            log.warning(f"get_managed_repos join failed: {e}")
            c.execute(
                "SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, "
                "bot_gitcode_token, gitcode_webhook_token, bot_username, created_at, 0, 0 "
                "FROM managed_repos ORDER BY full_name")
        rows = c.fetchall()
        # autocommit=False 下 SELECT 会打开事务，同线程连接后续 SELECT 看到旧快照。
        # commit 释放事务，确保下次查询看到最新数据（修复 /repo 返回老列表）。
        db.commit()
        return [{
            "repo_key": r[0], "repo_owner": r[1], "repo_name": r[2],
            "full_name": from_repo_key(r[0]), "webhook_id": r[4],
            "bot_gitcode_token": r[5] or "", "gitcode_webhook_token": r[6] or "",
            "bot_username": r[7] or "", "created_at": r[8],
            "is_join": bool(r[9]), "is_admin": bool(r[10]),
        } for r in rows]


def update_managed_repo_admin(user_id: int, repo_key: str, is_admin: bool) -> bool:
    """更新该用户在 user_repos 的管理员状态（用户级）"""
    from src.repo_key import to_repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_repos SET is_admin=? WHERE user_id=? AND repo_key=?", (int(is_admin), user_id, to_repo_key(repo_key)))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update managed repo admin failed: {e}")
        return False


def update_managed_repo_bot_token(repo_key: str, bot_token: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET bot_gitcode_token=? WHERE repo_key=?", (bot_token, repo_key))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update managed repo bot token failed: {e}")
        return False


def update_managed_repo_bot_username(repo_key: str, bot_username: str) -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET bot_username=? WHERE repo_key=?", (bot_username, repo_key))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update managed repo bot username failed: {e}")
        return False


def get_bot_token_by_repo(repo_owner: str, repo_name: str) -> str:
    """按 owner/repo 查仓库 bot token（内部按带前缀 repo_key 命中）"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{repo_owner}/{repo_name}")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT bot_gitcode_token FROM managed_repos WHERE repo_key=? AND bot_gitcode_token!='' LIMIT 1", (repo_key,))
        row = c.fetchone()
        if row:
            return row[0]
    return ""


def get_repo_bot_token_no_global(repo_key: str) -> str:
    """仓库级 bot token（不含全局兜底）：managed_repos.bot_gitcode_token / devops_agent_session.repo_gitcode_token。

    用于服务端内部端点（A2A/tasks）鉴权——对齐 webhook token 思路但用 bot token，
    避免全局兜底导致未托管仓库也拿到 token。
    """
    from src.repo_key import split_owner_repo
    try:
        owner, rname = split_owner_repo(repo_key)
        _tok = get_bot_token_by_repo(owner, rname) or ""
        if _tok:
            return _tok
    except Exception:
        pass
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_gitcode_token FROM devops_agent_session WHERE repo_key=? AND repo_gitcode_token IS NOT NULL AND repo_gitcode_token!='' LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return ""


def _get_global_bot_token() -> str:
    """读取全局 bot token（configs/global-bot-token.json）"""
    import json, os
    try:
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs", "global-bot-token.json")
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                return json.load(f).get("gitcode_api_token", "") or ""
    except Exception as e:
        log.warning(f"Failed to load global bot token: {e}")
    return ""


def get_gitcode_token(repo_key: str = "", user_id: int = 0) -> str:
    """统一获取 GitCode API Token。

    - repo_key 非空 → bot token：devops_agent_session.repo_gitcode_token
      → managed_repos.bot_gitcode_token（get_bot_token_by_repo）→ 全局 bot token
    - user_id > 0 → user token（user_accounts.gitcode_api_token）
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if repo_key:
        # 1. devops_agent_session.repo_gitcode_token（agent 专属配置）
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT repo_gitcode_token FROM devops_agent_session WHERE repo_key=? AND repo_gitcode_token IS NOT NULL AND repo_gitcode_token!='' LIMIT 1", (repo_key,))
                row = c.fetchone()
                if row:
                    return row[0]
        except Exception as e:
            log.warning(f"get_gitcode_token: query devops_agent_session failed: {e}")
        # 2. managed_repos.bot_gitcode_token（仓库级配置）
        if "/" in repo_key:
            from src.repo_key import split_owner_repo
            owner, repo = split_owner_repo(repo_key)
            tok = get_bot_token_by_repo(owner, repo)
            if tok:
                return tok
        # 3. 全局 bot token
        return _get_global_bot_token()
    if user_id > 0:
        return get_user_gitcode_token(user_id)
    return ""


def get_agent_bot_token(repo_key: str) -> str:
    """获取 Agent 执行的 GitCode Bot Token（兼容包装，统一走 get_gitcode_token）"""
    return get_gitcode_token(repo_key=repo_key)


def get_user_gitcode_token(user_id: int) -> str:
    """获取用户的 GitCode API token（user_accounts.gitcode_api_token）"""
    if not user_id or user_id <= 0:
        return ""
    try:
        creds = get_credentials(user_id)
        for c in creds:
            if c.get("provider") == "gitcode":
                return c.get("config", {}).get("token", "") or ""
    except Exception as e:
        log.warning(f"get_user_gitcode_token failed for user={user_id}: {e}")
    return ""


def get_webhook_token_by_repo(full_name: str) -> str:
    """根据 full_name（owner/repo）查找 repo 的预期 webhook token"""
    from src.repo_key import to_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT gitcode_webhook_token FROM managed_repos WHERE repo_key=? LIMIT 1", (to_repo_key(full_name),))
        row = c.fetchone()
        return row[0] if row else ""


def remove_managed_repo(repo_key: str) -> dict | None:
    """删除仓库级托管行，返回被删除的记录（含 webhook_id）或 None；同时复位所有用户 is_join。"""
    from src.repo_key import from_repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT full_name, webhook_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if not row:
                return None
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("UPDATE user_repos SET is_join=0 WHERE repo_key=?", (repo_key,))
            db.commit()
            return {"full_name": from_repo_key(repo_key), "webhook_id": row[1]}
    except Exception as e:
        log.warning(f"Remove managed repo failed: {e}")
        return None


def get_all_managed_repos() -> list[dict]:
    """Admin 用：获取所有托管记录（repo_key 带前缀，full_name 无前缀）"""
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, created_at FROM managed_repos ORDER BY full_name")
        return [{"repo_key": r[0], "repo_owner": r[1], "repo_name": r[2],
                 "full_name": from_repo_key(r[0]), "webhook_id": r[4], "created_at": r[5]} for r in c.fetchall()]


# ====== 用户账户 CRUD ======

USER_ACCOUNT_FIELDS = [
    "id", "username", "password_hash", "gitcode_id",
    "gitcode_api_token",
    "nickname",
    "feishu_app_id", "feishu_app_secret",
    "huawei_cloud_ak", "huawei_cloud_sk",
    "CODEARTS_CLI_AK", "CODEARTS_CLI_SK",
    "gitcode_publish_repo", "github_publish_repo",
    "password",
    "created_by", "created_at",
    "hw_iam_domain", "hw_iam_user", "hw_iam_password",
    "hw_iam_account_type",
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
    fields = {k: data.get(k, "") for k in USER_ACCOUNT_FIELDS if k not in ("id", "username", "created_at")}
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


def get_all_bot_usernames() -> list[str]:
    """获取所有托管仓库配置的 bot 用户名列表（用于 self-comment 检测）。
    包含 managed_repos.bot_username + 全局 bot（configs/global-bot-token.json）。"""
    result = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT DISTINCT bot_username FROM managed_repos WHERE bot_username IS NOT NULL AND bot_username!=''")
            for row in c.fetchall():
                result.append(row[0])
        except Exception:
            pass
    # 全局 bot 用户名（self-comment 检测必须包含，否则 bot 自己评论会死循环）
    try:
        import json, os
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs", "global-bot-token.json")
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                _gbt = json.load(f)
            _us = _gbt.get("bot_usernames") or []
            result.extend(_us)
    except Exception:
        pass
    return list(set(r for r in result if r))


def get_meta_value(key: str) -> str:
    """从 meta 表读取配置值"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = c.fetchone()
            return row[0] if row else ""
        except Exception:
            return ""


def set_meta_value(key: str, value: str):
    """写入配置值到 meta 表"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
            db.commit()
        except Exception as e:
            log.warning(f"set_meta_value failed: {key}: {e}")


def get_server_base_url() -> str:
    """返回服务端 API 基址（A2A/tasks 等协议端点用，不含 /webhook/gitcode 后缀）。

    优先取 configs/server-config.json 显式配置；无则 meta.server_base_url；
    再兜底从 webhook_url 剥掉 /webhook/gitcode 后缀；最后 http://localhost:8080。
    """
    try:
        from src.config import read_server_config
        _sb = (read_server_config().get("server_base_url") or "").strip().rstrip("/")
        if _sb:
            return _sb
    except Exception:
        pass
    try:
        base = get_meta_value("server_base_url") or ""
        if base:
            return base.rstrip("/")
        wu = get_webhook_url() or ""
        if wu:
            base = wu.rstrip("/")
            for suffix in ("/webhook/gitcode", "/webhook"):
                if base.endswith(suffix):
                    return base[: -len(suffix)]
            return base
    except Exception:
        pass
    return "http://localhost:8080"


def get_webhook_url() -> str:
    """返回服务端 webhook 地址（GitCode 事件回调注册用，含 /webhook/gitcode 后缀）。

    优先取 configs/server-config.json 显式配置；无则 meta.webhook_url；再兜底空串
    （不设 localhost 兜底，让 repos.py「webhook_url 未配置即拒」守卫生效）。
    """
    try:
        from src.config import read_server_config
        _wh = (read_server_config().get("webhook_url") or "").strip()
        if _wh:
            return _wh
    except Exception:
        pass
    try:
        return (get_meta_value("webhook_url") or "").strip()
    except Exception:
        return ""


def migrate_accounts_from_json():
    """首次运行加载配置：从 global-bot-token.json 将 GitCode OAuth 配置写入 meta 表（仅首次/幂等）。

    accounts.json 已废弃删除；OAuth 凭据收敛到 configs/global-bot-token.json。
    user_accounts 账户通过 OAuth 登录动态创建，无需预置。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs", "global-bot-token.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
        _oauth_keys = [
            ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
            ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
            ("gitcode_oauth_redirect_uri", "oauth_gitcode_redirect_uri"),
        ]
        loaded = []
        with db_lock:
            for src_key, meta_key in _oauth_keys:
                val = data.get(src_key, "") or ""
                if not val:
                    continue
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                if c.fetchone()[0] == 0:
                    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (meta_key, val))
                    loaded.append(meta_key)
            db.commit()
        if loaded:
            log.info(f"Loaded OAuth config from global-bot-token.json into meta: {loaded}")
    except Exception as e:
        log.warning(f"Migration from global-bot-token.json failed: {e}")


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


# ====== 用户仓库缓存表（user_repos）======
# DDL 定义已移至文件前部（_migrate_repo_key_tables 之前），此处仅保留 init 函数

def init_user_repos_table():
    with db_lock:
        db.execute(_USER_REPOS_DDL)
        db.commit()


def save_user_repos(user_id: int, repos: list[dict]):
    """批量写入用户仓库缓存（repos 元素含 full_name/owner/name/...，full_name 无前缀）

    is_join 从用户自己的主动托管记录（mark_user_repo_join 写入的 is_join=1 AND is_admin=1）继承，
    不再查询全局 managed_repos，防止 A 托管的仓库污染 B 的下拉框。
    """
    from src.repo_key import to_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key FROM user_repos WHERE user_id=? AND is_join=1 AND is_admin=1", (user_id,))
        joined_keys = {row[0] for row in c.fetchall()}
        c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
        for r in repos:
            full_name = r.get("full_name", "") or r.get("repo_key", "")
            repo_key = to_repo_key(full_name)
            is_join = 1 if repo_key in joined_keys else 0
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, description, role, is_admin, is_join, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, repo_key, r.get("owner", ""), r.get("name", ""),
                 r.get("description", ""), r.get("role", ""), 1 if r.get("is_admin") else 0, is_join,
                 r.get("created_at", "") or "")
            )
        db.commit()


def get_user_repos(user_id: int) -> list[dict]:
    """读取用户仓库缓存（对外返回 full_name 无前缀）"""
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, owner, name, description, role, is_admin, is_join, created_at FROM user_repos WHERE user_id=? ORDER BY repo_key", (user_id,))
        return [
            {"repo_key": r[0], "full_name": from_repo_key(r[0]), "owner": r[1], "name": r[2],
             "description": r[3], "role": r[4], "is_admin": bool(r[5]), "is_join": bool(r[6]),

             "created_at": r[7] or ""}
            for r in c.fetchall()
        ]


def mark_user_repo_join(user_id: int, repo_key: str, is_join: bool = True, is_admin: bool = False):
    """标记用户已加入某仓库（user_repos.is_join），无对应缓存行则插入最小记录。"""
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_repos SET is_join=?, is_admin=? WHERE user_id=? AND repo_key=?",
                  (int(is_join), int(is_admin), user_id, repo_key))
        if c.rowcount == 0:
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                (user_id, repo_key, owner, repo, int(is_admin), int(is_join)))
        db.commit()


def get_user_repos_synced_at(user_id: int) -> str:
    """返回用户仓库缓存的最近同步时间（synced_at），无缓存返回空串"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT MAX(synced_at) FROM user_repos WHERE user_id=?", (user_id,))
        row = c.fetchone()
        return row[0] or "" if row else ""


def clear_user_repos(user_id: int):
    """清除用户仓库缓存"""
    with db_lock:
        db.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
        db.commit()


def has_user_repos(user_id: int) -> bool:
    """检查是否有缓存"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM user_repos WHERE user_id=?", (user_id,))
        return c.fetchone()[0] > 0


init_user_repos_table()


# ====== Agent Runtimes 缓存表 ======

def save_backend_agents(agents: list[dict], scope: str = "global", owner_id: str = "", host_name: str = ""):
    """[兼容别名] 见 save_agent_runtimes"""
    return save_agent_runtimes(agents, scope, owner_id, host_name)


def save_agent_runtimes(agents: list[dict], scope: str = "global", owner_id: str = "", host_name: str = ""):
    """批量写入 agent runtimes 缓存（scope: global/repo/user）

    幂等 upsert：按 name+scope+owner_id+host_name 匹配，存在则更新（保留 id），不存在才插入。
    保证跨刷新 id 稳定，供各表 runtime_id 引用。
    host_name：本机直连引擎（credential_id=0）归属的主控机 hostname，区分多台主控机。
    """
    with db_lock:
        c = db.cursor()
        # P0 修复：MySQL 下缩短 innodb_lock_wait_timeout（50s→5s），
        # 防止 admin 连接池泄漏占锁时 DELETE 被阻塞导致进程崩溃 (Lock wait timeout 1205)。
        # SQLite 无此概念，执行会报错直接忽略。
        try:
            c.execute("SET SESSION innodb_lock_wait_timeout=5")
        except Exception:
            pass
        try:
            for a in agents:
                name = (a.get("name", "") or "").strip().lower()
                if not name:
                    continue
                # 本机直连 engine 的 local_skills + skill 详情
                _ls_json = json.dumps(a.get("local_skills", []), ensure_ascii=False)
                _detail = a.get("local_skills_detail")
                _detail_json = json.dumps(_detail, ensure_ascii=False) if _detail is not None else ""
                # 仅操作本机未绑定凭据的行（credential_id=0）；凭据绑定行由边端 register 独占管理
                c.execute("SELECT id FROM agent_runtimes WHERE name=? AND scope=? AND owner_id=? AND credential_id=0 AND host_name=?", (name, scope, owner_id, host_name))
                row = c.fetchone()
                if row:
                    c.execute(
                        "UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, healthy=?, pip_package=?, status='online', last_seen_at=datetime('now'), updated_at=datetime('now'), local_skills=?, skill_scan_result=? WHERE id=?",
                        (a.get("display_name", name),
                         1 if a.get("installed") else 0, a.get("cli_path", ""),
                         a.get("version", ""), 1 if a.get("healthy") else 0,
                         a.get("pip_package", ""), _ls_json, _detail_json, row[0])
                    )
                else:
                    # 方案1（engine-selection-design.md §4.3）：本机直连（credential_id=0）是独立受管资源。
                    # refresh 仅持久化"真实安装"的本机 CLI（settings.py 已过滤 installed），即使同名已有
                    # 凭据绑定行（远端 worker），也创建本机直连行；未安装则跳过（防 -cred0 幽灵占位，
                    # 见 runtime-admin-two-view-design §6.2）。
                    if not a.get("installed"):
                        continue
                    c.execute(
                        "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, local_skills, skill_scan_result, host_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, a.get("display_name", name),
                         1, a.get("cli_path", ""),
                         a.get("version", ""), 1 if a.get("healthy") else 0,
                         a.get("pip_package", ""), scope, owner_id, _ls_json, _detail_json, host_name)
                    )
                    # 不再自动写入默认 "all" 标签——新引擎默认无标签，由用户在控制台手动配置。
            # 清理本次 scope 中已不在列表里的 runtime（忽略大小写）。
            # 仅清理未绑定凭据的本机行（credential_id=0）；凭据绑定的远端/边端注册行一律保留
            #（见 multi-runtime-instance-design §5.1 防误删）。
            current_names = [(a.get("name", "") or "").strip().lower() for a in agents if a.get("name")]
            if current_names:
                placeholders = ",".join("?" for _ in current_names)
                c.execute(f"DELETE FROM agent_runtimes WHERE scope=? AND owner_id=? AND credential_id=0 AND host_name=? AND lower(name) NOT IN ({placeholders})", (scope, owner_id, host_name, *current_names))
            db.commit()
        except Exception as e:
            log.warning(f"save_agent_runtimes failed (lock timeout or error): {e}")
            try:
                db.rollback()
            except Exception:
                pass


def get_backend_agents(scope: str = "global", owner_id: str = "") -> list[dict]:
    """[兼容别名] 见 get_agent_runtimes"""
    return get_agent_runtimes(scope, owner_id)


def get_agent_runtimes(scope: str | None = "global", owner_id: str = "") -> list[dict]:
    """读取 agent runtimes 缓存（含 status/last_seen_at，供健康态派生）。

    scope=None 时返回全量（含 user/repo 个人行，供视图层自行按可见性过滤，
    如 settings.get_agent_runtimes 的 Engines 列表）。
    """
    with db_lock:
        c = db.cursor()
        if scope is None:
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name FROM agent_runtimes ORDER BY name")
        else:
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name FROM agent_runtimes WHERE scope=? AND (owner_id=? OR owner_id='') ORDER BY name", (scope, owner_id))
        return [
            {"id": r[0], "name": r[1], "display_name": r[2], "installed": bool(r[3]),
             "cli_path": r[4], "version": r[5], "healthy": bool(r[6]),
             "pip_package": r[7], "updated_at": r[8], "scope": r[9], "owner_id": r[10],
             "credential_id": r[11] or 0, "status": r[12] or "", "last_seen_at": r[13],
             "local_skills": json.loads(r[14] or "[]") if r[14] else [],
             "skill_scan_result": json.loads(r[15] or "[]") if r[15] else [],
             "agent_tags": json.loads(r[16]) if r[16] else [],
             "host_name": r[17] or ""}
            for r in c.fetchall()
        ]


def get_backend_id_by_name(name: str, scope: str = "global", owner_id: str = "") -> int | None:
    """[兼容别名] 见 get_runtime_id_by_name"""
    return get_runtime_id_by_name(name, scope, owner_id)


def get_local_direct_runtime_id(name: str) -> int | None:
    """按引擎名查本机直连（credential_id=0 且已装）runtime id。

    Chat backend 名解析专用：裸引擎名（如 'opencode'）可能同时存在本机直连与共享 worker，
    必须显式优先本机直连（chat-agent-backend-resolution-fix.md §方案A）。
    """
    if not name:
        return None
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM agent_runtimes WHERE lower(name)=? AND credential_id=0 AND installed=1 ORDER BY id LIMIT 1",
            (name.strip().lower(),))
        row = c.fetchone()
        return row[0] if row else None


def get_runtime_id_by_name(name: str, scope: str = "global", owner_id: str = "") -> int | None:
    """按 name+scope+owner_id 查找 runtime id（多实例：优先 online + 最近心跳，见 multi-runtime-instance-design §4.3 S2）。

    凭据已吊销（revoked）的行排除（2026-08-17 修复）：revoke 后旧 agent_runtimes 行
    保留但不可用，不参与解析，避免配置落到已注销的引擎上。
    """
    if not name:
        return None
    name = name.strip().lower()
    with db_lock:
        c = db.cursor()
        c.execute("""
            SELECT ar.id FROM agent_runtimes ar
            LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
            WHERE lower(ar.name)=? AND ar.scope=? AND ar.owner_id=?
              AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active')
            ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC
            LIMIT 1
        """, (name, scope, owner_id))
        row = c.fetchone()
        return row[0] if row else None


def ensure_default_backend(name: str = "opencode", scope: str = "global"):
    """[兼容别名] 见 ensure_default_runtime"""
    return ensure_default_runtime(name, scope)


def is_known_engine_name(name: str) -> bool:
    """是否为已知执行引擎类型名（discover_backends 白名单或已注册的自定义引擎）。

    用于 ensure_default_runtime / get_chat_config 等路径防幽灵：拒绝任意字符串
    （如历史遗留的 'default'）生成 `{name}-cred0` 的 credential_id=0 占位行
    （见 runtime-admin-two-view-design §6.2 / 防 -cred0 孤儿）。
    """
    name = (name or "").strip().lower()
    if not name:
        return False
    # 静态白名单（避免 lazy import 循环引用导致 always False）
    _KNOWN = {"opencode", "codearts", "hermes", "codex", "cursor"}
    if name in _KNOWN:
        return True
    try:
        from src.agent.executor import discover_backends
        if any((b.get("name") or "").strip().lower() == name for b in discover_backends()):
            return True
    except Exception:
        pass
    try:
        from src.agent.executor import _BACKENDS
        if name in _BACKENDS:
            return True
    except Exception:
        pass
    return False


def _local_engine_info(name: str) -> tuple[bool, str]:
    """按引擎类型名返回本机是否已安装 + cli_path（discover_backends 真实探测，见 engine-selection §7.3）。"""
    try:
        from src.agent.executor import discover_backends
        for b in discover_backends():
            if (b.get("name") or "").strip().lower() == (name or "").strip().lower():
                return bool(b.get("installed")), (b.get("cli_path") or "")
    except Exception:
        pass
    return False, ""


def ensure_default_runtime(name: str = "opencode", scope: str = "global"):
    """确保默认 runtime 存在（幂等），返回 id（多实例：优先 online，见 multi-runtime-instance-design §4.3 S2）。

    防幽灵：仅当 name 为已知引擎类型（is_known_engine_name）或已存在同名 runtime 时才创建/复用；
    否则返回 None，不生成 credential_id=0 占位行。
    建行时 installed/healthy 按真实发现结果写入（不再硬编码 installed=1/healthy=1，
    见 engine-selection §7.3），使未安装引擎不会被 `only_installed/only_healthy` 过滤误选。
    """
    name = (name or "opencode").strip().lower() or "opencode"
    with db_lock:
        c = db.cursor()
        c.execute("""
            SELECT ar.id FROM agent_runtimes ar
            LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
            WHERE lower(ar.name)=? AND ar.scope=? AND ar.owner_id=''
              AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active')
            ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC
            LIMIT 1
        """, (name, scope))
        row = c.fetchone()
        if row:
            return row[0]
        if not is_known_engine_name(name):
            log.warning(f"ensure_default_runtime: 拒绝为未知引擎 '{name}' 创建占位行")
            return None
        installed, cli_path = _local_engine_info(name)
        healthy, version = 0, ""
        if installed and cli_path:
            try:
                from src.runtime.local_probe import probe_local_engines
                probed = probe_local_engines([{"name": name, "cli_path": cli_path, "installed": True}])
                if probed:
                    healthy = 1 if probed[0].get("healthy") else 0
                    version = probed[0].get("version", "") or ""
            except Exception:
                pass
        c.execute("INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,'')",
                  (name, name.capitalize(), 1 if installed else 0, cli_path, version, healthy, name, scope))
        db.commit()
        return c.lastrowid


def get_backend_agents_age() -> float | None:
    """[兼容别名] 见 get_agent_runtimes_age"""
    return get_agent_runtimes_age()


def get_agent_runtimes_age() -> float | None:
    """返回缓存数据的 age（秒），无缓存返回 None"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT MIN(updated_at) FROM agent_runtimes")
        row = c.fetchone()
        if not row or not row[0]:
            return None
    import datetime
    updated = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return (datetime.datetime.now() - updated).total_seconds()


# ====== Scene Market CRUD（场景市场）======

def list_scene_categories() -> list[dict]:
    """返回启用的场景分类列表，按 order_num 排序"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, slug, icon, order_num FROM scene_category "
            "WHERE is_active=1 ORDER BY order_num"
        )
        return [{"id": r[0], "name": r[1], "slug": r[2] or "", "icon": r[3] or "", "order_num": r[4]}
                for r in c.fetchall()]


def get_scene_category(category_id: int) -> dict | None:
    """按 id 查场景分类"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, icon FROM scene_category WHERE id=?", (category_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "icon": row[2] or ""}


def list_scene_market(q: str = "", category_id: int = 0, sort: str = "latest",
                      featured_only: int = 0, limit: int = 0,
                      page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """查询场景市场列表（is_active=1），支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页。

    sort: latest（按 created_at 降序）/ hot（按 like_count 降序）。
    featured_only: 1=只查精选场景，0=全部。
    limit: >0 限制返回条数（精选场景用，不分页）。
    page/page_size: 分页参数（limit=0 时生效），page 从 1 开始。
    返回 (rows, total)。
    """
    where = ["is_active=1"]
    params: list = []
    if q:
        where.append("(name LIKE ? OR market_description LIKE ? OR display_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if category_id and category_id > 0:
        where.append("category_id=?")
        params.append(category_id)
    if featured_only:
        where.append("is_featured=1")
    where_sql = " AND ".join(where)
    order_by = "created_at DESC" if sort != "hot" else "like_count DESC"

    with db_lock:
        c = db.cursor()
        # 总数
        c.execute(f"SELECT COUNT(*) FROM scene_market WHERE {where_sql}", params)
        total = c.fetchone()[0]

        # 分页
        if limit and limit > 0:
            # 精选场景模式：不分页，直接 limit
            limit_sql = f" LIMIT {int(limit)}"
        else:
            offset = (page - 1) * page_size
            limit_sql = f" LIMIT {int(page_size)} OFFSET {int(offset)}"
        c.execute(
            f"SELECT id, name, display_name, version, description, market_description, "
            f"icon, obs_icon_key, category_id, is_featured, like_count, source, recommended_template, "
            f"nodes_json, onboarding, created_at "
            f"FROM scene_market WHERE {where_sql} ORDER BY {order_by}{limit_sql}",
            params,
        )
        rows = c.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "display_name": r[2] or r[1],
            "version": r[3] or "1.0.0", "description": r[4] or "",
            "market_description": r[5] or "", "icon": r[6] or "",
            "obs_icon_key": r[7] or "",
            "category_id": r[8], "is_featured": r[9], "like_count": r[10],
            "source": r[11] or "official", "recommended_template": r[12] or "",
            "nodes_json": r[13] or "[]", "onboarding": r[14] or "repo-onboarding",
            "created_at": str(r[15] or ""),
        })
    return result, total


def get_scene_market_by_id(market_id: int) -> dict | None:
    """按主键 id 查场景市场详情"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, display_name, version, description, market_description, "
            "icon, obs_icon_key, category_id, is_featured, like_count, source, source_user_id, "
            "recommended_template, nodes_json, onboarding, is_active, created_at, updated_at "
            "FROM scene_market WHERE id=?",
            (market_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "display_name": row[2] or row[1],
        "version": row[3] or "1.0.0", "description": row[4] or "",
        "market_description": row[5] or "", "icon": row[6] or "",
        "obs_icon_key": row[7] or "",
        "category_id": row[8], "is_featured": row[9], "like_count": row[10],
        "source": row[11] or "official", "source_user_id": row[12],
        "recommended_template": row[13] or "", "nodes_json": row[14] or "[]",
        "onboarding": row[15] or "repo-onboarding", "is_active": row[16],
        "created_at": str(row[17] or ""), "updated_at": str(row[18] or ""),
    }


# ====== Scene Likes CRUD（场景点赞）======

def is_scene_liked(user_id: int, scene_id: int) -> bool:
    """检查用户是否已点赞某场景"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
        return c.fetchone() is not None


def toggle_scene_like(user_id: int, scene_id: int) -> tuple[bool, int]:
    """场景点赞 toggle：已赞→取消，未赞→点赞。
    返回 (liked, like_count)。在 db_lock 事务内原子执行。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
        if c.fetchone():
            c.execute("DELETE FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
            c.execute("UPDATE scene_market SET like_count=like_count-1 WHERE id=?", (scene_id,))
            db.commit()
            c.execute("SELECT like_count FROM scene_market WHERE id=?", (scene_id,))
            row = c.fetchone()
            return (False, row[0] if row else 0)
        else:
            c.execute("INSERT INTO scene_likes (user_id, scene_id) VALUES (?, ?)", (user_id, scene_id))
            c.execute("UPDATE scene_market SET like_count=like_count+1 WHERE id=?", (scene_id,))
            db.commit()
            c.execute("SELECT like_count FROM scene_market WHERE id=?", (scene_id,))
            row = c.fetchone()
            return (True, row[0] if row else 0)


def batch_scene_liked(user_id: int, scene_ids: list[int]) -> dict[int, bool]:
    """批量查询用户对多个场景的点赞状态，返回 {scene_id: bool}"""
    if not scene_ids or not user_id:
        return {}
    placeholders = ",".join(["?"] * len(scene_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            f"SELECT scene_id FROM scene_likes WHERE user_id=? AND scene_id IN ({placeholders})",
            [user_id] + scene_ids,
        )
        liked_ids = {r[0] for r in c.fetchall()}
    return {sid: (sid in liked_ids) for sid in scene_ids}


# ====== Skill Likes CRUD（技能点赞）======

def is_skill_liked(user_id: int, skill_id: int) -> bool:
    """检查用户是否已点赞某技能"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        return c.fetchone() is not None


def toggle_skill_like(user_id: int, skill_id: int) -> tuple[bool, int]:
    """技能点赞 toggle：已赞→取消，未赞→点赞。
    返回 (liked, like_count)。在 db_lock 事务内原子执行。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        if c.fetchone():
            c.execute("DELETE FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
            c.execute("UPDATE skill SET like_count=like_count-1 WHERE id=?", (skill_id,))
            db.commit()
            c.execute("SELECT like_count FROM skill WHERE id=?", (skill_id,))
            row = c.fetchone()
            return (False, row[0] if row else 0)
        else:
            c.execute("INSERT INTO skill_likes (user_id, skill_id) VALUES (?, ?)", (user_id, skill_id))
            c.execute("UPDATE skill SET like_count=like_count+1 WHERE id=?", (skill_id,))
            db.commit()
            c.execute("SELECT like_count FROM skill WHERE id=?", (skill_id,))
            row = c.fetchone()
            return (True, row[0] if row else 0)


def batch_skill_liked(user_id: int, skill_ids: list[int]) -> dict[int, bool]:
    """批量查询用户对多个技能的点赞状态，返回 {skill_id: bool}"""
    if not skill_ids or not user_id:
        return {}
    placeholders = ",".join(["?"] * len(skill_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            f"SELECT skill_id FROM skill_likes WHERE user_id=? AND skill_id IN ({placeholders})",
            [user_id] + skill_ids,
        )
        liked_ids = {r[0] for r in c.fetchall()}
    return {sid: (sid in liked_ids) for sid in skill_ids}


# ====== 技能统计查询（通过 scene_skill_ref 关联表）======

def get_skill_usage_stats(skill_ids: list[int] | None = None) -> dict[int, dict]:
    """返回技能的使用统计 {skill_id: {scene_count, repo_count}}。

    skill_ids 不传时查全表（兼容）；传时只查指定技能（分页场景只聚合本页）。

    scene_count: 被多少场景使用（scene_skill_ref 聚合）
    repo_count:  被多少仓库间接使用（scene_skill_ref JOIN devops_pipeline_scene）
    """
    with db_lock:
        c = db.cursor()
        if skill_ids:
            placeholders = ",".join(["?"] * len(skill_ids))
            # 一条 SQL 同时拿 scene_count + repo_count
            c.execute(
                f"SELECT sr.skill_id, "
                f"COUNT(DISTINCT sr.scene_id) AS scene_count, "
                f"COUNT(DISTINCT d.id) AS repo_count "
                f"FROM scene_skill_ref sr "
                f"LEFT JOIN devops_pipeline_scene d ON sr.scene_id = d.market_scene_id "
                f"WHERE sr.skill_id IN ({placeholders}) "
                f"GROUP BY sr.skill_id",
                skill_ids,
            )
            return {r[0]: {"scene_count": r[1], "repo_count": r[2]} for r in c.fetchall()}
        # 全表聚合（兼容旧调用）
        c.execute(
            "SELECT sr.skill_id, "
            "COUNT(DISTINCT sr.scene_id) AS scene_count, "
            "COUNT(DISTINCT d.id) AS repo_count "
            "FROM scene_skill_ref sr "
            "LEFT JOIN devops_pipeline_scene d ON sr.scene_id = d.market_scene_id "
            "GROUP BY sr.skill_id"
        )
        return {r[0]: {"scene_count": r[1], "repo_count": r[2]} for r in c.fetchall()}


def get_skill_scenes(skill_id: int) -> list[dict]:
    """返回某技能所属的场景列表 [{id, name, display_name}]"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT sm.id, sm.name, sm.display_name FROM scene_skill_ref sr "
            "JOIN scene_market sm ON sr.scene_id = sm.id "
            "WHERE sr.skill_id=? AND sm.is_active=1",
            (skill_id,),
        )
        return [{"id": r[0], "name": r[1], "display_name": r[2] or r[1]} for r in c.fetchall()]


def batch_skill_scenes(skill_ids: list[int]) -> dict[int, list[dict]]:
    """批量查询多个技能所属的场景列表，返回 {skill_id: [{id, name, display_name}]}"""
    if not skill_ids:
        return {}
    placeholders = ",".join(["?"] * len(skill_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            f"SELECT sr.skill_id, sm.id, sm.name, sm.display_name FROM scene_skill_ref sr "
            f"JOIN scene_market sm ON sr.scene_id = sm.id "
            f"WHERE sr.skill_id IN ({placeholders}) AND sm.is_active=1",
            skill_ids,
        )
        rows = c.fetchall()
    result: dict[int, list[dict]] = {sid: [] for sid in skill_ids}
    for r in rows:
        sid = r[0]
        if sid not in result:
            result[sid] = []
        result[sid].append({"id": r[1], "name": r[2], "display_name": r[3] or r[2]})
    return result


def get_scene_repo_count(market_scene_id: int) -> int:
    """统计某场景的在用仓库数（devops_pipeline_scene WHERE market_scene_id=?）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT COUNT(*) FROM devops_pipeline_scene WHERE market_scene_id=? AND is_active=1",
            (market_scene_id,),
        )
        row = c.fetchone()
    return row[0] if row else 0


def batch_scene_repo_count(market_scene_ids: list[int]) -> dict[int, int]:
    """批量统计多个场景的在用仓库数"""
    if not market_scene_ids:
        return {}
    with db_lock:
        c = db.cursor()
        placeholders = ",".join(["?"] * len(market_scene_ids))
        c.execute(
            f"SELECT market_scene_id, COUNT(*) FROM devops_pipeline_scene "
            f"WHERE market_scene_id IN ({placeholders}) AND is_active=1 "
            f"GROUP BY market_scene_id",
            market_scene_ids,
        )
        rows = {r[0]: r[1] for r in c.fetchall()}
    return {mid: rows.get(mid, 0) for mid in market_scene_ids}


def get_scene_repos(market_scene_id: int) -> list[dict]:
    """返回某场景的在用仓库列表 [{repo_key, full_name}]"""
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT repo_key FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND is_active=1",
            (market_scene_id,),
        )
        rows = c.fetchall()
    return [{"repo_key": r[0], "full_name": from_repo_key(r[0])} for r in rows if r[0]]


def get_featured_skills(limit: int = 5) -> list[dict]:
    """精选技能列表：is_featured=1 的 official 技能，按 like_count 降序"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, skill_name, display_name, description, agent_type, "
            "is_featured, like_count "
            "FROM skill WHERE is_active=1 AND is_featured=1 AND scope='official' "
            "ORDER BY like_count DESC LIMIT ?",
            (limit,)
        )
        return [{"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
                 "description": r[3] or "", "agent_type": r[4] or "",
                 "is_featured": r[5], "like_count": r[6]}
                for r in c.fetchall()]

