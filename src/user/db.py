"""用户系统数据库：users/credentials/vms/skills 表管理

D32 迁移完成后 user_accounts 表为死表，不再被业务代码读取。
以下函数标记为 @deprecated——新代码不应调用，应使用 grape_account_db.py 中的对应函数：
  get_user/get_user_by_username → get_grape_account_by_id/get_grape_account_by_username
  create_user → create_grape_account
  verify_user → verify_grape_account
  update_password → grape_account_db.update_password
  get_token_version → grape_account_db.get_token_version
  increment_token_version → grape_account_db.increment_token_version
  get_user_account → get_grape_account_by_username
  link_gitcode_user/link_github_user → create_external_identity
  get_user_by_gitcode_id/get_user_by_github_id → get_external_identity
"""
import json, hashlib, time, os, warnings
from src.session_manager import db, db_lock
from src.config import log

# OAuth state 存储（内存，不依赖 SQLite 跨线程事务）
# OAuth state 已改为 DB 共享存储（oauth_states 表），不再用进程内存


def _deprecated_user_accounts_fn(replacement: str = ""):
    """装饰器：标记 user_accounts 老表函数为 deprecated。
    调用时发出 DeprecationWarning，日志记录。
    """
    import functools
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            msg = f"user_accounts.{fn.__name__}() is deprecated"
            if replacement:
                msg += f", use {replacement} instead"
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
            log.warning(f"Deprecated function called: {fn.__name__}() — {msg}")
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ====== 用户账户表（合并 users + bot_accounts） ======
_USER_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    gitcode_api_token TEXT DEFAULT '',
    github_id TEXT DEFAULT '',
    github_api_token TEXT DEFAULT '',
    github_login TEXT DEFAULT '',
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


@_deprecated_user_accounts_fn("create_grape_account")
def create_user(username: str, password: str, created_by: int = 0) -> dict | None:
    """创建普通用户，返回用户信息或 None。

    新用户自动创建个人组织（is_personal=1），实现组织特性向后兼容。
    """
    pw_hash = _hash_password(password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO user_accounts (username, password_hash, password, created_by, created_at) VALUES (?,?,?,?,datetime('now'))",
                      (username, pw_hash, password, created_by))
            db.commit()
            uid = c.lastrowid
            log.info(f"User created: {username} (id={uid}, by={created_by})")

            # 自动创建个人组织
            from src.user.org_db import create_org
            slug = f"personal-{uid}"
            create_org(
                name=f"{username}的个人空间", slug=slug,
                owner_id=uid, is_personal=1
            )
            log.info(f"Personal org created for user {uid}")

            return {"id": uid, "username": username}
    except Exception as e:
        log.warning(f"Create user failed: {e}")
        return None


@_deprecated_user_accounts_fn("get_grape_account_by_id")
def get_user(user_id: int) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


@_deprecated_user_accounts_fn("get_grape_account_by_username")
def get_user_by_username(username: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, password_hash, created_by, created_at FROM user_accounts WHERE username=?", (username,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "created_by": row[3], "created_at": row[4]}


@_deprecated_user_accounts_fn("admin/db.py list_grape_accounts")
def list_users() -> list[dict]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts ORDER BY id")
        return [{"id": r[0], "username": r[1], "created_by": r[2], "created_at": r[3]} for r in c.fetchall()]


@_deprecated_user_accounts_fn("grape_account_db.soft_delete_account")
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


@_deprecated_user_accounts_fn("grape_account_db.update_password")
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


@_deprecated_user_accounts_fn("grape_account_db.verify_grape_account")
def verify_user(username: str, password: str) -> dict | None:
    """验证用户名密码，成功返回用户信息（含 token_version），失败返回 None。

    被管理员禁用的用户（is_disabled=1）返回 None（Issue #281：admin 禁用后旧体系登录口立即失效）。
    """
    user = get_user_by_username(username)
    if not user:
        return None
    if user["password_hash"] != _hash_password(password):
        return None
    token_version = get_token_version(user["id"])
    with db_lock:
        c = db.cursor()
        c.execute("SELECT is_disabled FROM user_accounts WHERE id=?", (user["id"],))
        row = c.fetchone()
    if row and row[0]:
        return None
    return {"id": user["id"], "username": user["username"], "created_by": user["created_by"], "created_at": user["created_at"], "token_version": token_version}


# ====== JWT token_version 管理 ======

@_deprecated_user_accounts_fn("grape_account_db.get_token_version")
def get_token_version(user_id: int) -> int:
    """获取用户的 token_version——JWT verify 时对比，不匹配则 token 失效。
    用户不存在时返回 -1（不等于任何有效 token 的 ver=0，使旧 token 失效）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    if not row:
        return -1  # 用户不存在 → 任何 token 都失效
    return (row[0] if row[0] is not None else 0)


@_deprecated_user_accounts_fn("grape_account_db.increment_token_version")
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
        # D32-补丁：更新 grape_accounts.last_active_at（不写 user_accounts 老表）
        for i in range(0, len(uids), 1000):
            batch = uids[i:i+1000]
            placeholders = ",".join(["?"] * len(batch))
            c.execute(
                f"UPDATE grape_accounts SET last_active_at=datetime('now') WHERE id IN ({placeholders})",
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
    """保存 OAuth state（防 CSRF，10 分钟过期，DB 共享存储——集群多实例可用）。

    P2-1: 懒清理——每次保存时顺带删除过期记录（概率性，避免全表扫描开销）。
    """
    import json as _json
    expires = time.time() + 600
    try:
        with db_lock:
            c = db.cursor()
            # P2-1: 懒清理——概率性删除过期 state（每次保存时 1/10 概率触发）
            import random as _rnd
            if _rnd.random() < 0.1:
                c.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            # P2 加固：用 INSERT 而非 INSERT OR REPLACE——state 是 SHA-256 随机值，
            # 碰撞概率可忽略。如果碰巧碰撞（INSERT 失败），旧 state 不会被覆盖，
            # 调用方会收到异常并重试。INSERT OR REPLACE 会静默覆盖未消费的旧 state。
            c.execute(
                "INSERT INTO oauth_states (state, provider, data_json, expires) VALUES (?,?,?,?)",
                (state, provider, _json.dumps(data or {}), expires),
            )
            db.commit()
        log.info(f"save_oauth_state: state={state[:20]}... saved OK")
    except Exception as e:
        log.warning(f"save_oauth_state failed: {e}")


# P0-3: exchange_code 一次性交换码——替代 URL 传递 access_token
def save_exchange_code(code: str, user_id: int, username: str,
                       provider: str, token_version: int, is_new: bool) -> bool:
    """保存一次性 exchange code（10 秒过期）。

    P2-1: 懒清理——每次保存时顺带删除过期 exchange_codes。
    """
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM exchange_codes WHERE expires < ?", (_t.time(),))
            c.execute(
                "INSERT OR REPLACE INTO exchange_codes "
                "(code, user_id, username, provider, token_version, is_new, created_at, expires) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (code, user_id, username, provider, token_version,
                 1 if is_new else 0, _t.time(), _t.time() + 10),
            )
            db.commit()
        return True
    except Exception as e:
        log.warning(f"save_exchange_code failed: {e}")
        return False


def validate_exchange_code(code: str) -> dict | None:
    """验证并原子消费 exchange code（SELECT + DELETE + rowcount 检查）。

    与 validate_oauth_state 一致：DELETE rowcount=0 说明已被其他请求消费，
    返回 None。保证一次性消费在 MySQL 多实例下也成立。
    """
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT user_id, username, provider, token_version, is_new, expires "
                "FROM exchange_codes WHERE code=?", (code,)
            )
            row = c.fetchone()
            if row:
                # 原子消费：DELETE 带 expires 条件，确保过期的 code 不能被消费
                c.execute(
                    "DELETE FROM exchange_codes WHERE code=? AND expires > ?",
                    (code, _t.time()),
                )
                if c.rowcount == 0:
                    # 已被消费或已过期
                    db.commit()
                    return None
            db.commit()
        if not row:
            return None
        if _t.time() > row[5]:
            return None
        return {
            "user_id": row[0], "username": row[1], "provider": row[2],
            "token_version": row[3], "is_new": bool(row[4]),
        }
    except Exception as e:
        log.warning(f"validate_exchange_code failed: {e}")
        return None


def cleanup_expired_exchange_codes() -> int:
    """清理过期的 exchange codes（防积累）。"""
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM exchange_codes WHERE expires < ?", (_t.time(),))
            db.commit()
            return c.rowcount
    except Exception:
        return 0


def validate_oauth_state(state: str, provider: str = "") -> dict | None:
    """验证并原子消费 OAuth state（SELECT + DELETE + rowcount 检查）。

    需求 2：原子消费，确保只有一个请求拿到 state 数据。
    - SQLite/单实例：db_lock 保证线程安全
    - MySQL 多实例：db_lock 跨实例无效，原子性靠 DELETE rowcount 保证
    - 不使用 FOR UPDATE，避免方言判断

    D26 修复：provider 参数改为可选——统一回调地址时不传 provider，
    从查询结果取 provider 返回给调用方。传了 provider 则做额外校验。

    返回的 dict 额外包含 "_provider" 字段（从 DB 记录恢复）。
    """
    import json as _json
    import time as _time
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT provider, data_json, expires FROM oauth_states WHERE state=?",
                (state,),
            )
            row = c.fetchone()
            if row:
                # 原子消费：DELETE 带 state + expires 条件
                # rowcount=0 说明已被其他请求消费或已过期
                c.execute(
                    "DELETE FROM oauth_states WHERE state=? AND expires > ?",
                    (state, _time.time()),
                )
                if c.rowcount == 0:
                    # 已被消费或已过期，不能使用 SELECT 到的数据
                    db.commit()
                    log.warning(f"validate_oauth_state: state={state[:20]}... already consumed or expired")
                    return None
            db.commit()
        if not row:
            log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
            return None
        db_provider = row[0]
        if provider and provider != db_provider:
            log.warning(f"validate_oauth_state: provider mismatch (expected={provider}, db={db_provider})")
            return None
        data = _json.loads(row[1] or "{}")
        data["_provider"] = db_provider
        return data
    except Exception as e:
        log.warning(f"validate_oauth_state failed: {e}")
        return None







@_deprecated_user_accounts_fn("grape_account_db.get_external_identity")
def get_user_by_gitcode_id(gitcode_id: str) -> dict | None:
    """通过 GitCode 用户 ID 查找本地用户"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts WHERE gitcode_id=?", (gitcode_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


@_deprecated_user_accounts_fn("grape_account_db.create_external_identity")
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
                          "refresh_token": refresh_token,
                          "fetched_at": int(time.time())
                      })))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Link GitCode user failed: {e}")
        return False


@_deprecated_user_accounts_fn("grape_account_db.get_external_identity")
def get_user_by_github_id(github_id: str) -> dict | None:
    """通过 GitHub 用户 ID 查找本地用户"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, username, created_by, created_at FROM user_accounts WHERE github_id=?", (github_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "created_by": row[2], "created_at": row[3]}


@_deprecated_user_accounts_fn("grape_account_db.create_external_identity")
def link_github_user(user_id: int, github_id: str, access_token: str, refresh_token: str = "") -> bool:
    """绑定本地用户与 GitHub 用户，存储 access_token 到 user_credentials"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_accounts SET github_id=? WHERE id=?", (github_id, user_id))
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) VALUES (?,?,?,datetime('now'))",
                      (user_id, "github_oauth", json.dumps({
                          "github_id": github_id,
                          "access_token": access_token,
                          "refresh_token": refresh_token,
                          "fetched_at": int(time.time())
                      })))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Link GitHub user failed: {e}")
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
    """获取用户凭据（D32-补丁：从 user_credentials 表 + external_identities 读，不查 user_accounts）"""
    result = []
    # 1. user_credentials 表（huawei_cloud/codearts/feishu 等非 OAuth 凭据）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider, config_json FROM user_credentials WHERE user_id=?", (user_id,))
        rows = c.fetchall()
    for provider, config_json in rows:
        try:
            cfg = json.loads(config_json) if config_json else {}
            result.append({"provider": provider, "config": cfg})
        except Exception:
            pass
    # 2. external_identities（gitcode/github OAuth token）
    # BUG-1 修复：get_user_identities 不含 access_token 列（防泄露到前端），
    # 改用 get_user_identities_with_tokens 读取解密后的 token。
    try:
        from src.auth.grape_account_db import get_user_identities_with_tokens
        for ident in get_user_identities_with_tokens(user_id):
            provider = ident.get("provider", "")
            token = ident.get("access_token", "")
            if provider and token:
                result.append({"provider": provider, "config": {"token": token}})
    except Exception as e:
        log.warning(f"get_credentials: read external_identities failed: {e}")
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
    host_user_id INTEGER DEFAULT 0,       -- Issue #372: 路径 B 托管用户 ID（无全局 bot token 时）
    webhook_secret TEXT DEFAULT '',       -- Issue #372: webhook 验证密钥（与 token 解耦）
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

        # 幂等补充：managed_repos 加 scene_recommend_config 列（智能推荐配置）
        try:
            _mr_cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "scene_recommend_config" not in _mr_cols:
                c.execute("ALTER TABLE managed_repos ADD COLUMN scene_recommend_config TEXT DEFAULT ''")
                db.commit()
                log.info("Migrate repo_key: added managed_repos.scene_recommend_config")
        except Exception as e:
            log.warning(f"Add managed_repos.scene_recommend_config failed: {e}")


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
        # 兼容旧列迁移（幂等）：github_id / github_api_token（GitHub OAuth 登录）
        for col in ["github_id", "github_api_token"]:
            try:
                db.execute(f"ALTER TABLE user_accounts ADD COLUMN {col} TEXT DEFAULT ''")
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
        # 兼容旧列迁移（幂等）：managed_repos 新增 github 列
        for col in ["bot_github_token", "github_webhook_token"]:
            try:
                db.execute(f"ALTER TABLE managed_repos ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
        # 兼容旧列迁移（幂等）：login_whitelist 新增 github_username 列
        try:
            db.execute("ALTER TABLE login_whitelist ADD COLUMN github_username TEXT DEFAULT ''")
        except Exception:
            pass
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
        # 2026-10-17 修复（账号体系重构遗留）：user_credentials 的 user_id 现在指向
        # grape_accounts.id（新体系），但下面的重映射/清理曾锚定旧表 user_accounts——
        # 新体系 id（如 1）不在旧表（id 区间 37+）里，被当"悬空行"每次启动时批量
        # 删除，导致用户在设置页保存的 AK/SK 反复丢失。三处全部改锚 grape_accounts；
        # users 中间表已不存在（重映射 UPDATE 自然 no-op，保留幂等兼容）。
        try:
            db.execute("""
                UPDATE user_credentials
                SET user_id = (SELECT ga.id FROM grape_accounts ga, user_accounts ua
                                WHERE ga.username = ua.username AND ua.id = user_credentials.user_id)
                WHERE EXISTS (SELECT 1 FROM user_accounts ua WHERE ua.id = user_credentials.user_id)
                  AND NOT EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = user_credentials.user_id)
                  AND EXISTS (SELECT 1 FROM grape_accounts ga, user_accounts ua
                              WHERE ga.username = ua.username AND ua.id = user_credentials.user_id)
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
            # P2-A (Issue #357): Batched DELETE for user_credentials to avoid deadlock.
            # 2026-10-17 修复：白名单从 user_accounts（旧表）改为 grape_accounts（新体系），
            # 否则新体系 id 的凭据行每次启动被误删（AK/SK 丢失事故根因）。
            from src.mysql_compat import get_db_backend as _get_be
            _is_mysql = _get_be() == "mysql"
            while True:
                if _is_mysql:
                    # MySQL: use derived table wrapper + LIMIT (translate_sql also handles this,
                    # but we do it explicitly here for clarity in the migration path)
                    db.execute("""
                        DELETE FROM user_credentials WHERE user_id IN (
                            SELECT user_id FROM (
                                SELECT uc.user_id FROM user_credentials uc
                                LEFT JOIN grape_accounts ga ON uc.user_id = ga.id
                                WHERE ga.id IS NULL
                                LIMIT 100
                            ) AS __to_delete
                        )
                    """)
                else:
                    # SQLite: no LIMIT in DELETE subquery, just do it in one shot
                    db.execute("""
                        DELETE FROM user_credentials WHERE user_id NOT IN (SELECT id FROM grape_accounts)
                    """)
                db.commit()
                # Check if more rows remain (MySQL batch loop)
                if _is_mysql:
                    c = db.cursor()
                    c.execute("""
                        SELECT COUNT(*) FROM user_credentials uc
                        LEFT JOIN grape_accounts ga ON uc.user_id = ga.id
                        WHERE ga.id IS NULL
                    """)
                    _remaining = c.fetchone()[0]
                    if _remaining == 0:
                        break
                else:
                    break
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
        # Issue #372: 用户 Token 降级支持
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN host_user_id INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN webhook_secret TEXT DEFAULT ''")
        except Exception:
            pass
        # R16.5.1: managed_repos.bot_token_status（过期标记，空=正常，expired=过期）
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN bot_token_status TEXT DEFAULT ''")
        except Exception:
            pass
        # R16.5.5: managed_repos.host_vcs_username（托管用户的 VCS 平台用户名，路径 B 自循环检测用）
        try:
            db.execute("ALTER TABLE managed_repos ADD COLUMN host_vcs_username TEXT DEFAULT ''")
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
                    # Issue #345: 按 repo_key 前缀分流——github: 仓不填 GitCode bot name
                    # user_accounts.bot_usernames 是 GitCode 的 bot 列表，只填 GitCode 仓库
                    c.execute(
                        "UPDATE managed_repos SET bot_username=? "
                        "WHERE full_name=? AND (bot_username='' OR bot_username IS NULL) "
                        "AND repo_key NOT LIKE 'github:%'",
                        (bot_name, uname))
                    c.execute(
                        "UPDATE managed_repos SET bot_username=? "
                        "WHERE (repo_owner=? OR full_name=?) AND (bot_username='' OR bot_username IS NULL) "
                        "AND repo_key NOT LIKE 'github:%'",
                        (bot_name, uname, uname))
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
    # P2-1: 启动时也清理过期的 exchange_codes
    try:
        cleanup_expired_exchange_codes()
    except Exception:
        pass


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


def resolve_skill_id(skill_name: str, owner_id: str = "",
                     repo_key: str = "", user_id: str | int = "") -> int:
    """按 skill_name 匹配 skill 表拿主键 id。

    查询顺序（Issue #367 个人优先）：user scope（按 user_id，owner_id 强匹配）
    → repo scope（按 repo_key）→ official。
    user_id 缺失时按 repo → official 降级（等同旧无用户上下文行为）。
    owner_id 参数兼容旧调用（非空时等价于 repo_key=owner_id）。
    """
    if not skill_name:
        return 0
    # 兼容旧调用：owner_id 非空时作为 repo_key 回退
    if owner_id and not repo_key:
        repo_key = owner_id
    if user_id:
        user_id = str(user_id)
    try:
        with db_lock:
            c = db.cursor()
            if user_id:
                c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='user' AND owner_id=? AND is_active=1 LIMIT 1", (skill_name, user_id))
                row = c.fetchone()
                if row:
                    log.info(f"resolve_skill_id: '{skill_name}' hit user scope (owner={user_id})")
                    return row[0]
            if repo_key:
                c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 LIMIT 1", (skill_name, repo_key))
                row = c.fetchone()
                if row:
                    log.info(f"resolve_skill_id: '{skill_name}' hit repo scope (owner={repo_key})")
                    return row[0]
            c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='official' AND is_active=1 LIMIT 1", (skill_name,))
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
                     description: str, object_key: str, created_by: str = "",
                     category_id: int = 0) -> bool:
    """OBS 模式技能记录 upsert（存在则更新固定 key，不存在则插入；v2.5 file_path + by 字段）。

    scope: user/repo；owner: user_id 或 repo_key。返回是否成功。
    Issue #179: 新增 category_id 参数。
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
                    "agent_type=?, description=?, category_id=?, updated_by=?, is_active=1, updated_at=datetime('now') WHERE id=?",
                    (object_key, skill_name, agent_type or "", description or "",
                     category_id, created_by or "", row[0]))
            else:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                    "file_path, scope, owner_id, created_by, updated_by, is_active, category_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (skill_name, skill_name, description or "", agent_type or "",
                     object_key, scope, owner, created_by or "", created_by or "", category_id))
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
        c.execute("SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id FROM skill WHERE scope='user' AND owner_id=? ORDER BY id", (str(user_id),))
        return [{"id": r[0], "skill_name": r[1], "agent_type": r[2] or "", "description": r[3] or "", "scope": r[4], "owner_id": r[5], "file_path": r[6] or "", "category_id": r[7] or 0, "created_at": ""} for r in c.fetchall()]


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
                     bot_username: str = "", dev_scene: str = "",
                     github_webhook_token: str = "", bot_github_token: str = "",
                     org_id: int = 0, host_user_id: int = 0, webhook_secret: str = "") -> bool:
    """写入仓库级托管配置（repo_key 带平台前缀，一仓库一行 upsert）。

    org_id > 0 时绑定到指定组织；org_id=0 时为全局（兼容旧逻辑）。
    host_user_id: Issue #372 路径 B 托管时写入当前用户 ID（无全局 bot token 场景）。
    webhook_secret: Issue #372 路径 B webhook 验证密钥（随机生成，与 token 解耦）。
    """
    from src.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo_key)
    full_name = from_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            # Issue #372: 先查是否已有记录——有则 UPDATE（保留已有 host_user_id 不被覆盖），
            # 无则 INSERT。避免 INSERT OR REPLACE 覆盖路径 B 已有的 host_user_id。
            # R14.12: 同时查出 webhook_secret / bot_gitcode_token / bot_github_token，
            # 调用方传空串时保留已有值（防止 B→A 升级后二次调用覆盖 bot token / webhook 密钥）。
            c.execute(
                "SELECT host_user_id, webhook_secret, bot_gitcode_token, bot_github_token "
                "FROM managed_repos WHERE repo_key=?", (repo_key,))
            existing = c.fetchone()
            if existing:
                # 已有记录——UPDATE（不覆盖 host_user_id/webhook_secret/bot_token，除非调用方显式传非空值）
                _existing_host = existing[0] or 0
                # R14.15: 路径 A 成功时（调用方传 bot_token 非空 + host_user_id=0）显式清零，
                # 防止旧的 host_user_id 残留导致读取层继续走用户 token 降级。
                # 路径 B rebind 时（调用方传 host_user_id=0 + bot_token 空）保留已有值。
                if host_user_id > 0:
                    _final_host = host_user_id
                elif bot_gitcode_token or bot_github_token:
                    # 路径 A：显式传了 bot token → 清零 host_user_id
                    _final_host = 0
                else:
                    # 未传 bot token → 保留已有 host_user_id（路径 B rebind / scene 变更等）
                    _final_host = _existing_host
                # R14.12: webhook_secret 空串时保留已有值（防覆盖路径 B 密钥）
                _final_secret = webhook_secret if webhook_secret else (existing[1] or "")
                # R14.12: bot_token 空串时保留已有值（防 B→A 升级后覆盖 bot token）
                _final_bot_gc = bot_gitcode_token if bot_gitcode_token else (existing[2] or "")
                _final_bot_gh = bot_github_token if bot_github_token else (existing[3] or "")
                try:
                    c.execute(
                        "UPDATE managed_repos SET repo_owner=?, repo_name=?, full_name=?, webhook_id=?, dev_scene=?, "
                        "bot_gitcode_token=?, gitcode_webhook_token=?, bot_username=?, github_webhook_token=?, "
                        "bot_github_token=?, org_id=?, host_user_id=?, webhook_secret=? WHERE repo_key=?",
                        (repo_owner, repo_name, full_name, webhook_id, dev_scene or "",
                         _final_bot_gc, gitcode_webhook_token, bot_username, github_webhook_token,
                         _final_bot_gh, org_id, _final_host, _final_secret, repo_key))
                except Exception:
                    # 列不存在时降级（兼容未迁移的库）
                    c.execute(
                        "UPDATE managed_repos SET repo_owner=?, repo_name=?, full_name=?, webhook_id=?, dev_scene=?, "
                        "bot_gitcode_token=?, gitcode_webhook_token=?, bot_username=?, github_webhook_token=?, "
                        "bot_github_token=?, org_id=? WHERE repo_key=?",
                        (repo_owner, repo_name, full_name, webhook_id, dev_scene or "",
                         bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token,
                         bot_github_token, org_id, repo_key))
            else:
                # 无记录——INSERT
                try:
                    c.execute(
                        "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id, host_user_id, webhook_secret) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene or "", bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id, host_user_id, webhook_secret))
                except Exception:
                    # 列不存在时降级
                    c.execute(
                        "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene or "", bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id))
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


def get_scene_recommend_config(repo_key: str) -> dict:
    """读取仓库智能推荐配置（managed_repos.scene_recommend_config）。

    返回 {"fallback_scene": "", "rules": [{"scene_id": "", "condition": ""}]}。
    无记录或未设置返回默认值。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            _cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "scene_recommend_config" not in _cols:
                return {"fallback_scene": "", "rules": []}
            c.execute("SELECT scene_recommend_config FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                import json
                cfg = json.loads(row[0])
                return {
                    "fallback_scene": cfg.get("fallback_scene", "") or "",
                    "rules": cfg.get("rules", []) or [],
                }
    except Exception as e:
        log.warning(f"Get scene recommend config failed: {e}")
    return {"fallback_scene": "", "rules": []}


def set_scene_recommend_config(repo_key: str, fallback_scene: str, rules: list):
    """写入仓库智能推荐配置（managed_repos.scene_recommend_config），幂等。

    写入时自检：过滤 scene_id 不在当前场景列表中的规则。
    fallback_scene 必填，空则报错。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    import json

    # 自检：过滤无效 scene_id
    try:
        from src.api.pipeline_config import get_scenes
        scenes = get_scenes(repo_key=repo_key)
        valid_ids = {s["id"] for s in scenes}
        if not valid_ids:
            log.warning(f"Set scene recommend config: no scenes for {repo_key}")
            return
        rules = [r for r in (rules or []) if isinstance(r, dict) and r.get("scene_id") in valid_ids]
        if fallback_scene and fallback_scene not in valid_ids:
            fallback_scene = scenes[0]["id"] if scenes else ""
            log.info(f"Set scene recommend config: fallback invalid, reset to {fallback_scene}")
    except Exception as e:
        log.warning(f"Set scene recommend config: sanitize failed: {e}")

    cfg = json.dumps({"fallback_scene": fallback_scene or "", "rules": rules or []}, ensure_ascii=False)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?", (cfg, repo_key))
            db.commit()
        log.info(f"Scene recommend config saved: repo={repo_key} fallback={fallback_scene} rules={len(rules)}")
    except Exception as e:
        log.warning(f"Set scene recommend config failed: {e}")


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
    """读取全局 bot 配置，返回 (bot_token, bot_username)。

    Issue #345: 改用统一入口（DB 优先 → 文件 fallback）。
    """
    tok = get_global_gitcode_bot_token()
    us = get_global_bot_usernames()
    user = us[0] if us else ""
    return tok, user


def get_managed_repos(user_id: int, org_id: int = 0, personal_org: bool = False) -> list[dict]:
    """返回该用户可见（JOIN user_repos）的托管仓库；对外 full_name 无前缀、repo_key 带前缀。

    org_id > 0 时按组织过滤（managed_repos.org_id=?），实现组织级仓库隔离。
    org_id=0 时返回全部（兼容旧调用，但实际由调用方 require_org_member 保证 org_id 已解析）。

    personal_org=True（当前组织是个人组织）时放宽 org 过滤：
    自己组织托管的 + 托管在其他个人组织的仓库都显示（与大堂 /repos/member 的
    _filter_repos 对个人组织的口径完全一致——个人组织不隔离其他个人组织、
    但仍隔离其他团队组织）。见 docs/weekly-06/repo-dropdown-grape-invisible-diagnosis.md 方案A。
    注意不能用 user_repos.is_join 判定：VCS 协作者同步的行 is_join=0（is_admin=1），
    大堂同样显示这类仓库，下拉框口径必须一致。
    团队组织（is_personal=0）仍严格按 m.org_id=? 隔离，不影响团队间隔离语义。
    """
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        try:
            if org_id and personal_org:
                c.execute(
                    "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                    "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                    "COALESCE(u.is_join,0), COALESCE(u.is_admin,0) "
                    "FROM managed_repos m "
                    "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                    "WHERE u.repo_key IS NOT NULL "
                    "AND (m.org_id=? OR m.org_id IN (SELECT id FROM organization WHERE is_personal=1)) "
                    "ORDER BY m.full_name", (user_id, org_id))
            elif org_id:
                c.execute(
                    "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                    "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                    "COALESCE(u.is_join,0), COALESCE(u.is_admin,0) "
                    "FROM managed_repos m "
                    "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                    "WHERE u.repo_key IS NOT NULL AND m.org_id=? "
                    "ORDER BY m.full_name", (user_id, org_id))
            else:
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


def _rebuild_webhook_with_bot_token(repo_key: str, bot_token: str, provider: str) -> str:
    """R14.16: 统一 webhook 重建逻辑——删旧 webhook + 用 bot token 建新 webhook。

    被 update_managed_repo_bot_token（手动设置入口）和 auto_upgrade_path_b_repos
    （自动升级入口）共用，确保所有写 bot token 的路径都重建 webhook。

    返回新 webhook_id（字符串），失败返回空串。
    """
    from src.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    _wh_url = get_webhook_url()
    if not _wh_url:
        log.warning(f"_rebuild_webhook_with_bot_token: webhook_url not configured for {repo_key}")
        return ""
    if provider == "github":
        _wh_url = _wh_url.replace("/webhook/gitcode", "/webhook/github")
    try:
        owner, repo_name = split_owner_repo(repo_key)
    except Exception:
        log.warning(f"_rebuild_webhook_with_bot_token: cannot split repo_key {repo_key}")
        return ""
    # 删旧 webhook
    _old_hook_id = _get_managed_webhook_id(repo_key)
    if _old_hook_id:
        try:
            if provider == "github":
                from src.user.repo_manager import remove_github_webhook
                remove_github_webhook(bot_token, owner, repo_name, _old_hook_id)
            else:
                from src.user.repo_manager import remove_webhook
                remove_webhook(bot_token, owner, repo_name, _old_hook_id)
        except Exception as _re:
            log.warning(f"_rebuild_webhook_with_bot_token: remove old webhook failed for {repo_key}: {_re}")
            # 删旧失败 → 不继续建新（旧 webhook 仍在，新建会创建第二个）
            # 返回空串：调用方会清 webhook_secret 并写 bot token，
            # 旧 webhook 的实际密码（旧 secret/token）与 DB 新值不匹配 → webhook 403
            # 这是 loud fail（有日志），用户可 repair_host 恢复
            return ""
    # 用 bot token 建新 webhook
    try:
        if provider == "github":
            from src.user.repo_manager import setup_github_webhook
            return str(setup_github_webhook(bot_token, owner, repo_name, _wh_url, webhook_secret=""))
        else:
            from src.user.repo_manager import setup_webhook
            return str(setup_webhook(bot_token, owner, repo_name, _wh_url, webhook_password=bot_token))
    except Exception as _se:
        log.error(f"_rebuild_webhook_with_bot_token: setup new webhook FAILED for {repo_key}: {_se}")
        return ""


def update_managed_repo_bot_token(repo_key: str, bot_token: str) -> bool:
    """写入仓库级 bot token（管理员手动设置 bot token 用）。

    R14.14: 同时清 host_user_id=0 + webhook_secret=''。
    R14.14: 按 repo_key 前缀分列写入。
    R14.16: 统一走 _update_repo_bot_token + _rebuild_webhook_with_bot_token，
    确保手动设置 token 后 webhook 也用 bot token 重建（同 auto_upgrade 入口）。
    R14.19: 先检查仓库存在再重建 webhook，返回 rowcount 语义（404 检测不失效）。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # 先检查仓库是否存在于 managed_repos（不存在返回 False，调用方走 404）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            if not c.fetchone():
                return False
    except Exception as e:
        log.warning(f"update_managed_repo_bot_token: check existence failed: {e}")
        return False
    provider = "github" if repo_key.startswith("github:") else "gitcode"
    # 重建 webhook（删旧 + 建新），失败时仍写 DB（webhook_id 为空，用户可手动 repair）
    _new_hook_id = _rebuild_webhook_with_bot_token(repo_key, bot_token, provider)
    if not _new_hook_id:
        log.warning(f"update_managed_repo_bot_token: webhook rebuild failed for {repo_key}, "
                    f"DB written but webhook may be stale — user should run repair_host")
    # 统一入口：写 token + 清 host_user_id + 清 webhook_secret + 写 webhook_id
    _update_repo_bot_token(repo_key, bot_token, provider,
                           webhook_token=bot_token, webhook_id=_new_hook_id)
    return True


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
    """按 owner/repo 查仓库 bot token（内部按带前缀 repo_key 命中）。

    R12.12: 路径 B 仓库（host_user_id > 0）守卫下沉到此处——
    所有调用方（scenes/feishu_async/chat/agents/session_store/settings 等 12 处）
    自动继承路径 B 跳过逻辑，与 get_repo_token_with_refresh / get_repo_bot_token 对齐。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{repo_owner}/{repo_name}")
    # R12.12: 路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）
    if get_host_user_id(repo_key) > 0:
        return ""
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

    Issue #372: 路径 B 仓库（无 bot token，有 host_user_id）降级返回 host 用户 token，
    确保 A2A bot token 通道在路径 B 场景下仍可用（远程 worker 无 session token 时）。

    R20.13.1: 改为调 TokenResolver.read_stored_token()——走统一策略链。
    R25.1: 跳过第 3 级 GlobalBotToken 策略，保持"no_global"语义。
    read_stored_token 契约：不检查过期、不触发 refresh、走缓存层。
    """
    from src.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    resolver = TokenResolver(provider)
    # R25.1: 跳过第 3 级 GlobalBotToken（保持"no_global"语义）
    resolver.strategies = [s for s in resolver.strategies if s.level != "global_bot"]
    return resolver.read_stored_token(repo_key)


# Issue #345: 全局 bot token 内存缓存——TTL 60 秒
# bot token 变更频率极低（admin 偶尔改一次），60 秒 TTL 意味着最多 60 秒后所有实例看到新值。
# 集群下不需要缓存失效通知（无 Redis），TTL 自然过期即可。
_global_bot_cache: dict[str, tuple] = {}  # key → (value, expire_ts)
_GLOBAL_BOT_CACHE_TTL = 60


def _cached_get_meta(key: str, file_fallback_fn=None) -> str:
    """带 TTL 缓存的 meta 读取，DB 无值时 fallback 到文件。

    file_fallback_fn: 可选的文件读取函数，DB 返回空时调用。
    确保迁移失败或集群竞态时系统仍可用（向前兼容）。

    迁移完成后（meta.global_bot_token_migrated=1），不再 fallback 到文件——
    admin 清空字段时 DB 存空字符串，不会被文件旧值顶回来。
    """
    import time as _t
    now = _t.time()
    cached = _global_bot_cache.get(key)
    if cached:
        val, expire = cached
        if now < expire:
            return val
    val = get_meta_value(key) or ""
    # 只在迁移未完成时 fallback 到文件
    if not val and file_fallback_fn:
        try:
            _migrated = get_meta_value("global_bot_token_migrated") == "1"
        except Exception:
            _migrated = False
        if not _migrated:
            val = file_fallback_fn()
    _global_bot_cache[key] = (val, now + _GLOBAL_BOT_CACHE_TTL)
    return val


def _invalidate_global_bot_cache():
    """清除全局 bot token 缓存（admin 改配置后调用）。"""
    _global_bot_cache.clear()


def _read_gbt_file_field(field: str) -> str:
    """从 global-bot-token.json 读取指定字段（文件 fallback 用）。"""
    import json, os
    try:
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                  "configs", "global-bot-token.json")
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                return json.load(f).get(field, "") or ""
    except Exception as e:
        log.warning(f"Failed to read {field} from global-bot-token.json: {e}")
    return ""


def _read_gbt_file_usernames() -> str:
    """从 global-bot-token.json 读取 bot_usernames（JSON 数组，返回原始字符串）。"""
    import json, os
    try:
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                  "configs", "global-bot-token.json")
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                us = json.load(f).get("bot_usernames") or []
                return json.dumps(us) if us else ""
    except Exception:
        pass
    return ""


def get_global_gitcode_bot_token() -> str:
    """统一入口：DB meta 表读取 + 文件 fallback（60s 缓存）。

    Issue #345: DB 是权威源。文件 fallback 确保迁移失败或集群竞态时系统仍可用。
    """
    return _cached_get_meta("global_bot_gitcode_token",
                            file_fallback_fn=lambda: _read_gbt_file_field("gitcode_api_token"))


def get_global_github_bot_token() -> str:
    """统一入口：DB meta 表读取 + 文件 fallback（60s 缓存，GitHub 版）。"""
    return _cached_get_meta("global_bot_github_token",
                            file_fallback_fn=lambda: _read_gbt_file_field("github_bot_token"))


def get_global_github_bot_username() -> str:
    """统一入口：DB meta 表读取 + 文件 fallback（60s 缓存）。

    GitHub bot username 不在 bot_usernames 列表中（那是 GitCode 的），
    需要单独存储。用于 GitHub 仓库托管时移除 bot 协作者身份。
    """
    return _cached_get_meta("global_bot_github_username",
                            file_fallback_fn=lambda: _read_gbt_file_field("github_bot_username"))


def get_global_bot_usernames() -> list[str]:
    """统一入口：DB meta 表读取 + 文件 fallback（60s 缓存）。"""
    raw = _cached_get_meta("global_bot_usernames",
                           file_fallback_fn=_read_gbt_file_usernames)
    if raw:
        try:
            import json
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            pass
    return []


def _get_global_bot_token() -> str:
    """读取全局 GitCode bot token——向后兼容包装。

    Issue #345: 内部改为调用统一入口 get_global_gitcode_bot_token()。
    保留原函数名避免大量调用方改动。
    """
    return get_global_gitcode_bot_token()


def get_gitcode_token(repo_key: str = "", user_id: int = 0) -> str:
    """统一获取 GitCode API Token。

    Issue #372: 内部改为调 get_repo_token_with_refresh（统一入口），
    保留 user_id 参数兼容原有调用方。

    - repo_key 非空 → get_repo_token_with_refresh（四级 fallback + 用户 token 降级）
    - user_id > 0 → user token（通过 get_user_gitcode_token 从 external_identities 读）
    """
    if repo_key:
        return get_repo_token_with_refresh(repo_key)
    if user_id > 0:
        return get_user_gitcode_token(user_id)
    return ""


def get_repo_bot_token(repo_key: str = "") -> str:
    """获取仓库级 bot token（不含 agent session token）。

    Issue #343: 建单/关单/评论等用户交互写操作用此函数，不用 get_gitcode_token——
    后者第一优先级是 devops_agent_session.repo_gitcode_token（agent 专属，scope 可能受限）。
    本函数跳过 agent session，直接用 managed_repos.bot_gitcode_token → 全局 bot token。

    Issue #372: 末尾加用户 token 降级（路径 B 托管场景）。
    **不委托 get_repo_token_with_refresh**（后者包含 devops_agent_session 层，会破坏 #343 语义）。

    R18: 内部委托 TokenResolver，跳过第 1 级（DevopsAgentSessionToken）。
    """
    if not repo_key:
        return ""
    from src.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    resolver = TokenResolver(provider)
    resolver.strategies = resolver.strategies[1:]  # 跳过第 1 级 session token
    return resolver.get_token(repo_key)


def get_task_token(task: dict) -> str:
    """Issue #372 R11.4: 获取 task 的 GitCode/GitHub token（公共函数）。

    优先读 task 行的 repo_gitcode_token（路径 A 存入的 bot token）。
    为空时（路径 B 托管）通过实时获取（带自动刷新）。
    按 repo_key 前缀分流：github: → get_github_token，其他 → get_repo_token_with_refresh。
    """
    tok = task.get("repo_gitcode_token") or ""
    if tok:
        return tok
    repo_key = task.get("repo_key") or ""
    if repo_key:
        try:
            if repo_key.startswith("github:"):
                from src.user.db import get_github_token
                return get_github_token(repo_key=repo_key) or ""
            else:
                from src.user.db import get_repo_token_with_refresh
                return get_repo_token_with_refresh(repo_key) or ""
        except Exception:
            pass
    return ""


def get_host_user_id(repo_key: str) -> int:
    """Issue #372: 查 managed_repos.host_user_id（路径 B 托管用户 ID）。

    返回 host_user_id（>0 表示用户托管），0 表示未配置或仓库不存在。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT host_user_id FROM managed_repos WHERE repo_key=? LIMIT 1",
                (repo_key,),
            )
            row = c.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception as e:
        log.warning(f"get_host_user_id: query managed_repos failed: {e}")
    return 0


def get_webhook_secret(repo_key: str) -> str:
    """Issue #372: 查 managed_repos.webhook_secret（webhook 验证密钥，与 token 解耦）。

    返回 webhook_secret 字符串，空字符串表示未配置。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT webhook_secret FROM managed_repos WHERE repo_key=? LIMIT 1",
                (repo_key,),
            )
            row = c.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        log.warning(f"get_webhook_secret: query managed_repos failed: {e}")
    return ""


def get_webhook_verify_token(repo_key: str) -> str:
    """R20.12.1: 获取 webhook 验证用的 token/password。不走 TokenResolver，独立查询。

    R20.12.2: 验证优先级 webhook_secret → webhook_token_field → get_repo_bot_token_no_global（兼容旧数据）。
    R20.12.3: 短路机制不影响 webhook 验证——即使 bot token 过期短路，webhook 验证仍用 webhook_secret / webhook_token。
    R20.12.4: 路径 B 仓库 webhook 验证用 webhook_secret（随机密钥），与 token 过期状态无关。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return ""
    # 1. 查 webhook_secret（路径 B 随机密钥，优先）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT webhook_secret FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        log.warning(f"get_webhook_verify_token: query webhook_secret failed: {e}")
    # 2. 查 webhook_token_field（路径 A bot token 写入的 webhook password）
    _is_github = repo_key.startswith("github:")
    _wt_field = "github_webhook_token" if _is_github else "gitcode_webhook_token"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT {_wt_field} FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        log.warning(f"get_webhook_verify_token: query {_wt_field} failed: {e}")
    # 3. fallback：兼容旧数据（未迁移的仓库）
    return get_repo_bot_token_no_global(repo_key) or ""


def get_host_vcs_username(repo_key: str, provider: str = "") -> str:
    """R20.1.3/R20.1.4: 获取路径 B 仓库的 host 用户 VCS 平台用户名。

    优先从 managed_repos.host_vcs_username 列读取（R16.5.5），
    为空时 fallback 到 external_identities 查询 host_user_id 对应的 provider username。

    R20.1.7: 查询失败时降级为返回空串（不阻断 pipeline）。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return ""
    # 1. 查 managed_repos.host_vcs_username
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT host_vcs_username FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        log.warning(f"get_host_vcs_username: query managed_repos failed: {e}")
    # 2. fallback: 从 external_identities 查
    if not provider:
        provider = "github" if repo_key.startswith("github:") else "gitcode"
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        try:
            c2 = db.cursor()
            c2.execute(
                "SELECT external_login FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status IN (1, 4) LIMIT 1",
                (host_user_id, provider),
            )
            row = c2.fetchone()
            if row and row[0]:
                return row[0]
        except Exception as e:
            log.warning(f"get_host_vcs_username: query external_identities failed: {e}")
    return ""


def sync_host_vcs_username(repo_key: str, login: str):
    """R20.1.5: 同步 host_vcs_username 到 managed_repos（verify_token 返回 login 变更时调用）。"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key or not login:
        return
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET host_vcs_username=? WHERE repo_key=? AND host_user_id>0",
                      (login, repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"sync_host_vcs_username: update failed: {e}")


def get_repo_token_with_refresh(repo_key: str) -> str:
    """Issue #372 R2: 仓库级 token 获取（含用户 token 降级 + 刷新）。

    R18: 内部委托 TokenResolver 策略链。保留原签名向后兼容。

    优先级（保留原 get_gitcode_token 全部三级 + 用户 token 降级）：
      A. devops_agent_session.repo_gitcode_token（agent 专属配置）→ 直接返回，不刷新
      B. managed_repos.bot_gitcode_token（仓库级 Bot Token）→ 直接返回，不刷新
      C. _get_global_bot_token()（全局 Bot Token）→ 直接返回，不刷新
      D. managed_repos.host_user_id → get_user_token_with_refresh(username, host_user_id)
         （用户 OAuth token，支持过期刷新）

    路径 A/B/C 是 bot token（长期有效，无需刷新）；路径 D 是用户 token（OAuth，
    可能过期，由 get_user_token_with_refresh 内部判断刷新）。

    R16.4: 过期短路——标记过期的 token 短路返回空串（用户 token status=4 除外，best-effort）。
    """
    from src.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).get_token(repo_key)


def _is_user_token_repo(repo_key: str) -> bool:
    """Issue #372 R2.4: 判断仓库是否走路径 D（用户 token 降级）。

    前三级（devops_agent_session / managed_repos.bot_gitcode_token / 全局 bot）
    全为空且 host_user_id > 0 → True。用于 task 创建时区分 token 来源——
    用户 OAuth token 会过期，不应写入 task 行，应由运行时实时获取。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return False
    # R22.2.5: 路径 B 仓库（host_user_id > 0）跳过 session token 查询——
    # 与 DevopsAgentSessionToken._fetch 的 host_user_id 守卫对齐（R22.2.4）。
    # 防止路径 A→B 降级后 session token 残留导致 _is_user_token_repo 误返回 False。
    _hid = get_host_user_id(repo_key)
    if _hid > 0:
        # 路径 B 仓库：session token 层无关，直接跳到 bot_gitcode_token 检查
        pass
    else:
        # 路径 A: devops_agent_session.repo_gitcode_token
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT repo_gitcode_token FROM devops_agent_session "
                    "WHERE repo_key=? AND repo_gitcode_token IS NOT NULL "
                    "AND repo_gitcode_token!='' LIMIT 1",
                    (repo_key,),
                )
                if c.fetchone():
                    return False
        except Exception:
            pass
    # 路径 B: managed_repos.bot_gitcode_token
    # R14.13: 路径 B 仓库（host_user_id > 0）跳过此层——与 get_repo_token_with_refresh 守卫对齐，
    # 防止脏写入的 bot_gitcode_token 导致判断不一致
    if _hid == 0:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT bot_gitcode_token FROM managed_repos "
                    "WHERE repo_key=? AND bot_gitcode_token IS NOT NULL "
                    "AND bot_gitcode_token!='' LIMIT 1",
                    (repo_key,),
                )
                if c.fetchone():
                    return False
        except Exception:
            pass
    # 路径 C: 全局 Bot Token
    # R12.11: 路径 B 仓库（host_user_id > 0）跳过全局 bot token 层（与主函数一致）
    # R14.13: 复用上方 _hid 避免重复 DB 查询
    if _hid == 0:
        if _get_global_bot_token():
            return False
    # 路径 D: host_user_id > 0 → 用户 token
    return _hid > 0


def _is_github_user_token_repo(repo_key: str) -> bool:
    """Issue #372 R2.4: 判断 GitHub 仓库是否走用户 token 降级路径。

    GitHub get_github_token fallback：managed_repos.bot_github_token →
    owner external_identities → 全局 → host_user_id 用户 token。
    前三级全为空且 host_user_id > 0 → True。
    """
    from src.repo_key import to_repo_key, parse_repo_key
    repo_key = to_repo_key(repo_key, provider="github")
    if not repo_key:
        return False
    provider, _, _ = parse_repo_key(repo_key)
    if provider != "github":
        return False
    # 1. managed_repos.bot_github_token
    # R14.13: 路径 B 仓库（host_user_id > 0）跳过此层——与 get_github_token 守卫对齐
    _hid = get_host_user_id(repo_key)
    if _hid == 0:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT bot_github_token FROM managed_repos "
                    "WHERE repo_key=? AND bot_github_token!='' LIMIT 1",
                    (repo_key,),
                )
                if c.fetchone():
                    return False
        except Exception:
            pass
    # 2. owner external_identities（owner 有 GitHub 绑定 token → 非 user token 降级）
    # R14.13: 路径 B 仓库跳过此层（与 get_github_token 一致）
    if _hid == 0:
        try:
            from src.repo_key import parse_repo_key as _prk
            from src.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
            _prov, owner, _name = _prk(repo_key)
            if owner:
                acc = get_grape_account_by_username(owner)
                if acc:
                    for i in get_user_identities_with_tokens(acc["id"]):
                        if i.get("provider") == "github" and i.get("access_token"):
                            return False
        except Exception:
            pass
    # 3. 全局 GitHub bot token
    # R12.11: 路径 B 仓库（host_user_id > 0）跳过全局 bot token 层（与主函数一致）
    # R14.13: 复用上方 _hid 避免重复 DB 查询
    if _hid == 0:
        if get_global_github_bot_token():
            return False
    # 4. host_user_id > 0 → 用户 token
    return _hid > 0


def get_github_token(repo_key: str = "") -> str:
    """统一获取 GitHub API Token（对应 get_gitcode_token 的 GitHub 版）。

    R18: 内部委托 TokenResolver（GitHubProvider），保留 D32-补丁 fallback。
    D32-补丁：TokenResolver 返回空时查仓库 owner 的 GitHub identity（标准策略链不含此层）。

    - repo_key 非空 → bot token：managed_repos.bot_github_token → 全局 → 用户 token 降级
    - 无 repo_key → 空（用户级 token 走 get_user_github_token）
    """
    from src.token_resolver import TokenResolver, create_provider
    token = TokenResolver(create_provider("github")).get_token(repo_key)
    if token:
        return token
    # D32-补丁：TokenResolver 返回空时，查仓库 owner 的 GitHub identity
    try:
        from src.repo_key import parse_repo_key, to_repo_key
        repo_key = to_repo_key(repo_key, provider="github")
        if not repo_key:
            return ""
        _prov, owner, _name = parse_repo_key(repo_key)
        if owner:
            from src.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
            acc = get_grape_account_by_username(owner)
            if acc:
                for i in get_user_identities_with_tokens(acc["id"]):
                    if i.get("provider") == "github" and i.get("access_token"):
                        return i["access_token"]
    except Exception as e:
        log.warning(f"get_github_token: D32-patch fallback failed: {e}")
    return ""


def _query_path_b_repos(provider: str = "gitcode") -> list[dict]:
    """Issue #372 §13.2: 查询路径 B 仓库（bot_gitcode_token 空 + host_user_id > 0）。

    用于自动升级——管理员配置全局 bot token 时遍历这些仓库，
    best-effort 邀 bot 为协作者 + 写入 bot_gitcode_token。

    provider: "gitcode" 查 GitCode 路径 B 仓库，"github" 查 GitHub 路径 B 仓库。
    返回 [{repo_key, repo_owner, repo_name, host_user_id}, ...]
    """
    try:
        with db_lock:
            c = db.cursor()
            if provider == "github":
                c.execute(
                    "SELECT repo_key, repo_owner, repo_name, host_user_id FROM managed_repos "
                    "WHERE (bot_github_token IS NULL OR bot_github_token='') "
                    "AND host_user_id > 0 AND repo_key LIKE 'github:%'"
                )
            else:
                c.execute(
                    "SELECT repo_key, repo_owner, repo_name, host_user_id FROM managed_repos "
                    "WHERE (bot_gitcode_token IS NULL OR bot_gitcode_token='') "
                    "AND host_user_id > 0 AND repo_key NOT LIKE 'github:%'"
                )
            rows = c.fetchall()
        return [
            {"repo_key": r[0], "repo_owner": r[1], "repo_name": r[2], "host_user_id": r[3]}
            for r in (rows or [])
        ]
    except Exception as e:
        log.warning(f"_query_path_b_repos: query failed: {e}")
        return []


def _update_repo_bot_token(repo_key: str, bot_token: str, provider: str = "gitcode",
                           webhook_token: str = "", webhook_id: str = ""):
    """Issue #372 §13.2: 写入 managed_repos 的 bot token 字段（路径 B → A 升级用）。

    写入后 get_repo_token_with_refresh 第 2 步自动命中，pipeline 切路径 A。
    Issue #372 §25: 同时清 host_user_id=0，消除语义混合状态。
    R14.1: 同时写入 webhook_token + webhook_id + 清 webhook_secret，
    确保升级后 webhook 验证用 bot token（而非已被清空的旧随机密钥）。
    R16.3.3: 同时清除 bot_token_status 过期标记（新 token 写入时短路自动解除）。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = db.cursor()
            if provider == "github":
                # R14.1: 同步写入 github_webhook_token=bot_token, webhook_id, 清 webhook_secret
                # R16.3.3: 清 bot_token_status
                c.execute(
                    "UPDATE managed_repos SET bot_github_token=?, host_user_id=0, webhook_secret='', "
                    "bot_token_status='', github_webhook_token=?, webhook_id=? WHERE repo_key=?",
                    (bot_token, webhook_token or bot_token, str(webhook_id) if webhook_id else "", repo_key),
                )
            else:
                # R14.1: 同步写入 gitcode_webhook_token=bot_token, webhook_id, 清 webhook_secret
                # R16.3.3: 清 bot_token_status
                c.execute(
                    "UPDATE managed_repos SET bot_gitcode_token=?, host_user_id=0, webhook_secret='', "
                    "bot_token_status='', gitcode_webhook_token=?, webhook_id=? WHERE repo_key=?",
                    (bot_token, webhook_token or bot_token, str(webhook_id) if webhook_id else "", repo_key),
                )
            db.commit()
    except Exception as e:
        log.warning(f"_update_repo_bot_token: update failed: {e}")


def auto_upgrade_path_b_repos(provider: str, bot_token: str, bot_username: str):
    """Issue #372 §13.2: 异步升级路径 B 仓库——邀 bot + 写 bot_gitcode_token。

    best-effort：失败的仓库跳过（兼容修复 §13.1 保证仍走用户 token 正常工作）。
    此函数应在后台线程中调用，不阻塞 save_config 响应。

    provider: "gitcode" 或 "github"
    bot_token: 全局 bot token（刚配置的）
    bot_username: 全局 bot 用户名
    """
    import time
    log.info(f"auto_upgrade_path_b_repos: starting for provider={provider}, repos to check")
    repos = _query_path_b_repos(provider)
    if not repos:
        log.info(f"auto_upgrade_path_b_repos: no path B repos to upgrade for {provider}")
        return
    log.info(f"auto_upgrade_path_b_repos: {len(repos)} path B repos to upgrade for {provider}")
    upgraded = 0
    skipped = 0
    for repo in repos:
        try:
            # 获取 host 用户的 token（用于邀请 bot 为协作者）
            host_user_id = repo["host_user_id"]
            host_token = ""
            if provider == "github":
                from src.api.deps import get_user_github_token_with_refresh
                from src.auth.grape_account_db import get_grape_account_by_id
                acc = get_grape_account_by_id(host_user_id)
                if acc and acc.get("username"):
                    host_token = get_user_github_token_with_refresh(acc["username"], host_user_id) or ""
            else:
                from src.api.deps import get_user_token_with_refresh
                from src.auth.grape_account_db import get_grape_account_by_id
                acc = get_grape_account_by_id(host_user_id)
                if acc and acc.get("username"):
                    host_token = get_user_token_with_refresh(acc["username"], host_user_id) or ""
            if not host_token:
                skipped += 1
                continue  # host user token 过期，跳过

            # 邀请 bot 为协作者
            owner = repo["repo_owner"]
            repo_name = repo["repo_name"]
            if provider == "github":
                # R12.2: GitHub 需要显式 accept（原注释"不需显式 accept"是错误的）
                from src.user.repo_manager import add_github_collaborator, accept_github_invite
                _gh_ok = add_github_collaborator(host_token, owner, repo_name, bot_username, "push")
                if not _gh_ok:
                    skipped += 1
                    log.warning(f"auto_upgrade: add_github_collaborator failed for {repo['repo_key']}, skipping")
                    continue
                if not accept_github_invite(bot_token, owner, repo_name):
                    skipped += 1
                    log.warning(f"auto_upgrade: accept_github_invite failed for {repo['repo_key']}, skipping")
                    continue
            else:
                from src.user.repo_manager import add_collaborator, accept_invite_for_repo
                _ok, _err = add_collaborator(host_token, owner, repo_name, bot_username, "maintainer")
                if not _ok:
                    skipped += 1
                    log.warning(f"auto_upgrade: add_collaborator failed for {repo['repo_key']}: {_err}, skipping")
                    continue
                # R12.8: 校验 accept 返回值，失败则 skip 不写 token
                if not accept_invite_for_repo(bot_token, owner, repo_name):
                    skipped += 1
                    log.warning(f"auto_upgrade: accept_invite_for_repo failed for {repo['repo_key']}, skipping")
                    continue

            # R14.16: 统一走 _rebuild_webhook_with_bot_token（与手动设置入口共用）
            # 部分失败处理：删旧失败 → 不建新（返回空），旧 webhook 保留；
            # 建新失败 → 返回空，DB 写空 webhook_id，用户可 repair_host
            _rk = repo["repo_key"]
            _new_hook_id = _rebuild_webhook_with_bot_token(_rk, bot_token, provider)
            if not _new_hook_id:
                log.warning(f"auto_upgrade: webhook rebuild failed for {_rk}, "
                            f"DB token written but webhook may need manual repair")
                # webhook 重建失败：token 写入但 webhook 可能不工作
                # 仍计为 upgraded（bot 协作者已邀请成功），但标记 webhook 重建失败
                _update_repo_bot_token(_rk, bot_token, provider,
                                       webhook_token=bot_token, webhook_id="")
                upgraded += 1
                _webhook_rebuild_failed = getattr(auto_upgrade_path_b_repos, '_webhook_rebuild_failed', 0) + 1
                setattr(auto_upgrade_path_b_repos, '_webhook_rebuild_failed', _webhook_rebuild_failed)
            else:
                _update_repo_bot_token(_rk, bot_token, provider,
                                       webhook_token=bot_token, webhook_id=_new_hook_id)
                upgraded += 1
            # 限流保护：每次邀请间隔 1 秒
            time.sleep(1)
        except Exception as e:
            skipped += 1
            log.warning(f"auto_upgrade_path_b_repos: upgrade failed for {repo.get('repo_key')}: {e}")
            continue
    _wbf = getattr(auto_upgrade_path_b_repos, '_webhook_rebuild_failed', 0)
    if _wbf:
        log.info(f"auto_upgrade_path_b_repos: done for {provider}: upgraded={upgraded}, skipped={skipped}, webhook_rebuild_failed={_wbf}")
        setattr(auto_upgrade_path_b_repos, '_webhook_rebuild_failed', 0)  # reset for next call
    else:
        log.info(f"auto_upgrade_path_b_repos: done for {provider}: upgraded={upgraded}, skipped={skipped}")


def get_agent_bot_token(repo_key: str) -> str:
    """获取 Agent 执行的 GitCode Bot Token（兼容包装，统一走 get_gitcode_token）"""
    return get_gitcode_token(repo_key=repo_key)


def get_user_gitcode_token(user_id: int) -> str:
    """获取用户的 GitCode API token（D32-补丁：通过 get_credentials 从 external_identities 读）"""
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


def get_user_github_token_by_id(user_id: int) -> str:
    """获取用户的 GitHub API token（D32-补丁：通过 get_credentials 从 external_identities 读）。

    Issue #343: 原名 get_user_github_token(user_id: int) 与 deps.py 的
    get_user_github_token(username: str) 同名不同签名，极易混淆。
    改名为 get_user_github_token_by_id 消除歧义。
    保留 get_user_github_token 作为别名向后兼容（内部调用）。
    """
    if not user_id or user_id <= 0:
        return ""
    try:
        creds = get_credentials(user_id)
        for c in creds:
            if c.get("provider") == "github":
                return c.get("config", {}).get("token", "") or ""
    except Exception as e:
        log.warning(f"get_user_github_token_by_id failed for user={user_id}: {e}")
    return ""


def get_user_github_token(user_id: int) -> str:
    """向后兼容别名，委托给 get_user_github_token_by_id。"""
    return get_user_github_token_by_id(user_id)


def _get_username_by_id(user_id: int) -> str:
    """根据 user_id 查 username（D32-补丁：查 grape_accounts，不查 user_accounts）。"""
    try:
        from src.auth.grape_account_db import get_grape_account_by_id
        acc = get_grape_account_by_id(user_id)
        return acc["username"] if acc else ""
    except Exception:
        return ""


def refresh_gitcode_oauth_token(user_id: int) -> str | None:
    """用 refresh_token 刷新 GitCode OAuth access_token（Issue #52）。

    D32-补丁：委托给通用 refresh_oauth_token(user_id, "gitcode")，消除代码重复。
    """
    return refresh_oauth_token(user_id, "gitcode")


def get_webhook_token_by_repo(full_name: str) -> str:
    """根据 full_name（owner/repo）查找 repo 的预期 webhook token（按 provider 分流）。

    Issue #372: 优先返回 webhook_secret（路径 B 随机密钥，与 token 解耦），
    为空时 fallback 到 gitcode_webhook_token / github_webhook_token（路径 A 兼容）。
    """
    from src.repo_key import to_repo_key
    with db_lock:
        c = db.cursor()
        # 同时查 gitcode: 和 github: 前缀
        gc_key = to_repo_key(full_name)  # 默认 gitcode:
        gh_key = to_repo_key(full_name, provider="github")
        try:
            c.execute(
                "SELECT webhook_secret, gitcode_webhook_token, github_webhook_token FROM managed_repos WHERE repo_key IN (?, ?) ORDER BY webhook_secret DESC LIMIT 1",
                (gc_key, gh_key)
            )
            row = c.fetchone()
        except Exception:
            # 列不存在时降级（兼容未迁移的库）
            c.execute(
                "SELECT gitcode_webhook_token, github_webhook_token FROM managed_repos WHERE repo_key IN (?, ?) ORDER BY gitcode_webhook_token DESC LIMIT 1",
                (gc_key, gh_key)
            )
            row = c.fetchone()
            if not row:
                return ""
            return row[1] or row[0] or ""
        if not row:
            return ""
        # 优先 webhook_secret（路径 B），fallback 到 webhook_token（路径 A）
        webhook_secret = row[0] or ""
        if webhook_secret:
            return webhook_secret
        # GitHub 仓库有 github_webhook_token，GitCode 有 gitcode_webhook_token
        return row[2] or row[1] or ""


def remove_managed_repo(repo_key: str) -> dict | None:
    """删除仓库级托管行，返回被删除的记录（含 webhook_id）或 None；同时复位所有用户 is_join。

    R19.3.4: 删除仓库时清理 devops_agent_session 中的 token 字段。
    """
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
            # R19.3.4: 清理 devops_agent_session 中的 token 字段
            try:
                c.execute(
                    "UPDATE devops_agent_session SET repo_gitcode_token='', repo_github_token='', token_status='' "
                    "WHERE repo_key=?", (repo_key,))
            except Exception:
                pass  # 列可能不存在（未 ALTER），忽略
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
    "github_id", "github_api_token", "github_login",
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


@_deprecated_user_accounts_fn("grape_account_db.get_grape_account_by_username")
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


@_deprecated_user_accounts_fn("grape_account_db.soft_delete_account")
def delete_user_account(username: str) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username=?", (username,))
        db.commit()
        return c.rowcount > 0


def get_all_bot_usernames() -> list[str]:
    """获取所有托管仓库配置的 bot 用户名列表（用于 self-comment 检测）。

    Issue #345: 全局 bot 用户名改用统一入口（DB 优先 → 文件 fallback）。
    """
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
    result.extend(get_global_bot_usernames())
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
    再兜底从 webhook_url 剥掉 /webhook/gitcode 后缀；最后 http://localhost:{GRAPE_PORT}（默认 3000）。
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
    # Issue #372 §15.5: fallback 端口从 GRAPE_PORT 读取，默认 3000（非硬编码 8080）
    _port = os.environ.get("GRAPE_PORT", "3000")
    return f"http://localhost:{_port}"


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


def detect_webhook_from_host(host: str, forwarded_host: str = "", forwarded_proto: str = "") -> str:
    """Issue #372 §14.2.2: 从入站请求头推导 webhook_url。

    支持nginx 代理场景：
    - 优先用 forwarded_host（X-Forwarded-Host，nginx 配 $http_host 时含端口）
    - fallback 到 host（Host 头，直连场景含端口）
    - forwarded_proto（X-Forwarded-Proto）判断 https

    安全约束：
    - 排除 localhost / 127.0.0.1 / 0.0.0.0（本地开发不自动发现）
    - 只在调用方确认请求已认证后才调用此函数
    - Issue #372 §32: TRUSTED_HOSTS 白名单校验（如果配置了），防止伪造 Host 头

    返回推导的 webhook_url（含 /webhook/gitcode 后缀），无效 host 返回空串。
    """
    # R11.1: 未设 TRUSTED_HOSTS 时不读 X-Forwarded-Host（防伪造）
    _trusted = os.environ.get("TRUSTED_HOSTS", "").strip()
    if _trusted:
        # 已设白名单 → 可以信任 X-Forwarded-Host（经 nginx 设置）
        raw = (forwarded_host or host or "").strip()
    else:
        # 未设白名单 → 仅读 Host header（不读 X-Forwarded-Host，防伪造）
        raw = (host or "").strip()
    if not raw:
        return ""
    raw = raw.lower()
    # 排除本地开发地址
    _local = ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    for _lh in _local:
        if raw == _lh or raw.startswith(_lh + ":"):
            return ""
    # Issue #372 §32: TRUSTED_HOSTS 白名单校验
    # _trusted 已在上方读取（R11.1）
    if _trusted:
        _trusted_list = [h.strip().lower() for h in _trusted.split(",") if h.strip()]
        _host_no_port = raw.split(":")[0]
        _host_with_port = raw
        _matched = any(_host_no_port == t or _host_with_port == t or
                       _host_no_port.endswith("." + t) for t in _trusted_list)
        if not _matched:
            log.warning(f"detect_webhook_from_host: host '{raw}' not in TRUSTED_HOSTS, rejected")
            return ""
    # 协议：X-Forwarded-Proto 优先，无则默认 http
    proto = (forwarded_proto or "").strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    # host 可能含端口（如 1.2.3.4:3000），保留端口
    return f"{proto}://{raw}/webhook/gitcode"


def migrate_oauth_providers():
    """从配置文件同步 OAuth 配置到 meta 表——统一入口。

    优先级：oauth-providers.json > global-bot-token.json（向前兼容）。

    - 协议参数（oauth_providers_config）：覆盖模式（配置文件是真相源）
    - 凭据（client_id/secret）：幂等模式（仅首次写入，管理员可通过 API 修改不被覆盖）
    """
    config_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "configs")

    # 1. 尝试读取 oauth-providers.json
    new_path = os.path.join(config_dir, "oauth-providers.json")
    if os.path.exists(new_path):
        try:
            with open(new_path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            log.warning(f"Failed to load oauth-providers.json: {e}")
            data = {}
        if data:
            try:
                _sync_providers_from_config(data)
            except Exception as e:
                log.warning(f"migrate_oauth_providers: sync from oauth-providers.json failed: {e}")
            return

    # 2. 向前兼容：读取 global-bot-token.json 中的旧 OAuth 字段
    old_path = os.path.join(config_dir, "global-bot-token.json")
    if not os.path.exists(old_path):
        return
    try:
        with open(old_path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        log.warning(f"Failed to load global-bot-token.json: {e}")
        return
    _oauth_keys = [
        ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
        ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
        ("github_oauth_client_id", "oauth_github_client_id"),
        ("github_oauth_client_secret", "oauth_github_client_secret"),
    ]
    loaded = []
    try:
        with db_lock:
            c = db.cursor()
            for src_key, meta_key in _oauth_keys:
                val = data.get(src_key, "") or ""
                if not val:
                    continue
                c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                if c.fetchone()[0] == 0:
                    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                              (meta_key, val))
                    loaded.append(meta_key)
            db.commit()
        if loaded:
            log.info(f"Loaded OAuth credentials from global-bot-token.json (fallback): {loaded}")
    except Exception as e:
        log.warning(f"migrate_oauth_providers: fallback sync failed: {e}")
    # 协议参数不写——由 _DEFAULT_PROVIDERS 兜底


def _sync_providers_from_config(data: dict):
    """从 oauth-providers.json 格式的配置同步到 meta 表。

    新文件格式：嵌套的 { provider: { name, auth_url, ..., client_id, client_secret } }
    - 协议参数（不含 client_id/secret）：覆盖写入 oauth_providers_config
    - 凭据（client_id/secret）：幂等写入 oauth_{provider}_client_id/secret
    """
    providers_cfg = {}
    loaded_creds = []
    with db_lock:
        c = db.cursor()
        for provider, cfg in data.items():
            # 剥离 client_id/secret，只存协议参数
            prov_cfg = {k: v for k, v in cfg.items()
                        if k not in ("client_id", "client_secret")}
            providers_cfg[provider] = prov_cfg
            # 幂等同步凭据
            for cred_field in ("client_id", "client_secret"):
                val = cfg.get(cred_field, "") or ""
                if not val:
                    continue
                meta_key = f"oauth_{provider}_{cred_field}"
                c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                if c.fetchone()[0] == 0:
                    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                              (meta_key, val))
                    loaded_creds.append(meta_key)
        # 覆盖写入协议参数
        if providers_cfg:
            import json as _json
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                      ("oauth_providers_config", _json.dumps(providers_cfg, ensure_ascii=False)))
        db.commit()
    log.info(f"Synced OAuth providers from oauth-providers.json: "
             f"providers={list(providers_cfg.keys())}, credentials={loaded_creds}")


def migrate_accounts_from_json():
    """首次运行加载配置：迁移旧数据到 meta 表。

    OAuth 配置迁移已移交给 migrate_oauth_providers()（统一入口）。
    本函数仅保留 default 账户清理（旧版 user_accounts default 账户的 OAuth 配置迁移到 meta 表）。
    """
    # OAuth 配置由 migrate_oauth_providers 统一处理
    migrate_oauth_providers()

    # default 账户迁移（旧版兼容——OAuth 配置曾存于 user_accounts default 账户）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT * FROM user_accounts WHERE username='default'")
            row = c.fetchone()
            if row:
                cols = [d[0] for d in c.description]
                acc = dict(zip(cols, row))
                _oauth_keys = [
                    ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
                    ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
                    ("github_oauth_client_id", "oauth_github_client_id"),
                    ("github_oauth_client_secret", "oauth_github_client_secret"),
                    # 不迁移 redirect_uri——D27 已废弃
                ]
                migrated = False
                for src_key, meta_key in _oauth_keys:
                    val = acc.get(src_key, "") or ""
                    if val:
                        c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                        if c.fetchone()[0] == 0:
                            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                                      (meta_key, val))
                            migrated = True
                c.execute("DELETE FROM user_accounts WHERE username='default'")
                db.commit()
                if migrated:
                    log.info("Migrated OAuth config from default account to meta table")
                else:
                    log.info("Deleted default account (no config to migrate)")
    except Exception as e:
        log.warning(f"Migrate/delete default account failed: {e}")


def link_user_provider(user_id: int, provider: str, external_id: str,
                        access_token: str, refresh_token: str = "") -> bool:
    """通用 OAuth 绑定——统一写入 user_credentials。

    D32-补丁：不再写 user_accounts 老表专属列（gitcode_id/github_id）。
    此函数已无外部调用方（_sync_legacy_binding 已删除），保留仅作历史兼容。
    """
    provider_key = f"{provider}_oauth"
    try:
        with db_lock:
            c = db.cursor()
            # 统一写入 user_credentials
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) VALUES (?,?,?,datetime('now'))",
                      (user_id, provider_key, json.dumps({
                          f"{provider}_id": external_id,
                          "access_token": access_token,
                          "refresh_token": refresh_token,
                          "fetched_at": int(time.time())
                      }, ensure_ascii=False)))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Link {provider} user failed: {e}")
        return False


def _read_refresh_token_from_identity(user_id: int, provider: str) -> str:
    """从 external_identities 读 refresh_token（权威存储，解密）。

    需求 4：fallback 判定链的第一步。
    - 无记录 → 返回空
    - refresh_token 字段为空 → 返回空
    - 解密失败 → 返回空（safe 模式，不抛异常）
    - 解密成功 → 返回明文
    """
    try:
        from src.session_manager import db as _sm_db, db_lock as _sm_dl
        from src.auth.credential_crypto import decrypt
        with _sm_dl:
            c = _sm_db.cursor()
            c.execute(
                "SELECT refresh_token FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (user_id, provider),
            )
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        try:
            return decrypt(row[0])
        except Exception as e:
            log.warning(f"refresh_oauth: decrypt refresh_token failed for "
                       f"user={user_id}, provider={provider}: {e}")
            return ""
    except Exception as e:
        log.warning(f"_read_refresh_token_from_identity failed: {e}")
        return ""


def _read_refresh_token_from_credentials(user_id: int, provider: str) -> str:
    """从 user_credentials 读 refresh_token（迁移期 fallback）。

    使用同一密钥解密——如果 external_identities 解密失败，
    user_credentials 大概率也解密失败。
    """
    provider_key = f"{provider}_oauth"
    cred = get_credential(user_id, provider_key)
    if not cred or not cred.get("refresh_token"):
        return ""
    return cred["refresh_token"]


def refresh_oauth_token(user_id: int, provider: str) -> str | None:
    """通用 OAuth token 刷新——用 provider 配置里的 token_url，消除硬编码 URL。

    需求 4：refresh_token 权威存储改为 external_identities（加密），
    fallback 读 user_credentials（迁移期兼容）。
    需求 5：删除 provider 硬编码，统一更新 external_identities。

    成功时更新：
    - external_identities.access_token + refresh_token（权威存储）
    - user_credentials.config_json（迁移期同步保留）

    返回新的 access_token，失败返回 None。
    """
    import urllib.request, urllib.parse, json as _json

    # 1. 从 external_identities 读 refresh_token（权威存储）
    rt = _read_refresh_token_from_identity(user_id, provider)
    if not rt:
        # fallback：从 user_credentials 读（迁移期兼容）
        rt = _read_refresh_token_from_credentials(user_id, provider)
    if not rt:
        log.warning(f"refresh_oauth: no refresh_token for user={user_id}, provider={provider}")
        return None

    client_id = get_meta_value(f"oauth_{provider}_client_id")
    client_secret = get_meta_value(f"oauth_{provider}_client_secret")
    if not client_id or not client_secret:
        log.warning(f"refresh_oauth: OAuth client_id/secret not configured for {provider}")
        return None

    # 2. 获取 token_url（从 provider 配置）
    from src.auth.providers import get_provider
    cfg = get_provider(provider)
    if not cfg:
        log.warning(f"refresh_oauth: provider {provider} not configured")
        return None
    token_url = cfg["token_url"]

    # 3. 调 OAuth refresh endpoint
    try:
        req = urllib.request.Request(
            token_url,
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": client_id,
                "client_secret": client_secret,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        token_data = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"refresh_oauth: refresh failed for user={user_id}, provider={provider}: {e}")
        # R21.4.2: refresh 失败时重读 external_identities 获取最新 refresh_token 重试一次
        # 场景：双主集群下另一实例已刷新了 token，本实例持有的旧 refresh_token 已失效
        _new_rt = _read_refresh_token_from_identity(user_id, provider)
        if _new_rt and _new_rt != rt:
            log.info(f"refresh_oauth: retrying with updated refresh_token for user={user_id}, provider={provider}")
            try:
                req2 = urllib.request.Request(
                    token_url,
                    data=urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "refresh_token": _new_rt,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST"
                )
                resp2 = urllib.request.urlopen(req2, timeout=10)
                token_data = _json.loads(resp2.read())
            except Exception as e2:
                log.warning(f"refresh_oauth: retry also failed for user={user_id}, provider={provider}: {e2}")
                return None
        else:
            return None

    new_access = token_data.get("access_token", "")
    new_refresh = token_data.get("refresh_token", "")
    new_expires_in = token_data.get("expires_in", 0)
    if not new_access:
        log.warning(f"refresh_oauth: no access_token in response for user={user_id}, provider={provider}")
        return None

    # 4. 更新 external_identities（权威存储）——不限制 provider
    from src.auth.grape_account_db import update_external_identity_tokens
    update_external_identity_tokens(
        user_id, provider,
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=new_expires_in,
    )

    # 5. 同步更新 user_credentials（迁移期保留，上层 deps.py 读 fetched_at/token_expires_at 判断过期）
    # Issue #343: cred 不存在时创建新条目，否则预刷新永远跳过（user_credentials 无 gitcode_oauth → 死路）
    provider_key = f"{provider}_oauth"
    cred = get_credential(user_id, provider_key)
    new_cred = dict(cred) if cred else {}
    new_cred["access_token"] = new_access
    if new_refresh:
        new_cred["refresh_token"] = new_refresh
    new_cred["fetched_at"] = int(time.time())
    if new_expires_in:
        # Issue #343: 统一 token_expires_at 格式为 epoch int（和 external_identities 的 TEXT 格式不同，
        # 但 user_credentials 的 config_json 是 JSON dict，int 更自然）。
        # deps.py get_user_token_with_refresh 读此处 token_expires_at 做 (epoch - now) < 600 数值比较。
        new_cred["token_expires_at"] = int(time.time()) + new_expires_in
        new_cred["expires_in"] = new_expires_in
    set_credential(user_id, provider_key, new_cred)

    log.info(f"refresh_oauth: success for user={user_id}, provider={provider}")
    return new_access


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
            # 优先使用 repos 中已有的 repo_key（含 provider 前缀），否则用 full_name 生成
            repo_key = r.get("repo_key", "") or to_repo_key(full_name)
            is_join = 1 if repo_key in joined_keys else 0
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, description, role, is_admin, is_join, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, repo_key, r.get("owner", ""), r.get("name", ""),
                 r.get("description", ""), r.get("role", ""), 1 if r.get("is_admin") else 0, is_join,
                 r.get("created_at", "") or "")
            )
        db.commit()


def get_user_repos(user_id: int, org_id: int = 0) -> list[dict]:
    """读取用户仓库缓存（对外返回 full_name 无前缀）。

    org_id 参数保留兼容，但实际过滤在 _filter_repos 中完成
    （需要同时返回未托管的仓库供用户托管到当前组织）。
    """
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
                # 匹配已有本机直连行：优先 host_name 精确匹配，其次 host_name 为空/NULL 的遗留孤儿行（claim 它）
                # 防止 host_name 字段引入后旧行（host_name=''）匹配不上 → 新建重复行 → 前端显示两个 opencode-cred0
                c.execute(
                    "SELECT id FROM agent_runtimes WHERE name=? AND scope=? AND owner_id=? AND credential_id=0 "
                    "AND (host_name=? OR host_name='' OR host_name IS NULL) "
                    "ORDER BY CASE WHEN host_name=? THEN 0 ELSE 1 END, id LIMIT 1",
                    (name, scope, owner_id, host_name, host_name))
                row = c.fetchone()
                if row:
                    c.execute(
                        "UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, healthy=?, "
                        "pip_package=?, status='online', last_seen_at=datetime('now'), updated_at=datetime('now'), "
                        "local_skills=?, skill_scan_result=?, host_name=? WHERE id=?",
                        (a.get("display_name", name),
                         1 if a.get("installed") else 0, a.get("cli_path", ""),
                         a.get("version", ""), 1 if a.get("healthy") else 0,
                         a.get("pip_package", ""), _ls_json, _detail_json, host_name, row[0])
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
            # 去重：同 name+scope+owner_id+org_id+cred=0 的重复行只保留 id 最小的一条
            # （upsert 已 claim 旧行并写入 host_name，此处清理未被 claim 的重复行）
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id=0 AND id NOT IN "
                "(SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 "
                "GROUP BY lower(name), scope, owner_id, COALESCE(org_id, 0))"
            )
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
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, worker_version, org_id FROM agent_runtimes ORDER BY name")
        else:
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, worker_version, org_id FROM agent_runtimes WHERE scope=? AND (owner_id=? OR owner_id='') ORDER BY name", (scope, owner_id))
        return _rows_to_runtimes(c.fetchall())



def _rows_to_runtimes(rows) -> list[dict]:
    """将 DB 行列表转为 runtime dict 列表（供 get_agent_runtimes / paged 复用）。"""
    # AIShell 显示名（2026-10-13）：出口统一转换 name/display_name（内部键不变）
    from src.runtime.instances import to_display as _td
    return [
        {"id": r[0], "name": _td(r[1]), "display_name": _td(r[2] or r[1]), "installed": bool(r[3]),
         "cli_path": r[4], "version": r[5], "healthy": bool(r[6]),
         "pip_package": r[7], "updated_at": r[8], "scope": r[9], "owner_id": r[10],
         "credential_id": r[11] or 0, "status": r[12] or "", "last_seen_at": r[13],
         "local_skills": json.loads(r[14] or "[]") if r[14] else [],
         "skill_scan_result": json.loads(r[15] or "[]") if r[15] else [],
         "agent_tags": json.loads(r[16]) if r[16] else [],
         "host_name": r[17] or "",
         "max_concurrent": r[18] or 20,
         "scene_tags": json.loads(r[19]) if r[19] else ["default"],
         "alias": r[20] or "",
         "worker_version": r[21] or "",
         "org_id": r[22] or 0}
        for r in rows
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
        c.execute("SELECT id, name, slug, icon FROM scene_category WHERE id=?", (category_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "slug": row[2] or "", "icon": row[3] or ""}


def list_skill_categories() -> list[dict]:
    """返回启用的技能分类列表，按 order_num 排序"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, slug, icon, order_num FROM skill_category "
            "WHERE is_active=1 ORDER BY order_num"
        )
        return [{"id": r[0], "name": r[1], "slug": r[2] or "", "icon": r[3] or "", "order_num": r[4]}
                for r in c.fetchall()]


def get_skill_category(category_id: int) -> dict | None:
    """按 id 查技能分类"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, slug, icon FROM skill_category WHERE id=?", (category_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "slug": row[2] or "", "icon": row[3] or ""}


def list_scene_market(q: str = "", category_id: int = 0, sort: str = "latest",
                      featured_only: int = 0, limit: int = 0,
                      page: int = 1, page_size: int = 20,
                      scope: str = "", repo_key: str = "",
                      user_id: int = 0) -> tuple[list[dict], int]:
    """查询场景市场列表（is_active=1），支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页 + scope 过滤。

    sort: latest（按 updated_at 降序）/ hot（按 like_count 降序）。
    featured_only: 1=只查精选场景，0=全部。
    limit: >0 限制返回条数（精选场景用，不分页）。
    page/page_size: 分页参数（limit=0 时生效），page 从 1 开始。
    scope: official / repo，空=全部。
      scope='repo': 查当前仓库下所有用户的仓库场景（scope='repo' AND repo_key=?）
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
    if scope:
        where.append("scope=?")
        params.append(scope)
        if scope == "repo" and repo_key:
            where.append("repo_key=?")
            params.append(repo_key)
    where_sql = " AND ".join(where)
    order_by = "updated_at DESC" if sort != "hot" else "like_count DESC"

    # 精选模式：优先按 admin 管理台设置的排序返回
    featured_ordered_ids = []
    if limit and limit > 0 and featured_only:
        from src.featured_order import get_featured_order
        featured_ordered_ids = get_featured_order("scene")

    with db_lock:
        c = db.cursor()
        # 总数
        c.execute(f"SELECT COUNT(*) FROM scene_market WHERE {where_sql}", params)
        total = c.fetchone()[0]

        # 分页
        if limit and limit > 0:
            if featured_only and featured_ordered_ids:
                # 精选模式且有 admin 排序：查全部精选，排序后再截取前 limit 条
                limit_sql = ""
            else:
                # 精选模式但无 admin 排序：直接 limit
                limit_sql = f" LIMIT {int(limit)}"
        else:
            offset = (page - 1) * page_size
            limit_sql = f" LIMIT {int(page_size)} OFFSET {int(offset)}"
        c.execute(
            f"SELECT id, name, display_name, version, description, market_description, "
            f"icon, obs_icon_key, category_id, is_featured, like_count, source, recommended_template, "
            f"nodes_json, onboarding, created_at, scope "
            f"FROM scene_market WHERE {where_sql} ORDER BY {order_by}{limit_sql}",
            params,
        )
        rows = c.fetchall()

    # 精选模式且有 admin 排序：按 admin 顺序重排结果
    if featured_ordered_ids and limit and limit > 0:
        row_map = {r[0]: r for r in rows}
        reordered = []
        for item_id in featured_ordered_ids:
            r = row_map.get(item_id)
            if r:
                reordered.append(r)
        # admin 排序中不存在的精选场景，追加到末尾（保持原 updated_at 顺序）
        for r in rows:
            if r[0] not in featured_ordered_ids:
                reordered.append(r)
        rows = reordered[:limit]
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
            "scope": r[16] or "official",
        })
    return result, total


def get_scene_market_by_id(market_id: int) -> dict | None:
    """按主键 id 查场景市场详情"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, display_name, version, description, market_description, "
            "icon, obs_icon_key, category_id, is_featured, like_count, source, source_user_id, "
            "recommended_template, nodes_json, onboarding, is_active, created_at, updated_at, "
            "scope, repo_key, preset_custom_fields "
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
        "scope": row[19] or "official", "repo_key": row[20] or "",
        "preset_custom_fields": row[21] or "[]",
    }


# ====== Scene Market CRUD（用户自定义场景，Issue-54）======

def create_scene_market(name: str, display_name: str = "", description: str = "",
                        scope: str = "repo", repo_key: str = "", source_user_id: int = 0,
                        nodes_json: str = "[]", icon: str = "", obs_icon_key: str = "",
                        category_id: int = 1, recommended_template: str = "",
                        onboarding: str = "repo-onboarding", created_by: str = "",
                        market_description: str = "") -> int:
    """创建用户自定义场景（写入 scene_market 表）。返回新记录 id。

    UNIQUE (name, scope, source_user_id, repo_key) 约束冲突时抛 IntegrityError。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market "
            "(name, display_name, version, description, market_description, icon, obs_icon_key, "
            "category_id, is_featured, like_count, source, source_user_id, recommended_template, "
            "nodes_json, onboarding, tags, author, content_hash, is_active, scope, repo_key, "
            "created_by, updated_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,'user',?,?,?,?, '','', '', 1, ?,?, ?,?, datetime('now'),datetime('now'))",
            (name, display_name or name, "1.0.0", description, market_description or description,
             icon, obs_icon_key, category_id, source_user_id,
             recommended_template, nodes_json, onboarding,
             scope, repo_key, created_by, created_by),
        )
        db.commit()
        return c.lastrowid


def update_scene_market(market_id: int, name: str = "", display_name: str = "",
                        description: str = "", nodes_json: str = "",
                        icon: str = "", obs_icon_key: str = "",
                        category_id: int = 0, recommended_template: str = "",
                        updated_by: str = "") -> bool:
    """更新用户自定义场景（scope != 'official'）。返回是否更新成功。

    版本号自动管理：只有内容真正变更才 patch+1，无变更不涨。
    版本号仅代表编辑轮次，不是标准语义化版本。
    """
    with db_lock:
        c = db.cursor()
        # 读取旧数据做内容比对
        c.execute(
            "SELECT name, display_name, description, market_description, nodes_json, "
            "icon, obs_icon_key, category_id, recommended_template, version "
            "FROM scene_market WHERE id=? AND scope!='official'",
            [market_id],
        )
        row = c.fetchone()
        if not row:
            return False

        old_name, old_display, old_desc, old_mkt_desc, old_nodes, old_icon, old_obs_key, old_cat, old_tpl, old_version = row

        # 比对变更
        changed = False
        sets = []
        params: list = []
        if name and name != old_name:
            sets.append("name=?"); params.append(name); changed = True
        if display_name and display_name != old_display:
            sets.append("display_name=?"); params.append(display_name); changed = True
        if description != old_desc:
            sets.append("description=?"); sets.append("market_description=?")
            params.append(description); params.append(description); changed = True
        if nodes_json and nodes_json != old_nodes:
            sets.append("nodes_json=?"); params.append(nodes_json); changed = True
        if icon and icon != old_icon:
            sets.append("icon=?"); params.append(icon); changed = True
        if obs_icon_key and obs_icon_key != old_obs_key:
            sets.append("obs_icon_key=?"); params.append(obs_icon_key); changed = True
        if category_id and category_id != old_cat:
            sets.append("category_id=?"); params.append(category_id); changed = True
        if recommended_template and recommended_template != old_tpl:
            sets.append("recommended_template=?"); params.append(recommended_template); changed = True

        if not changed:
            return True  # 无变更，不升级版本

        # patch+1
        parts = (old_version or "1.0.0").split(".")
        if len(parts) == 3:
            try:
                parts[2] = str(int(parts[2]) + 1)
                new_version = ".".join(parts)
            except ValueError:
                new_version = "1.0.0"
        else:
            new_version = "1.0.0"

        sets.append("version=?")
        params.append(new_version)
        if updated_by:
            sets.append("updated_by=?"); params.append(updated_by)
        sets.append("updated_at=datetime('now')")
        params.append(market_id)

        try:
            c.execute(
                f"UPDATE scene_market SET {', '.join(sets)} WHERE id=? AND scope!='official'",
                params,
            )
            db.commit()
            return c.rowcount > 0
        except Exception as e:
            if "UNIQUE" in str(e).upper() or "DUPLICATE" in str(e).upper():
                raise ValueError(f"场景标识已存在")
            raise


def delete_scene_market(market_id: int) -> bool:
    """硬删除用户自定义场景（scope != 'official'）。
    场景市场无回收站需求，删除即销毁，name 立即释放。
    级联清理 scene_skill_ref 和 scene_likes 关联记录。
    """
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_skill_ref WHERE scene_id=?", (market_id,))
        c.execute("DELETE FROM scene_likes WHERE scene_id=?", (market_id,))
        c.execute(
            "DELETE FROM scene_market WHERE id=? AND scope!='official'",
            (market_id,),
        )
        db.commit()
        return c.rowcount > 0


def check_scene_name_exists(name: str, scope: str, repo_key: str = "",
                            source_user_id: int = 0, exclude_id: int = 0) -> bool:
    """检查场景标识是否重复。
    - 不能和 official 场景重名（所有 scope 都检查 official）
    - repo 场景：同仓库下 name 唯一，不同仓库可以同名
    """
    with db_lock:
        c = db.cursor()
        # 1. 先查 official scope（任何 scope 都不能和 official 重名）
        sql1 = "SELECT 1 FROM scene_market WHERE name=? AND scope='official' AND is_active=1"
        params1: list = [name]
        if exclude_id:
            sql1 += " AND id!=?"
            params1.append(exclude_id)
        sql1 += " LIMIT 1"
        c.execute(sql1, params1)
        if c.fetchone():
            return True
        # 2. 再查当前仓库（repo 按 repo_key 唯一）
        if scope == "official":
            return False  # official 已经查过了
        if not repo_key:
            return False
        sql2 = ("SELECT 1 FROM scene_market WHERE name=? AND repo_key=? AND is_active=1")
        params2: list = [name, repo_key]
        if exclude_id:
            sql2 += " AND id!=?"
            params2.append(exclude_id)
        sql2 += " LIMIT 1"
        c.execute(sql2, params2)
        return c.fetchone() is not None


# ====== Uploaded Files CRUD（Issue-54 图标上传）======

def insert_uploaded_file(file_key: str, original_name: str = "", file_size: int = 0,
                         mime_type: str = "", file_type: str = "scene_icon",
                         created_by: str = "") -> int:
    """插入临时上传文件记录（scope='temp'）。返回新记录 id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO uploaded_files "
            "(file_key, original_name, file_size, mime_type, file_type, scope, "
            "ref_table, ref_id, is_active, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?, 'temp', '', 0, 1, ?, datetime('now'),datetime('now'))",
            (file_key, original_name, file_size, mime_type, file_type, created_by),
        )
        db.commit()
        return c.lastrowid


def commit_uploaded_file(file_id: int, ref_table: str, ref_id: int) -> bool:
    """将临时文件标记为已提交（scope='committed'）。返回是否成功。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE uploaded_files SET scope='committed', ref_table=?, ref_id=?, "
            "updated_at=datetime('now') WHERE id=? AND scope='temp'",
            (ref_table, ref_id, file_id),
        )
        db.commit()
        return c.rowcount > 0


def get_uploaded_file(file_id: int) -> dict | None:
    """按 id 查上传文件记录。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, file_key, original_name, file_size, mime_type, file_type, "
            "scope, ref_table, ref_id, is_active, created_by, created_at "
            "FROM uploaded_files WHERE id=?",
            (file_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "file_key": row[1], "original_name": row[2] or "",
        "file_size": row[3], "mime_type": row[4] or "", "file_type": row[5] or "",
        "scope": row[6] or "temp", "ref_table": row[7] or "", "ref_id": row[8],
        "is_active": row[9], "created_by": row[10] or "",
        "created_at": str(row[11] or ""),
    }


def get_uploaded_files_by_ref(ref_table: str, ref_id: int) -> list[dict]:
    """按关联表+ID 查已提交的文件列表。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, file_key, original_name, file_size, mime_type, file_type, "
            "scope, ref_table, ref_id, is_active, created_by, created_at "
            "FROM uploaded_files WHERE ref_table=? AND ref_id=? AND is_active=1",
            (ref_table, ref_id),
        )
        rows = c.fetchall()
    return [
        {
            "id": r[0], "file_key": r[1], "original_name": r[2] or "",
            "file_size": r[3], "mime_type": r[4] or "", "file_type": r[5] or "",
            "scope": r[6] or "temp", "ref_table": r[7] or "", "ref_id": r[8],
            "is_active": r[9], "created_by": r[10] or "",
            "created_at": str(r[11] or ""),
        }
        for r in rows
    ]


def delete_uploaded_file(file_id: int) -> str:
    """软删除上传文件记录，返回 file_key（供调用方删物理文件）。空串=未找到。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT file_key FROM uploaded_files WHERE id=? AND is_active=1",
            (file_id,),
        )
        row = c.fetchone()
        if not row:
            return ""
        c.execute(
            "UPDATE uploaded_files SET is_active=0, updated_at=datetime('now') WHERE id=?",
            (file_id,),
        )
        db.commit()
        return row[0] or ""


def list_temp_uploaded_files_before(hours: int = 24) -> list[dict]:
    """查超过指定小时数的临时文件（供定时清理用）。"""
    from src.mysql_compat import get_db_backend
    is_mysql = get_db_backend() == "mysql"
    time_expr = f"DATE_SUB(NOW(), INTERVAL {int(hours)} HOUR)" if is_mysql else f"datetime('now', '-{int(hours)} hours')"
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, file_key, file_type FROM uploaded_files "
            "WHERE scope='temp' AND is_active=1 "
            f"AND created_at < {time_expr}",
        )
        rows = c.fetchall()
    return [{"id": r[0], "file_key": r[1], "file_type": r[2] or ""} for r in rows]


def hard_delete_uploaded_file(file_id: int) -> str:
    """硬删除上传文件记录（清理时用），返回 file_key。空串=未找到。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT file_key FROM uploaded_files WHERE id=?", (file_id,))
        row = c.fetchone()
        if not row:
            return ""
        c.execute("DELETE FROM uploaded_files WHERE id=?", (file_id,))
        db.commit()
        return row[0] or ""


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
    """精选技能列表：is_featured=1 的 official 技能。

    排序优先级：
    1. admin 管理台 admin_featured_config 表的 sort_order（与管理台设置一致）
    2. 降级：按 like_count 降序
    """
    from src.featured_order import get_featured_order

    ordered_ids = get_featured_order("skill")

    with db_lock:
        c = db.cursor()
        if ordered_ids:
            # 按 admin 排序返回——查全部精选技能后在 Python 中按 ordered_ids 排序
            placeholders = ",".join("?" * len(ordered_ids))
            c.execute(
                f"SELECT id, skill_name, display_name, description, agent_type, "
                f"is_featured, like_count, category_id "
                f"FROM skill WHERE is_active=1 AND is_featured=1 AND scope='official' "
                f"AND id IN ({placeholders})",
                ordered_ids
            )
            rows = {r[0]: r for r in c.fetchall()}
            result = []
            for item_id in ordered_ids:
                r = rows.get(item_id)
                if r:
                    result.append({"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
                                   "description": r[3] or "", "agent_type": r[4] or "",
                                   "is_featured": r[5], "like_count": r[6], "category_id": r[7] or 0})
            # admin 排序中不存在的精选技能（如新加的），追加到末尾
            if len(result) < limit:
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, "
                    "is_featured, like_count, category_id "
                    "FROM skill WHERE is_active=1 AND is_featured=1 AND scope='official' "
                    f"AND id NOT IN ({placeholders}) "
                    "ORDER BY like_count DESC",
                    ordered_ids
                )
                for r in c.fetchall():
                    result.append({"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
                                   "description": r[3] or "", "agent_type": r[4] or "",
                                   "is_featured": r[5], "like_count": r[6], "category_id": r[7] or 0})
            return result[:limit]
        else:
            # 降级：按 like_count 降序
            c.execute(
                "SELECT id, skill_name, display_name, description, agent_type, "
                "is_featured, like_count, category_id "
                "FROM skill WHERE is_active=1 AND is_featured=1 AND scope='official' "
                "ORDER BY like_count DESC LIMIT ?",
                (limit,)
            )
            return [{"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
                     "description": r[3] or "", "agent_type": r[4] or "",
                     "is_featured": r[5], "like_count": r[6], "category_id": r[7] or 0}
                    for r in c.fetchall()]

