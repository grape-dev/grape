"""grape_accounts + external_identities CRUD。

Grape 账号表（grape_accounts）只存账号自身字段，不含任何第三方平台字段。
第三方绑定关系存储在 external_identities 表。
"""

import time

from src.domains.mgmt.auth.business_data import BusinessDataProbe, legacy_table_tuples
from src.kernel.config import log


class _OwnedCursor:
    """自带连接的游标：close 时把连接一并关掉。"""

    def __init__(self, conn, cursor):
        self._conn = conn
        self._cur = cursor

    def __getattr__(self, item):
        return getattr(self._cur, item)

    def close(self):
        try:
            self._cur.close()
        finally:
            try:
                self._conn.close()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")


class _MgmtDbProxy:
    """把 _mg_db.cursor()/commit()/rollback() 代理到 mgmt 域连接。

    grape_accounts / api token 都在 mgmt 域。三库模式下 insight / runtime / event 进程
    也要校验会话（get_status_and_token_version → token_version 不匹配就被判 SESSION_REVOKED），
    这些进程主库不是 mgmt 库，必须走 GRAPE_DSN_MGMT。mgmt 进程自身仍用主库（零回归）。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.cursor(*a, **kw)
        if db_registry.uses_own_dsn("mgmt"):
            _conn = db_registry.new_connection("mgmt")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.commit()
        if db_registry.uses_own_dsn("mgmt"):
            return None
        from src.kernel.session_manager import db as _main
        return _main.commit()

    def rollback(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.rollback()
        if db_registry.uses_own_dsn("mgmt"):
            try:
                db_registry.domain_db("mgmt").rollback()
            except Exception as _e:
                log.debug(f"domain rollback failed: {_e}")
            return None
        from src.kernel.session_manager import db as _main
        return _main.rollback()

    def __getattr__(self, item):
        from src.kernel.session_manager import db as _main
        return getattr(_main, item)


_mg_db = _MgmtDbProxy()

from src.kernel.session_manager import db, db_lock

from src.domains.mgmt.auth.password import hash_password, verify_password, needs_upgrade

def _safe_decrypt(encrypted: str) -> str:
    """D46 P1-10: 安全解密包装——解密失败时返回空字符串而非崩溃。

    decrypt 抛异常说明密钥配置错误或数据损坏，返回空字符串让
    调用方知道凭据不可用，走重新绑定流程。
    """
    from src.domains.mgmt.auth.credential_crypto import decrypt
    try:
        return decrypt(encrypted)
    except (ValueError, RuntimeError) as e:
        log.warning(f"Safe decrypt failed: {e}")
        return ""

def _sync_um_platform_col(grape_user_id: str, provider: str, external_login: str):
    """S92/D68：um 平台归属列运行时停写（原 FR-18.6 补写语义退役，no-op）。

    依据：①读者全部身份优先（S51/S54，external_identities 权威）；
    ②session_manager 启动反向回填持续从身份域喂养 um 平台列（只填空，
    存量读者兜底不断粮）；③"配置即接入"要求新平台零 um 列。
    um 平台列自此退化为启动时快照；feishu/昵称列不受影响（另有运行时
    写路径，身份域无对应物）。调用方保留（解绑清空语义在 unbind 路由）。
    """
    return None


def create_grape_account(
    username: str,
    email: str,
    password: str,
    nickname: str = "",
    avatar_url: str = "",
    email_verified: bool = True,
) -> "dict | None":
    """创建 Grape 账号（邮箱验证码注册）。

    返回 {"id": int, "username": str, "email": str} 或 None（冲突）。
    D12: email 空字符串转为 NULL 存储。
    P0-4: 邮箱验证码注册的账号 email_verified=1（邮箱已通过验证码验证）。
    Issue #372 R7: 无邮箱配置时 email_verified=False（无邮箱验证码验证，不应标记已验证）。
    email_source: 'user' 表示用户自己绑定的真实邮箱。
    """
    email = (email or "").lower().strip() or None
    pw_hash = hash_password(password)
    uid = uuid7_str()                   # D105 第二批：主键应用层 UUIDv7（插入即所得）
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "INSERT INTO grape_accounts "
                "(id, username, email, email_source, password_hash, nickname, avatar_url, status, token_version, email_verified) "
                "VALUES (?,?,?, 'user',?,?,?,1,0,?)",
                (uid, username, email, pw_hash, nickname, avatar_url, 1 if email_verified else 0),
            )
            _mg_db.commit()
            log.info(f"Grape account created: {username} (id={uid})")
            return {"id": uid, "username": username, "email": email or ""}
    except Exception as e:
        log.warning(f"Create grape account failed: {e}")
        return None

def create_grape_account_oauth(
    username: str,
    email: str = "",
    nickname: str = "",
    avatar_url: str = "",
    email_verified: bool = False,
    provider: str = "",
) -> "dict | str | None":
    """创建无密码的 Grape 账号（OAuth 注册）。

    返回 {"id": int, "username": str, "email": str} 或 None（其他失败）。
    返回 "username_conflict" / "email_conflict" 区分 UNIQUE 冲突类型。
    D12: email 空字符串转为 NULL 存储。
    P0-4: email_verified 标记第三方邮箱是否已验证。
    email_source: 记录邮箱来源（gitcode/github/user），解绑时按来源清理 placeholder 邮箱。
    占位账号: 空邮箱时 email_is_placeholder=1，created_by_oauth=1。
    """
    raw_email = (email or "").lower().strip() or None
    # Issue #3054：provider 私有/中转邮箱（gitee noreply 等）不可收信，不能按真实邮箱
    # 落库（否则占 email 唯一键、把登录推进二选一页）。归一判定单一来源见 identity_email。
    from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy

    email_stored, is_placeholder = ProviderEmailPolicy.for_storage(raw_email)
    uid = uuid7_str()                   # D105 第二批：主键应用层 UUIDv7（插入即所得）
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "INSERT INTO grape_accounts "
                "(id, username, email, email_source, password_hash, nickname, avatar_url, "
                "status, token_version, email_verified, email_is_placeholder, created_by_oauth) "
                "VALUES (?,?,?,?, '',?,?,1,0,?,?,1)",
                (uid, username, email_stored, provider, nickname, avatar_url,
                 1 if email_verified else 0, is_placeholder),
            )
            _mg_db.commit()
            log.info(f"Grape account created (OAuth): {username} (id={uid}, placeholder={is_placeholder})")
            return {"id": uid, "username": username, "email": email_stored or ""}
    except Exception as e:
        err_str = str(e).lower()
        if "unique" in err_str and "username" in err_str:
            log.warning(f"Create grape account (OAuth) username conflict: {username}")
            return "username_conflict"
        if "unique" in err_str and "email" in err_str:
            log.warning(f"Create grape account (OAuth) email conflict: {email_stored}")
            return "email_conflict"
        log.warning(f"Create grape account (OAuth) failed: {e}")
        return None

def release_failed_signup_account(user_id: str) -> bool:
    """OAuth 建号后中途失败的回滚：释放 email / username，不进冷静期占坑。

    与 `soft_delete_account`（用户主动注销：冷静期 30 天保留 email/username 以便恢复）
    语义不同：这类账号从未被用户使用，若按冷静期保留，同一邮箱/用户名在 30 天内对
    所有人不可用，且同一第三方账号重登仍撞唯一键、再次落回「注册新账号 / 关联已有
    账号」二选一页（Issue #3054 验收缺口）。

    做法（同一事务）：
    - grape_accounts：status=3、email=NULL、email_is_placeholder=1、
      username 追加 `_void_<id 前 8 位>` 后缀（释放用户名唯一键，同时留可审计痕迹）；
    - external_identities：该用户所有身份行置 status=0 并清 token（与注销级联同口径）。
    """
    import time as _time

    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute("SELECT username FROM grape_accounts WHERE id=? AND status=1", (user_id,))
            row = c.fetchone()
            if not row:
                _mg_db.commit()
                return False
            suffix = "_void_" + str(user_id)[:8]
            void_name = (f"{row[0] or 'user'}{suffix}")[:64]
            c.execute(
                "UPDATE grape_accounts SET status=3, email=NULL, email_is_placeholder=1, "
                "username=?, updated_at=? WHERE id=? AND status=1",
                (void_name, now, user_id),
            )
            if c.rowcount <= 0:
                _mg_db.commit()
                return False
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', "
                "refresh_token='', token_expires_at='', updated_at=? "
                "WHERE grape_user_id=? AND status IN (1,4)",
                (now, user_id),
            )
            _mg_db.commit()
            log.info(f"failed signup rolled back: id={user_id} username→{void_name} (email released)")
            return True
    except Exception as e:
        log.warning(f"release_failed_signup_account failed: {e}")
        return False

def get_grape_account_by_id(user_id: str) -> "dict | None":
    """按 ID 查询 Grape 账号。"""
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT id, username, email, password_hash, nickname, avatar_url, "
                "status, token_version, created_at, updated_at, last_login_at, email_verified, email_source, "
                "email_is_placeholder, created_by_oauth "
                "FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get grape account failed: {e}")
        return None
    if not row:
        return None
    return _row_to_account(row)

def get_grape_account_by_username(username: str) -> "dict | None":
    """按用户名查询 Grape 账号。"""
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT id, username, email, password_hash, nickname, avatar_url, "
                "status, token_version, created_at, updated_at, last_login_at, email_verified, email_source, "
                "email_is_placeholder, created_by_oauth "
                "FROM grape_accounts WHERE LOWER(username)=LOWER(?)",
                (username,),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get grape account by username failed: {e}")
        return None
    if not row:
        return None
    return _row_to_account(row)

def get_grape_account_by_email(email: str) -> "dict | None":
    """按邮箱查询 Grape 账号。

    BUG-4 修复：注释更正——不过滤 status，调用方根据 status 判断。
    status 值：0=禁用，1=活跃，2=已注销（硬删除保留记录），3=冷静期（软删除30天内可恢复），4=merged（占位账号合并后标记）。token 过期仅存在于 external_identities 表（status=4）。
    """
    email = email.lower().strip()
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT id, username, email, password_hash, nickname, avatar_url, "
                "status, token_version, created_at, updated_at, last_login_at, email_verified, email_source, "
                "email_is_placeholder, created_by_oauth "
                "FROM grape_accounts WHERE email=?",
                (email,),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get grape account by email failed: {e}")
        return None
    if not row:
        return None
    return _row_to_account(row)

def get_grape_account_by_identifier(identifier: str) -> "dict | None":
    """按标识符查询（自动识别邮箱或用户名）。"""
    if "@" in identifier:
        return get_grape_account_by_email(identifier)
    return get_grape_account_by_username(identifier)

def _row_to_account(row) -> dict:
    return {
        "id": row[0], "username": row[1], "email": row[2] or "",
        "password_hash": row[3], "nickname": row[4], "avatar_url": row[5],
        "status": row[6], "token_version": row[7],
        "created_at": row[8], "updated_at": row[9],
        "last_login_at": row[10] if len(row) > 10 else None,
        "email_verified": row[11] if len(row) > 11 else 0,
        "email_source": row[12] if len(row) > 12 else None,
        "email_is_placeholder": row[13] if len(row) > 13 else 0,
        "created_by_oauth": row[14] if len(row) > 14 else 0,
    }

def verify_grape_account(identifier: str, password: str) -> "dict | None":
    """验证 Grape 账号密码。

    成功返回用户信息（含 token_version），失败返回 None。
    旧密码（SHA-256）验证通过后自动升级为 bcrypt。

    注意：status != 1 的账号（禁用/注销/冷静期）密码验证仍会执行，
    如果密码正确，返回的 dict 中会带 status 字段，调用方可据此区分
    "密码错误" vs "账号被禁用"。
    """
    account = get_grape_account_by_identifier(identifier)
    if not account:
        return None
    if not verify_password(password, account["password_hash"]):
        return None
    # 透明升级旧密码
    if needs_upgrade(account["password_hash"]):
        _upgrade_password_hash(account["id"], password)
    return account

def _upgrade_password_hash(user_id: str, plain: str) -> None:
    """升级密码哈希为 bcrypt。"""
    new_hash = hash_password(plain)
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET password_hash=? WHERE id=?",
                (new_hash, user_id),
            )
            _mg_db.commit()
        log.info(f"Password hash upgraded for user {user_id}")
    except Exception as e:
        log.warning(f"Upgrade password hash failed: {e}")

def update_password(user_id: str, new_password: str) -> bool:
    """更新密码。"""
    new_hash = hash_password(new_password)
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET password_hash=? WHERE id=?",
                (new_hash, user_id),
            )
            _mg_db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update password failed: {e}")
        return False

def set_password(user_id: str, new_password: str) -> bool:
    """设置密码（无密码用户设置密码，无旧密码校验）。"""
    return update_password(user_id, new_password)

def update_email(user_id: str, new_email: str) -> bool | str:
    """更新邮箱。D12: 空字符串转为 NULL 存储。

    D46 P1-1: 区分 UNIQUE 冲突，返回 "conflict" 让路由层报 409。
    返回值：True=成功，False=其他失败，"conflict"=邮箱已被占用。
    """
    new_email = (new_email or "").lower().strip() or None
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET email=? WHERE id=?",
                (new_email, user_id),
            )
            _mg_db.commit()
            return c.rowcount > 0
    except Exception as e:
        # D46 P1-1: 检测 UNIQUE 约束冲突
        err_msg = str(e).lower()
        if "unique" in err_msg or "duplicate" in err_msg or "1062" in err_msg:
            log.warning(f"Update email conflict (already taken): user_id={user_id}")
            return "conflict"
        log.warning(f"Update email failed: {e}")
        return False

def update_last_login_at(user_id: str) -> None:
    """D11: 更新最近登录时间。"""
    # D46 P1-14: 用 Python 时间戳替代 datetime('now')，兼容 MySQL
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET last_login_at=? WHERE id=?",
                (now, user_id),
            )
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Update last_login_at failed: {e}")

def update_profile(user_id: str, nickname: "str | None" = None, avatar_url: "str | None" = None) -> bool:
    """更新昵称/头像。"""
    updates = []
    params = []
    if nickname is not None:
        updates.append("nickname=?")
        params.append(nickname)
    if avatar_url is not None:
        updates.append("avatar_url=?")
        params.append(avatar_url)
    if not updates:
        return True
    params.append(user_id)
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                f"UPDATE grape_accounts SET {', '.join(updates)} WHERE id=?",
                params,
            )
            _mg_db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update profile failed: {e}")
        return False

def get_token_version(user_id: str) -> int:
    """获取用户的 token_version。"""
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT token_version FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
        if not row:
            return -1
        return row[0] if row[0] is not None else 0
    except Exception:
        return -1

def increment_token_version(user_id: str) -> None:
    """token_version + 1（改密码/登出时调用）。"""
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET token_version=token_version+1 WHERE id=?",
                (user_id,),
            )
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Increment token version failed: {e}")

def get_status_and_token_version(user_id: str) -> "tuple[int, int, str] | None":
    """获取用户状态、token_version 和 username（鉴权中间件用）。

    Issue #343: 返回 username 用于 JWT payload 一致性校验，
    防止旧 JWT sub=旧id 撞上新表有效用户后拿别人 id 写数据。

    返回 (status, token_version, username) 或 None。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT status, token_version, username FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
        if not row:
            return None
        return (row[0], row[1] if row[1] is not None else 0, row[2] or "")
    except Exception:
        return None

def is_placeholder_email(email: str, email_source: str = "") -> bool:
    """检查是否为 OAuth 提供商生成的 placeholder 邮箱。

    Issue #3054：判定单一来源迁到 `auth/identity_email.ProviderEmailPolicy`（域名后缀
    匹配，覆盖 gitee/gitcode/github/gitlab/apple 私有中转邮箱）。本函数保留为兼容别名，
    新增域名只需改那一个模块。
    """
    from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy

    return ProviderEmailPolicy.is_private(email)

def is_email_registered(email: str) -> bool:
    """检查邮箱是否已注册（激活状态或冷静期）。

    placeholder 邮箱（noreply.gitcode.com / noreply.github.com）不参与冲突检查——
    它们是 OAuth 提供商生成的占位邮箱，不是用户真实邮箱。

    BUG-3 修复：status=3（冷静期）邮箱仍占唯一索引，
    需要参与冲突检查，否则 register 时 create_grape_account 因唯一约束失败返回 None，
    最终导致 500 而非业务提示。
    """
    email = email.lower().strip()
    if is_placeholder_email(email):
        return False
    try:
        with db_lock:
            c = _mg_db.cursor()
            # 排除占位账号（email_is_placeholder=1），占位邮箱不参与冲突检查
            c.execute(
                "SELECT 1 FROM grape_accounts WHERE email=? AND status IN (1, 3) "
                "AND (email_is_placeholder=0 OR email_is_placeholder IS NULL) LIMIT 1",
                (email,),
            )
            return c.fetchone() is not None
    except Exception:
        return False

def is_username_taken(username: str) -> bool:
    """检查用户名是否已被占用。

    BUG-2 说明：不过滤 status——软删除账号(status=3)仍占用用户名。
    设计理由：
    1. 冷静期内防止他人注册同名账号，恢复后原用户名回归
    2. 匿名化时 username 改为 deleted_{id}，原用户名自动释放
    3. 与 is_email_taken(只查 status=1) 不同——email 可为空且匿名化时置 NULL，
       用户名是唯一标识必须全程独占
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                # P2-2: 大小写不敏感查询，与 _validate_username 的 .lower() 保留字检查一致
                "SELECT 1 FROM grape_accounts WHERE LOWER(username)=LOWER(?) LIMIT 1",
                (username,),
            )
            return c.fetchone() is not None
    except Exception:
        return False

def soft_delete_account(user_id: str) -> bool:
    """软删除账号（标记为冷静期 status=3）+ 级联失效第三方身份行。

    P3-3（状态机/生命周期）：原实现只改 grape_accounts.status，身份行仍是
    status=1 且保留 token——注销后 OAuth 再登录会命中「已绑定」分支，只报
    「账号已禁用」，与历史绑定分支的 deleted/cooldown 文案不一致，且凭据在
    冷静期内仍可被取回。现在同一事务内把该用户 external_identities 置 status=0
    并清空 access_token/refresh_token/token_expires_at。
    """
    # D46 P1-14: 用 Python 时间戳替代 datetime('now')，兼容 MySQL
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET status=3, updated_at=? "
                "WHERE id=? AND status=1",
                (now, user_id),
            )
            if c.rowcount <= 0:
                _mg_db.commit()
                return False
            # 级联：注销账号不再持有任何可登录的第三方身份 / 凭据，**并释放**
            # UNIQUE(provider, external_id)（旧值进墓碑；Issue #3054 遗留缺口）。
            # 判定/SQL 在 identity_lifecycle.release_on_deactivation（状态机单一来源）。
            from src.domains.mgmt.auth.identity_lifecycle import IdentityLifecycleService

            IdentityLifecycleService().release_on_deactivation(c, user_id, now)
            _mg_db.commit()
            return True
    except Exception as e:
        log.warning(f"Soft delete account failed: {e}")
        return False

# Issue #3054 P2：身份行状态机（status=4 恢复/解绑释放/换绑墓碑/一次性票据）
# 收进 identity_lifecycle.IdentityLifecycleService，本文件只保留薄转发——
# 避免状态判断与 SQL 散落到调用方（路由/账号模块）。
# 服务实例在文件底部 import 段绑定（避免模块级循环导入）。


def get_external_identity(provider: str, external_id: str,
                          statuses: "tuple[int, ...]" = (1,)) -> "dict | None":
    """按平台 ID 查询绑定关系（token 已解密）。D22: 默认只查 status=1。

    Issue #3054 P2-1：登录决策链传 statuses=(1, 4)——status=4 是「绑定仍在、token
    过期」，必须仍被认作已绑定（否则重登会走建号/绑定失败 → status=3 孤儿账号）。
    其它调用方（绑定冲突判定等）保持默认 (1,)，语义不变。
    """
    return _identity_lifecycle.get(provider, external_id, statuses=statuses)


def get_external_identity_any_status(provider: str, external_id: str) -> "dict | None":
    """按平台 ID 查询绑定关系（含解绑 status=0 / 过期 status=4）。

    BUG-6 修复：用于 oauth_associate/oauth_register 检查是否存在历史记录。
    历史记录属于其他用户时，不允许静默恢复绑定（合法路径是票据/转移）。
    """
    return _identity_lifecycle.get_any_status(provider, external_id)


def find_identity_by_tombstone(provider: str, external_id: str) -> "dict | None":
    """按墓碑（曾绑定过的旧 external_id）反查归属行（Issue #3054 P2-2）。"""
    return _identity_lifecycle.find_tombstone(provider, external_id)


def get_user_identities(grape_user_id: str) -> list[dict]:
    """获取用户的所有第三方活跃绑定（不含 access_token，防止 token 泄露到前端）。
    D22: 只查 status=1 的活跃绑定。
    Issue #372 R10: 也返回 status=4（token 过期）的绑定，前端显示过期提示。
    """
    try:
        with _uow() as session:
            ds = _iar.list_by_user_statuses(session, grape_user_id, statuses=(1, 4))
        return [{"provider": d.provider, "external_login": d.external_login,
                 "external_name": d.external_name, "external_avatar": d.external_avatar,
                 "created_at": d.created_at, "status": d.status,
                 # D102-P v6 + D105k 解耦收口：ei.access_token 只承载 OAuth
                 # token——历史 PAT 已由 backfill_pat_identity_tokens 搬入凭据域
                 # <p>_pat，专用字段路径已改写不再触碰 ei。非空即 OAuth。
                 "has_oauth_token": bool(getattr(d, "access_token", ""))} for d in ds]
    except Exception as e:
        log.warning(f"Get user identities failed: {e}")
        return []

def list_platform_links(grape_user_id: str) -> list[dict]:
    """用户在各平台的标识统一视图 = 身份行 ∪ 凭据域 `<p>_pat`（A 批裁定）。

    对象模型（2026-11-13 用户拍板）：PAT 是「仓库执行凭据」，不是身份绑定——
    身份行（external_identities）只由首次 OAuth 登录或设置页 OAuth 绑定产生；
    PAT 只落凭据域 user_credentials `<p>_pat`（带 login）。但很多调用方真正要
    问的是「这个用户在平台 X 上是谁」（建单作者名 / host 用户名 / 仓库列表的
    平台集合 / 注入给 LLM 的登录名清单），这些必须看两者并集，否则纯 PAT 账号
    在所有身份行消费点上都表现为"未绑定"。

    返回条目与 get_user_identities 字段对齐（provider/external_login/
    external_name/external_avatar/created_at/status/has_oauth_token），并追加：
      source        "identity" | "pat"
      has_pat_token 该平台是否同时有可用 PAT 凭据
    同 provider 两者并存时以身份行为准（归属来自 OAuth），PAT 只补标记与
    login（身份行缺 login 时）。
    """
    out: list[dict] = []
    seen: set = set()
    # token 可用性（2026-11-16 实锤）：身份行存在不等于凭据可用——历史 `derived:` 派生
    # 密钥在当前密钥下解不出来时，行还在、token 为空，调用方若只看"有没有身份行"就会
    # 去拉仓并必然 401（yangaiwu 现场：关系/身份都在，页面却提示授权过期）。
    _tokens: dict = {}
    try:
        for _t in get_user_identities_with_tokens(grape_user_id):
            _tokens[str(_t.get("provider") or "")] = _t
    except Exception as e:
        log.warning(f"list_platform_links: token scan failed: {e}")
    try:
        for ident in get_user_identities(grape_user_id):
            provider = str(ident.get("provider") or "")
            if not provider:
                continue
            item = dict(ident)
            item["source"] = "identity"
            item["has_pat_token"] = False
            _tok = _tokens.get(provider) or {}
            item["token_usable"] = bool(str(_tok.get("access_token") or "").strip()
                                        or str(_tok.get("refresh_token") or "").strip())
            out.append(item)
            seen.add(provider)
    except Exception as e:
        log.warning(f"list_platform_links: identity scan failed: {e}")

    try:
        from src.domains.mgmt.user.db import get_credentials
        for cred in get_credentials(grape_user_id):
            provider = str(cred.get("provider") or "")
            kind = str(cred.get("kind") or "")
            if kind:
                # A2 归位形态：类型走 kind 列
                if kind != "pat":
                    continue
                platform = provider
            elif provider.endswith("_pat"):
                # 迁移前/双读回退形态：旧复合键 <p>_pat
                platform = provider[:-len("_pat")]
            else:
                continue
            cfg = cred.get("config") or {}
            token = str(cfg.get("token") or "").strip() if isinstance(cfg, dict) else ""
            if platform in seen:
                if token:
                    for item in out:
                        if item.get("provider") == platform:
                            item["has_pat_token"] = True
                            item["token_usable"] = True
                            if not item.get("external_login"):
                                item["external_login"] = str(cfg.get("login") or "")
                continue
            if not token:
                continue
            out.append({
                "provider": platform,
                "external_id": "",
                "external_login": str(cfg.get("login") or "") if isinstance(cfg, dict) else "",
                "external_name": "",
                "external_avatar": "",
                "created_at": "",
                "status": 1,            # 可用凭据即视为该平台可用
                "has_oauth_token": False,
                "source": "pat",
                "has_pat_token": True,
                "token_usable": True,
            })
            seen.add(platform)
    except Exception as e:
        log.warning(f"list_platform_links: credential scan failed: {e}")
    return out


def get_user_identities_with_tokens(grape_user_id: str) -> list[dict]:
    """获取用户的所有第三方活跃绑定（含 access_token/refresh_token）。

    仅供后端内部使用（get_credentials 等），不暴露到前端。
    与 get_user_identities 的区别：此函数查询并解密 access_token/refresh_token。

    Issue #372 §19: 查询条件与 get_user_identities 对齐，status IN (1, 4)——
    status=4（token 过期）的绑定也返回，调用方使用 token 时 API 会返回 401，
    由 R10 fail_task 检测逻辑处理，不影响安全。
    """
    try:
        with _uow() as session:
            ds = _iar.list_by_user_statuses(session, grape_user_id, statuses=(1, 4))
        return [{"provider": d.provider, "external_login": d.external_login,
                 "external_name": d.external_name, "external_avatar": d.external_avatar,
                 "access_token": _safe_decrypt(d.access_token or ""),
                 "refresh_token": _safe_decrypt(d.refresh_token or ""),
                 "created_at": d.created_at, "status": d.status} for d in ds]
    except Exception as e:
        log.warning(f"Get user identities with tokens failed: {e}")
        return []

def _has_refresh_token(user_id: str, provider: str) -> bool:
    """Issue #372 R2: 判断用户在指定平台是否有 refresh_token。

    从 external_identities 读 refresh_token（解密后），非空返回 True。
    用于 get_user_token_with_refresh 跳过无 refresh_token 的凭据（如 PAT/导入 token）——
    这类 token 不会过期，无需刷新。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT refresh_token FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status IN (1, 4) LIMIT 1",
                (user_id, provider),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"_has_refresh_token: query failed: {e}")
        return False
    if not row:
        return False
    rt = _safe_decrypt(row[0] or "")
    return bool(rt)

def create_external_identity(
    grape_user_id: str,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
) -> bool:
    """创建/恢复/换绑第三方绑定（token 加密存储）。

    BUG-6 + Issue #3054 P2：一人一平台一行（UNIQUE(grape_user_id, provider)），
    换绑/解绑恢复/过期后重登统一由 IdentityLifecycleService.bind 复用本行，
    旧 external_id 进 metadata 墓碑。
    """
    return _identity_lifecycle.bind(
        grape_user_id=grape_user_id, provider=provider, external_id=external_id,
        external_login=external_login, external_name=external_name,
        external_avatar=external_avatar, access_token=access_token,
        refresh_token=refresh_token, expires_in=expires_in)


def delete_external_identity(grape_user_id: str, provider: str,
                             require_keep_one: bool = True) -> bool:
    """软删绑定（status=0，清 token）；status=4 可解绑并释放 UNIQUE 键。

    Issue #3054 P2-1：status=4 与 status=1 同等可解绑；status=4 解绑额外
    记墓碑 + 换行级占位 external_id（释放 UNIQUE(provider, external_id)）。
    require_keep_one: 删除后必须仍有一种登录方式（密码或另一条可用绑定）。

    Issue #3054 P3-1：「至少保留一种登录方式」的判据在
    `IdentityLifecycleService.unbind` 的单条条件 UPDATE 内求值（并发解绑由数据库
    语句原子性裁决 ≥ 一端失败），此处只做参数整形与委托。
    """
    return _identity_lifecycle.unbind(
        grape_user_id, provider, require_keep_one=require_keep_one,
        user_has_password=has_password(grape_user_id))

def is_placeholder_account_mergeable(grape_user_id: str) -> bool:
    """检查占位账号是否可安全自动转移绑定。

    条件（全部满足才可自动转移）：
    1. email_is_placeholder=1 + created_by_oauth=1（系统自动创建的占位账号）
    2. 无密码（password_hash 为空）
    3. 无其他活跃 OAuth 绑定（只有当前要转移的这一条）
    4. 账号 status=1（活跃）
    5. 无业务数据（未托管仓库、未创建任务/对话/issue 等）

    如果用户后来设了密码、绑定了其他平台、或产生了业务数据，
    说明用户真正使用过这个账号，不能自动转移，需走转移确认流程。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT email_is_placeholder, created_by_oauth, password_hash, status "
                "FROM grape_accounts WHERE id=?",
                (grape_user_id,),
            )
            row = c.fetchone()
            if not row:
                return False
            if not (row[0] and row[1]):
                return False
            if row[2]:
                return False
            if row[3] != 1:
                return False
            c.execute(
                "SELECT COUNT(*) FROM external_identities "
                "WHERE grape_user_id=? AND status=1",
                (grape_user_id,),
            )
            ident_count = c.fetchone()[0]
            if ident_count != 1:
                return False
            # 业务数据检查：有业务数据的占位账号不能自动转移
            return not _has_business_data(grape_user_id, c)
    except Exception as e:
        log.warning(f"is_placeholder_account_mergeable failed: {e}")
        return False

# 业务表清单与判定逻辑见 `auth/business_data.py`（Issue #3097：跨域表走 reader，
# 缺表/不可读 = 视为无数据 + 结构化 warn）。此处保留旧三元组结构供
# `transfer_business_data`（数据转移写路径）迭代，单一来源仍是那个模块。
_BUSINESS_TABLES = legacy_table_tuples()


def _has_business_data(grape_user_id, cursor=None) -> bool:
    """占位账号「是否有业务数据」判定（薄接线，零判定逻辑）。

    判定、读通道（本域表用调用方游标；runtime 表走 runtime public 具名读原语）与
    缺表语义全在 `auth/business_data.py::BusinessDataProbe`：缺表/不可读 = 视为无数据
    + 结构化 warn（不再「保守判有数据」，否则三库拓扑下占位账号自动合并永久失效）。
    """
    return BusinessDataProbe(cursor=cursor).has_business_data(grape_user_id)


def transfer_business_data(from_user_id: str, to_user_id: str) -> bool:
    """将一个用户的全部业务数据转移到另一个用户。在调用方的 db_lock + 事务内执行。

    转移策略：UPDATE user_id = to_user_id（不删除原数据，只改归属）。
    如果 to_user_id 已有同主键数据（如 user_repos 同 repo_key），用 INSERT OR REPLACE / IGNORE 处理。

    返回 True 表示全部成功。
    """
    c = _mg_db.cursor()
    for table, col, _ in _BUSINESS_TABLES:
        try:
            # user_repos 有复合主键 (user_id, repo_key)，目标用户可能已有同 repo_key 记录
            # 用 UPDATE ... WHERE 条件转移，冲突时跳过（INSERT OR IGNORE 补充）
            if table == "user_repos":
                # 先 UPDATE 已有记录的归属（目标用户无同 repo_key 的记录）
                # ⚠️ 子查询用派生表包裹（Issue #376）：MySQL 1093 禁止 UPDATE 目标表
                # 直接出现在子查询 FROM 中；包裹成 (SELECT ... FROM (SELECT ...) t)
                # 后目标表仅出现在派生表内部，SQLite/MySQL 双兼容。
                c.execute(
                    f"UPDATE {table} SET {col}=? "
                    f"WHERE {col}=? AND repo_key NOT IN "
                    f"(SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)",
                    (to_user_id, from_user_id, to_user_id),
                )
                # 目标用户已有同 repo_key 的记录 → 删除源用户的老记录（避免重复）
                # ⚠️ 同上：派生表包裹消除同表子查询（MySQL 1093）。
                c.execute(
                    f"DELETE FROM {table} WHERE {col}=? AND repo_key IN "
                    f"(SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)",
                    (from_user_id, to_user_id),
                )
            else:
                c.execute(
                    f"UPDATE {table} SET {col}=? WHERE {col}=?",
                    (to_user_id, from_user_id),
                )
        except Exception as e:
            log.warning(f"transfer_business_data: table {table} failed: {e}")
            # 不 return False——继续转移其他表，最终返回 False 让调用方决定是否回滚
            return False
    # runtime 归属表（agent_tasks / 会话 / 对话 / issue）的转移走契约：
    # 表名与 UPDATE 语句不出域（Issue #3097 三库四进程）；失败返回 False 让调用方回滚。
    try:
        from src.kernel.provider import get_runtime_service
        get_runtime_service().transfer_user_business_data(from_user_id, to_user_id)
    except Exception as e:
        log.warning(f"transfer_business_data: runtime business rows transfer failed: {e}")
        return False
    return True

def transfer_external_identity_in_tx(cursor, new_user_id: str, provider: str,
                                      external_id: str, external_login: str = "",
                                      external_name: str = "", external_avatar: str = "",
                                      access_token: str = "", original_user_id: str = "") -> bool:
    """在调用方事务内转移绑定关系（不加锁，不 commit）。

    与 transfer_external_identity 逻辑相同，但由调用方持有 db_lock 和事务。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        c = cursor
        # 1. 旧绑定标记为 transferred（status=0, 清 token）
        # Issue #3054 P2：status=4（token 过期）同样是「已有绑定」，转移后不得继续
        # 占用 UNIQUE(provider, external_id)——owner 已放弃却占键 = 转移必失败。
        if original_user_id:
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status IN (1, 4)",
                (now, original_user_id, provider),
            )
        # 检查并处理目标用户已有的同 provider 活跃绑定（status=1/4）
        # 用条件 UPDATE 代替 SELECT+UPDATE，避免 MySQL auto-commit 架空事务
        c.execute(
            "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
            "unbound_at=? WHERE grape_user_id=? AND provider=? AND status IN (1, 4)",
            (now, new_user_id, provider),
        )
        # 清理同 provider+external_id 的旧记录（status 0/4），防止 UNIQUE 冲突
        c.execute(
            "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status IN (0, 4)",
            (provider, external_id),
        )
        # 清理目标用户已有的同 provider 旧记录（status 0/4），防止 UNIQUE(grape_user_id, provider) 冲突
        c.execute(
            "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status IN (0, 4)",
            (new_user_id, provider),
        )
        # 2. 创建新活跃绑定
        c.execute(
            "INSERT INTO external_identities "
            "(id, grape_user_id, provider, external_id, external_login, "
            "external_name, external_avatar, access_token, refresh_token) "
            "VALUES (?, ?,?,?,?,?,?,?,?)",
            (uuid7_str(), new_user_id, provider, external_id, external_login,
             external_name, external_avatar,
             encrypt(access_token) if access_token else "",
             ""),
        )
        return True
    except Exception as e:
        log.warning(f"transfer_external_identity_in_tx failed: {e}")
        return False

def merge_placeholder_account(placeholder_user_id: str, target_user_id: str,
                               provider: str, external_id: str) -> bool:
    """将占位账号的绑定转移到目标账号，原账号标记为 merged（status=4）。

    ⚠️ 已废弃：请使用 unbind_placeholder_binding + restore_binding_in_tx +
    mark_account_merged 在调用方事务内组合调用。
    此函数保留作为兼容包装（内部 commit，不适合事务组合）。
    调用时会发出 DeprecationWarning。

    前提：调用方已通过 is_placeholder_account_mergeable 检查。
    转移后原账号 status=4（merged），不再能登录，不再参与冲突检查。
    """
    import warnings
    warnings.warn(
        "merge_placeholder_account is deprecated, use unbind_placeholder_binding + "
        "restore_binding_in_tx + mark_account_merged instead",
        DeprecationWarning,
        stacklevel=2,
    )
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
                (now, placeholder_user_id, provider),
            )
            # P1-4: 释放用户名——merged 账号 username 改为 merged_{id}，原用户名可供注册
            # 需求 7：同时清空邮箱字段
            c.execute(
                "UPDATE grape_accounts SET status=4, username=?, "
                "email=NULL, email_source=NULL, email_is_placeholder=0, "
                "updated_at=? WHERE id=?",
                (f"merged_{placeholder_user_id}", now, placeholder_user_id),
            )
            _mg_db.commit()
            log.info(f"Placeholder account {placeholder_user_id} merged to {target_user_id} "
                     f"(provider={provider}, external_id={external_id})")
            return True
    except Exception as e:
        log.warning(f"merge_placeholder_account failed: {e}")
        return False

def unbind_placeholder_binding(cursor, placeholder_user_id: str, provider: str) -> bool:
    """需求 6 Step 1：unbind 占位账号的旧绑定（在调用方事务内执行，不加锁不 commit）。

    将占位账号的 external_identities 记录标记为 status=0（软删除），
    清空 access_token/refresh_token。
    之后 restore_binding_in_tx 会用条件 UPDATE 恢复，避免 UNIQUE 冲突。

    ⚠️ MySQL auto-commit 陷阱：此函数和后续 restore_binding_in_tx 只做写操作，
    不含 SELECT，在 MySQL 下事务不会被中途 auto-commit 架空。
    """
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
            "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
            (now, placeholder_user_id, provider),
        )
        return True
    except Exception as e:
        log.warning(f"unbind_placeholder_binding failed: {e}")
        return False

def restore_binding_in_tx(
    cursor, old_user_id: str, new_user_id: str, provider: str, external_id: str,
    external_login: str = "", external_name: str = "", external_avatar: str = "",
    access_token: str = "", refresh_token: str = "",
    expires_in: int = 0,
) -> bool:
    """需求 6 Step 2（MySQL 安全版）：在调用方事务内恢复绑定（只写不读）。

    用条件 UPDATE + rowcount 代替 SELECT+UPDATE，避免 MySQL auto-commit 架空事务。
    unbind_placeholder_binding 刚把旧绑定标记为 status=0，此处直接 UPDATE 恢复：
    - status=0 → status=1
    - grape_user_id 改为 new_user_id
    - 更新 token/用户信息
    如果 rowcount=0（status=0 记录不存在），走清理+INSERT。

    ⚠️ 此函数不含任何 SELECT，在 MySQL 下事务不会被 auto-commit 架空。
    Issue #343 P1-4 修复: 新增 expires_in 参数，恢复绑定时写入 token_expires_at。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    _expires_at_str = ""
    if expires_in:
        _expires_at_str = _t.strftime("%Y-%m-%d %H:%M:%S",
                                       _t.gmtime(_t.time() + expires_in))
    try:
        c = cursor
        # 尝试恢复 status=0 记录（unbind_placeholder_binding 刚写入的）
        c.execute(
            "UPDATE external_identities SET "
            "grape_user_id=?, status=1, external_id=?, external_login=?, "
            "external_name=?, external_avatar=?, access_token=?, refresh_token=?, "
            "token_expires_at=?, unbound_at=NULL, updated_at=? "
            "WHERE grape_user_id=? AND provider=? AND status=0",
            (new_user_id, external_id, external_login, external_name,
             external_avatar, encrypt(access_token), encrypt(refresh_token),
             _expires_at_str, now, old_user_id, provider),
        )
        if c.rowcount > 0:
            return True
        # 无 status=0 记录，清理后 INSERT
        c.execute(
            "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0",
            (new_user_id, provider),
        )
        c.execute(
            "INSERT INTO external_identities "
            "(id, grape_user_id, provider, external_id, external_login, "
            "external_name, external_avatar, access_token, refresh_token, token_expires_at) "
            "VALUES (?, ?,?,?,?,?,?,?,?,?)",
            (uuid7_str(), new_user_id, provider, external_id, external_login,
             external_name, external_avatar,
             encrypt(access_token), encrypt(refresh_token), _expires_at_str),
        )
        return True
    except Exception as e:
        log.warning(f"restore_binding_in_tx failed: {e}")
        return False

def mark_account_merged(cursor, placeholder_user_id: str) -> bool:
    """需求 6 Step 3：标记占位账号 status=4 + 清邮箱（在调用方事务内执行，不加锁不 commit）。

    需求 7：同时清空 email/email_source/email_is_placeholder，释放邮箱唯一索引。
    """
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "UPDATE grape_accounts SET status=4, username=?, "
            "email=NULL, email_source=NULL, email_is_placeholder=0, "
            "updated_at=? WHERE id=?",
            (f"merged_{placeholder_user_id}", now, placeholder_user_id),
        )
        return True
    except Exception as e:
        log.warning(f"mark_account_merged failed: {e}")
        return False

def clear_placeholder_email(user_id: str, provider: str) -> bool:
    """需求 10：清理 placeholder 邮箱（解绑时调用）。

    如果 email 是 placeholder 且 email_source == provider → 清空。
    否则不动，返回 True（非 placeholder 邮箱不需要清理）。

    通过数据访问层统一操作，不绕过 grape_account_db 直接写 SQL。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT email, email_source, email_is_placeholder FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
            if not row:
                return True
            email, email_source, is_placeholder = row
            if email and is_placeholder and email_source == provider:
                import time as _t
                now = _t.strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE grape_accounts SET email=NULL, email_source=NULL, "
                    "email_is_placeholder=0, updated_at=? WHERE id=?",
                    (now, user_id),
                )
                _mg_db.commit()
                log.info(f"Cleared placeholder email for user={user_id}, provider={provider}")
            return True
    except Exception as e:
        log.warning(f"clear_placeholder_email failed: {e}")
        return False

def count_user_identities(grape_user_id: str, statuses: tuple = (1,)) -> int:
    """统计用户的第三方绑定数量。

    D22 默认只计 status=1（活跃）。Issue #3054 QA：status=4 是「绑定仍在、token 过期」，
    重新授权即可恢复，同样构成一种可用登录方式——「至少保留一种登录方式」的判据
    必须传 statuses=(1, 4)，否则持有 (1) + (4) 的无密码用户会被前置闸 400 挡住，
    服务层那条原子解绑判据根本走不到。
    """
    try:
        wanted = tuple(int(s) for s in (statuses or (1,)))
        ph = ",".join("?" for _ in wanted)
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                f"SELECT COUNT(*) FROM external_identities WHERE grape_user_id=? "
                f"AND status IN ({ph})",
                (grape_user_id, *wanted),
            )
            return c.fetchone()[0]
    except Exception:
        return 0

def has_password(grape_user_id: str) -> bool:
    """检查用户是否已设置密码。"""
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT password_hash FROM grape_accounts WHERE id=?",
                (grape_user_id,),
            )
            row = c.fetchone()
        if not row:
            return False
        return bool(row[0])
    except Exception:
        return False

def update_external_identity_tokens(
    grape_user_id: str,
    provider: str,
    access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
    external_name: str = "",
    external_avatar: str = "",
) -> bool:
    """每次 OAuth 授权刷新 token；status=4（过期）随之恢复 status=1。

    Issue #372 R10 + #3054 P2-1：一次新授权即代表凭据重新有效——服务层在
    status=4 → 1 时打 ``identity_status4_recovered`` 结构化事件。
    """
    return _identity_lifecycle.update_tokens(
        grape_user_id, provider, access_token=access_token,
        refresh_token=refresh_token, expires_in=expires_in,
        external_name=external_name, external_avatar=external_avatar)


def sync_external_login_on_rename(
    grape_user_id: str,
    provider: str,
    new_external_login: str,
    old_external_login: str = "",
) -> bool:
    """登录名变更同步（Issue #497）：external_id 不变、login 变（CSDN 联动改名最常见）。

    在 OAuth 登录回调「已绑定」/rebind 分支检测到 user_info.external_login 与库内
    external_login 不一致时调用，四表一致并写 audit_logs：
    1. external_identities.external_login → 新登录名；
    2. grape_accounts.username → 新登录名（UNIQUE 冲突时保留旧 username，不阻断登录）；
    3. unified_user_mapping 平台列 → 复用 _sync_um_platform_col；
    4. login_whitelist 平台列 → 旧名行改新名（UNIQUE 冲突时保留旧行，不阻断）。

    先对比：new 为空或与 old 相同 → 直接返回 False（不产生写放大）。
    返回 True 表示发生同步；False 表示无需同步或同步失败（失败不阻断登录）。
    """
    new_login = (new_external_login or "").strip()
    old_login = (old_external_login or "").strip()
    if not new_login or new_login == old_login:
        return False

    # 1) external_identities.external_login（活跃/过期绑定，与 token 刷新口径一致 status IN (1,4)）
    #    核心目标失败 → 不再继续（避免四表半同步），返回 False 不阻断登录
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE external_identities SET external_login=?, updated_at=datetime('now') "
                "WHERE grape_user_id=? AND provider=? AND status IN (1, 4)",
                (new_login, grape_user_id, provider),
            )
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Sync external_login failed for user {grape_user_id} provider {provider}: {e}")
        return False

    # 2) grape_accounts.username（UNIQUE 冲突 → 保留旧 username，不会因此登录失败）
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "UPDATE grape_accounts SET username=?, updated_at=datetime('now') WHERE id=?",
                (new_login, grape_user_id),
            )
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Sync username failed for user {grape_user_id} (UNIQUE conflict? keep old username): {e}")

    # 3) unified_user_mapping 平台列
    _sync_um_platform_col(grape_user_id, provider, new_login)

    # 4) login_whitelist 联动（与 2026-10-29 现网修数口径一致；冲突保留旧行）
    # 2026-11-12 行式化（weekly-11）：双写——新行按 (provider, username) 行式
    # UPDATE；老列按 provider key 派生列名 UPDATE（列不存在捕获跳过，新装/
    # 无老列渠道零影响）。渠道卡 whitelist_column 字段已退役，不再读卡。
    try:
        import re as _re
        if _re.match(r'^[a-z0-9_]+$', provider or ""):
            with db_lock:
                c = _mg_db.cursor()
                # login_whitelist 表无 updated_at 列（DDL 仅 created_at），只改用户名不碰时间戳
                c.execute(
                    "UPDATE login_whitelist SET username=? "
                    "WHERE provider=? AND username=? AND username!=''",
                    (new_login, provider, old_login),
                )
                col = f"{provider}_username"
                try:
                    c.execute(
                        f"UPDATE login_whitelist SET {col}=? WHERE {col}=?",
                        (new_login, old_login),
                    )
                except Exception:
                    pass  # 该渠道无老列（新装/未声明列存量）——跳过联动
                _mg_db.commit()
    except Exception as e:
        log.warning(f"Sync login_whitelist failed for user {grape_user_id} provider {provider}: {e}")

    # 5) audit_logs 留痕（event_type=login_rename，detail 记 old/new）
    try:
        from src.domains.mgmt.auth.audit import log_audit
        log_audit("login_rename", user_id=grape_user_id,
                  detail={"provider": provider, "old_login": old_login, "new_login": new_login})
    except Exception as e:
        log.warning(f"Sync audit login_rename failed for user {grape_user_id}: {e}")

    log.info(f"OAuth login name synced: user {grape_user_id} {provider} {old_login} -> {new_login}")
    return True

def mark_identity_expired(grape_user_id: str, provider: str) -> bool:
    """Issue #372 R10: 标记 token 过期（status=4），保留 token 供用户查看原账号。

    与 status=0（解绑，清 token）区别：过期只是凭据失效，重新授权即恢复。
    调用方：刷新失败 / 401 重试链 —— Issue #3054 P2 后不再造成死锁（重登即恢复）。
    """
    return _identity_lifecycle.mark_expired(grape_user_id, provider)


def is_identity_expired(grape_user_id: str, provider: str) -> bool:
    """Issue #372 R10: 检查用户 token 是否已过期（status=4）。"""
    return _identity_lifecycle.is_expired(grape_user_id, provider)


def get_unbound_identity(provider: str, external_id: str) -> "dict | None":
    """查询历史绑定记录（status=0）。用于 OAuth 登录时检测曾绑定过的账号。

    返回 {grape_user_id, external_login, external_name, external_avatar, unbound_at} 或 None。
    不返回 access_token/refresh_token（已清空）。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT grape_user_id, external_login, external_name, external_avatar, unbound_at "
                "FROM external_identities WHERE provider=? AND external_id=? AND status=0",
                (provider, external_id),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get unbound identity failed: {e}")
        return None
    if not row:
        return None
    return {
        "grape_user_id": row[0], "external_login": row[1],
        "external_name": row[2], "external_avatar": row[3],
        "unbound_at": row[4],
    }

def restore_external_identity(
    grape_user_id: str,
    provider: str,
    access_token: str = "",
    refresh_token: str = "",
    external_name: str = "",
    external_avatar: str = "",
    expires_in: int = 0,
) -> bool:
    """恢复绑定为活跃状态（status 0→1），同时更新 token 和用户信息。

    D10: access_token/refresh_token 加密存储。
    P2 加固：在同一事务内检查原用户是否已有同 provider 的活跃绑定（status=1），
    如果有则先标记为 status=0，避免 UNIQUE(grape_user_id, provider) 冲突。
    Issue #343 P1-4 修复: 新增 expires_in 参数，恢复绑定时写入 token_expires_at。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    _expires_at_str = ""
    if expires_in:
        _expires_at_str = _time.strftime("%Y-%m-%d %H:%M:%S",
                                         _time.gmtime(_time.time() + expires_in))
    try:
        with db_lock:
            c = _mg_db.cursor()
            # 检查原用户是否已有同 provider 的活跃绑定（可能解绑后又绑了另一个账号）
            c.execute(
                "SELECT id FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (grape_user_id, provider),
            )
            existing_active = c.fetchone()
            if existing_active:
                # 已有活跃绑定，先标记为 status=0（替换语义）
                c.execute(
                    "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                    "unbound_at=? WHERE id=?",
                    (now, existing_active[0]),
                )
            sets = [
                "status=1", "unbound_at=NULL", f"updated_at='{now}'",
            ]
            params = []
            if access_token:
                sets.append("access_token=?")
                params.append(encrypt(access_token))
            if refresh_token:
                sets.append("refresh_token=?")
                params.append(encrypt(refresh_token))
            if external_name:
                sets.append("external_name=?")
                params.append(external_name)
            if external_avatar:
                sets.append("external_avatar=?")
                params.append(external_avatar)
            # Issue #343 P1-4: 恢复绑定时写入 token_expires_at
            if _expires_at_str:
                sets.append("token_expires_at=?")
                params.append(_expires_at_str)
            params.extend([grape_user_id, provider])
            c.execute(
                f"UPDATE external_identities SET {', '.join(sets)} "
                f"WHERE grape_user_id=? AND provider=? AND status=0",
                params,
            )
            _mg_db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Restore external identity failed: {e}")
        return False

def restore_tombstoned_identity(
    grape_user_id: str,
    provider: str,
    external_id: str,
    access_token: str = "",
    refresh_token: str = "",
    external_name: str = "",
    external_avatar: str = "",
    expires_in: int = 0,
) -> bool:
    """凭墓碑把旧平台账号重新绑回原账号（Issue #3054 P2-2）。

    换绑后旧账号登录、status=4 解绑后旧账号登录都走这里——按 (用户, 平台)
    复用唯一一行，绝不新建第二个 Grape 号。
    """
    return _identity_lifecycle.restore_from_tombstone(
        grape_user_id=grape_user_id, provider=provider, external_id=external_id,
        access_token=access_token, refresh_token=refresh_token,
        external_name=external_name, external_avatar=external_avatar,
        expires_in=expires_in)


def delete_unbound_identity(provider: str, external_id: str) -> bool:
    """硬删除历史绑定记录（status=0）。

    用于转移绑定场景：用户选择绑定到其他 Grape 账号时，
    先删除历史记录释放 UNIQUE 约束，再在新账号下创建活跃绑定。
    """
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status=0",
                (provider, external_id),
            )
            _mg_db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete unbound identity failed: {e}")
        return False

def save_binding_ticket(
    initiating_user_id: str,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    original_user_id: str = "",
) -> str | None:
    """发放一次性转移票据（10 分钟 TTL，绑 session 与目标 external_id）。

    票据不存 access_token/refresh_token——凭据不出库；转移成功后新绑定 token
    留空，用户下次 OAuth 授权时自动补齐。
    Issue #3054 P2-1：status=4 与 status=0 同等可发票据（同一入口）。
    """
    return _identity_lifecycle.issue_ticket(
        initiating_user_id=initiating_user_id, provider=provider,
        external_id=external_id, external_login=external_login,
        external_name=external_name, external_avatar=external_avatar,
        original_user_id=original_user_id)


def validate_binding_ticket(ticket: str, initiating_user_id: str,
                            consume: bool = True) -> dict | None:
    """校验票据（存在/未过期/发起人一致）；consume=False 只校验不消费。

    Issue #376：transfer-bind 走「先校验 → 业务事务成功后再消费」，
    业务失败不再连带作废票据。
    """
    return _identity_lifecycle.validate_ticket(ticket, initiating_user_id,
                                               consume=consume)


def consume_binding_ticket(ticket: str) -> bool:
    """消费（删除）票据——一次性语义；业务 commit 成功后调用。"""
    return _identity_lifecycle.consume_ticket(ticket)


def get_pending_transfer_context(initiating_user_id: str) -> dict | None:
    """当前用户最近一条待转移票据（只读，不消费；popup 模式用）。"""
    return _identity_lifecycle.pending_ticket(initiating_user_id)


def transfer_external_identity(
    new_user_id: str,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    access_token: str = "",
    refresh_token: str = "",
    original_user_id: str = "",
) -> bool:
    """原子性转移绑定：旧绑定标记 transferred(status=0) → 新绑定创建(status=1)。

    前置条件：调用方已检查原账号有其他可用登录方式。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = _mg_db.cursor()
            # 1. 旧绑定标记为 transferred（status=0, 清 token）
            # Issue #3054 P2：status=4（token 过期）同为已有绑定，一并迁出
            if original_user_id:
                c.execute(
                    "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                    "unbound_at=? WHERE grape_user_id=? AND provider=? AND status IN (1, 4)",
                    (now, original_user_id, provider),
                )
            # 处理目标用户已有的同 provider 活跃绑定（status=1/4）
            # 用条件 UPDATE 代替 SELECT+UPDATE，避免 MySQL auto-commit 架空事务
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status IN (1, 4)",
                (now, new_user_id, provider),
            )
            # 同时清理同 provider+external_id 的旧记录（status 0/4），防止 UNIQUE 冲突
            c.execute(
                "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status IN (0, 4)",
                (provider, external_id),
            )
            # 还要清理目标用户已有的同 provider 旧记录（status 0/4），
            # 否则 UNIQUE(grape_user_id, provider) 约束会冲突
            c.execute(
                "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status IN (0, 4)",
                (new_user_id, provider),
            )
            # 2. 创建新活跃绑定
            c.execute(
                "INSERT INTO external_identities "
                "(id, grape_user_id, provider, external_id, external_login, "
                "external_name, external_avatar, access_token, refresh_token) "
                "VALUES (?, ?,?,?,?,?,?,?,?)",
                (uuid7_str(), new_user_id, provider, external_id, external_login,
                 external_name, external_avatar,
                 encrypt(access_token) if access_token else "",
                 encrypt(refresh_token) if refresh_token else ""),
            )
            _mg_db.commit()
        return True
    except Exception as e:
        log.warning(f"transfer_external_identity failed: {e}")
        try:
            _mg_db.rollback()
        except Exception:
            pass
        return False

import uuid

from src.kernel.id_gen import uuid7_str

from src.kernel.persistence.repositories import identity_admin_repository as _iar
from src.domains.mgmt.auth.identity_lifecycle import identity_lifecycle as _identity_lifecycle

from src.kernel.persistence.uow import uow as _uow

def revoke_provider_credentials(grape_user_id: str, provider: str) -> dict:
    """D40/S60 凭据撤销域服务：撤销该 provider 名下用户级凭据形态全集。

    身份域拥有凭据**键族规则**（集中一处，不散落调用点）：
      - {provider}_oauth：OAuth 登录凭据（user_credentials）
      - {provider}_pat：PAT 泛型凭据（S52 platform_tokens 写入形态）
    解绑/吊销场景单入口调用——此前解绑只清 _oauth，_pat 残留可用
    （撤销不传播的实锤缺口）。

    返回 {键: 是否删除成功}（键不存在返回 False，幂等安全）。
    external_identities 软删由调用方走 delete_external_identity
    （含 keep-one 语义，属账号域决策）。
    """
    from src.domains.mgmt.user import db as user_db
    result = {}
    # A2：按 (provider, kind) 撤销——覆盖归位行与双写期旧键行
    for kind in ("oauth", "pat"):
        key = f"{provider}_{kind}"
        try:
            result[key] = bool(user_db.delete_credential(grape_user_id, provider, kind=kind))
        except Exception as _e:
            log.warning(f"revoke_provider_credentials: revoke {key} failed "
                        f"(user={grape_user_id}): {_e}")
            result[key] = False
    return result

def resolve_user_id_by_platform_login(provider: str, platform_login: str) -> int | str:
    """M5/S51：按平台登录名反查 grape user_id——身份优先归属解。

    归属（事件/issue → 账号）的原实现是 unified_user_mapping 名字域 join
    （注册名 ≠ 平台登录名错位 → user_id=0 静默算错）。本函数走
    external_identities（平台登录名的权威域，与协议无关）：
    provider + external_login 精确匹配活跃绑定。

    返回 0 = 未命中（调用方回退既有 um 链，行为不变——零危害优先解：
    不迁数据、不删表，身份命中优先，未命中降级）。
    """
    if not provider or not platform_login:
        return 0
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT um.grape_user_id FROM external_identities um "
                "WHERE um.provider=? AND um.external_login=? AND um.status=1 "
                "AND EXISTS (SELECT 1 FROM grape_accounts ga "
                "            WHERE ga.id=um.grape_user_id AND ga.status=1) "
                "ORDER BY um.grape_user_id LIMIT 1",
                (provider, platform_login))
            row = c.fetchone()
        return row[0] if row and row[0] else 0
    except Exception as e:
        log.warning(f"resolve_user_id_by_platform_login failed ({provider}/{platform_login}): {e}")
        return 0

def _identity_meta(meta_json: str) -> dict:
    import json as _json
    try:
        v = _json.loads(meta_json or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}

def mark_identity_pat(grape_user_id: str, provider: str, pat_login: str = "") -> None:
    """D102-P：PAT 归属打标——metadata.source='pat'（ei 行只记身份，不承载 token）。"""
    import json as _json
    from sqlalchemy import select as _sel
    from src.kernel.persistence.models import ExternalIdentityModel as _M
    try:
        with _uow() as session:
            rows = session.execute(_sel(_M).where(
                _M.grape_user_id == str(grape_user_id), _M.provider == provider,
                _M.status.in_((1, 4)))).scalars().all()
            if not rows:
                # 命不中必须 loud（2026-11-13 复核）：原实现空转静默，上层照样打
                # "PAT bound" 成功日志，绑定其实没落（身份行缺失/归属在他人名下）。
                log.warning(
                    "mark_identity_pat: no active identity for user=%s provider=%s — "
                    "PAT metadata not marked (identity row may be missing or owned elsewhere)",
                    grape_user_id, provider)
            for m in rows:
                try:
                    meta = _json.loads(m.metadata_json or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta.update({"source": "pat", "pat_login": pat_login})
                m.metadata_json = _json.dumps(meta, ensure_ascii=False)
    except Exception as e:
        log.warning(f"mark_identity_pat failed (user={grape_user_id}, provider={provider}): {e}")

def backfill_pat_identity_tokens() -> int:
    """D102-P 一次性回填（幂等）：历史实现把 PAT 写进 ei.access_token（与 OAuth
    token 共字段）——按凭据域 <p>_pat 的 token 值精确比对，命中则清空 ei token
    字段并打标 metadata.source='pat'。PAT 权威存储本就在凭据域，供给链
    fetch_user_token ① 走 <p>_pat，清空不断供。返回迁移行数。"""
    from sqlalchemy import select as _sel
    from src.kernel.persistence.models import ExternalIdentityModel as _M, UserCredentialModel as _C
    from src.domains.mgmt.auth.credential_crypto import decrypt
    import json as _json
    moved = 0
    moved_out = 0
    try:
        with _uow() as session:
            # D105k：LIKE '\%_pat' 在 SQLite（无默认转义）匹配不中——改宽匹配+Python 精确过滤
            pats = [c for c in session.execute(_sel(_C)).scalars().all()
                    if c.provider.endswith("_pat")]
            for c in pats:
                user_id, prov = c.user_id, c.provider[:-4]  # 去掉 _pat 后缀 = 平台名
                try:
                    # D105-4 复审采纳：先 decrypt 再 loads（对齐 user/db.py _load_credential_json
                    # 模式；原先 loads 后 decrypt 靠异常当控制流，加密存储下必抛一次）
                    cfg = {}
                    if c.config_json:
                        try:
                            cfg = _json.loads(decrypt(c.config_json))
                        except Exception:
                            cfg = {}
                except Exception:
                    continue
                # config_json 通道层加密，读侧解密取 token
                try:
                    cfg = _json.loads(decrypt(c.config_json)) if not isinstance(cfg, dict) else cfg
                except Exception:
                    pass
                tok = str((cfg or {}).get("token", "") or "")
                if not tok:
                    continue
                rows = session.execute(_sel(_M).where(
                    _M.grape_user_id == str(user_id), _M.provider == prov,
                    _M.status.in_((1, 4)))).scalars().all()
                for m in rows:
                    if not m.access_token:
                        continue
                    try:
                        at = decrypt(m.access_token)
                    except Exception:
                        continue
                    if at.strip() != tok.strip():  # D105k：strip 归一防存储差异漏匹配
                        continue
                    m.access_token = ""  # PAT 归位：ei 不再承载 token
                    try:
                        meta = _json.loads(m.metadata_json or "{}")
                    except Exception:
                        meta = {}
                    if not isinstance(meta, dict):
                        meta = {}
                    meta.update({"source": "pat", "pat_login": m.external_login or ""})
                    m.metadata_json = _json.dumps(meta, ensure_ascii=False)
                    moved += 1
        if moved:
            log.info(f"backfill_pat_identity_tokens: {moved} row(s) migrated (ei token -> credential domain)")

        # D105k 解耦收口·搬家（幂等）：ei 里的历史 PAT 搬进凭据域 <p>_pat——
        # 接口层零暴露：搬完后 ei.access_token 语义纯净（非空即 OAuth），
        # 供给链 ①<p>_pat ②ei 兜底双读，搬家断不了供。
        # 三层证据：
        # ① 凭据域等值副本已在 → 只清 ei（原逻辑）
        # ② refresh/expires 痕迹 → 真 OAuth，保留
        # ③ OAuth 渠道未配置 → 可证明非 OAuth → 搬家（token 写入凭据域后清 ei）
        # ④ 渠道在配、无痕迹 → 不可判定，保守保留（误显示已绑定的边角，重绑自愈）
        from src.domains.mgmt.auth.credential_crypto import decrypt as _dec
        moved_out = 0
        # D105k：候选行经 ORM 读出，落写走全局连接裸 SQL——与 set_credential
        # 同连接同事务语义（ORM UoW 与全局连接混用时 ei 变更曾静默丢失，实锤）
        for m in session.execute(_sel(_M).where(_M.status.in_((1, 4)))).scalars().all():
            if m is None or not m.access_token:
                continue  # None 行（组合运行下 ORM 重载残留）：跳过不中断整批
            # refresh/expires 是 OAuth 痕迹（PAT 专用路径从不写）。注意两者都是
            # 加密存储：encrypt("") 密文非空，必须解密后判空（create 空参会落密文）
            try:
                _refresh = _dec(m.refresh_token).strip() if m.refresh_token else ""
            except Exception:
                _refresh = "?"
            if _refresh or m.token_expires_at:
                continue  # 有 OAuth 痕迹：不动
            try:
                _tok = _dec(m.access_token).strip()
            except Exception:
                continue  # 解不开（非本密钥体系）→ 不动
            if not _tok or _oauth_channel_configured(m.provider):
                continue  # 渠道在配 → 可能是真 OAuth，保守保留
            prov, uid = m.provider, str(m.grape_user_id)
            try:
                from src.domains.mgmt.user.db import get_credential as _gc, set_credential as _sc
                _existing = _gc(uid, prov, kind="pat") or {}
                if str(_existing.get("token", "") or "").strip() == _tok:
                    # 凭据域已有等值副本：仅清 ei
                    with db_lock:
                        c = _mg_db.cursor()
                        c.execute("UPDATE external_identities SET access_token='', "
                                  "metadata='' WHERE id=?", (str(m.id),))
                        _mg_db.commit()
                    moved_out += 1
                    continue
                _sc(uid, prov, {"token": _tok, "login": m.external_login or ""}, kind="pat")
                try:
                    meta = _json.loads(m.metadata_json or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta.update({"source": "pat", "pat_login": m.external_login or ""})
                with db_lock:
                    c = _mg_db.cursor()
                    c.execute("UPDATE external_identities SET access_token='', "
                              "metadata=? WHERE id=?",
                              (_json.dumps(meta, ensure_ascii=False), str(m.id)))
                    _mg_db.commit()
                moved_out += 1
            except Exception as _e:
                log.warning(f"backfill: move-to-credential-domain failed "
                            f"(user={uid}, provider={prov}): {_e}")
                continue
        if moved_out:
            log.info(f"backfill_pat_identity_tokens: {moved_out} row(s) legacy PAT moved "
                     f"ei -> credential domain (D105k decoupling, supply preserved)")
        session.flush()  # D105k：搬家变更同拍落库
    except Exception as e:
        log.warning(f"backfill_pat_identity_tokens failed (non-fatal): {e}", exc_info=True)
    return moved + moved_out

def _oauth_channel_configured(provider: str) -> bool:
    """该平台 OAuth 登录渠道是否真正可用（client_id 非空，D105k）。

    判据=卡条目内 client_id 或 meta oauth_<p>_client_id 任一非空。卡里"有这个
    平台的键"不算配置——gitcode/github 卡是内置默认形态（无 client_id 字段，
    client_id 走 meta 且常为空），OAuth 流程发不起来，不能当作"渠道在配"
    去阻止存量 PAT 搬家（uid=24 实测卡在配但 client_id 空，PAT 行被误保守保留）。
    """
    try:
        from src.domains.mgmt.user.db import get_meta_value
        import json as _json
        if (get_meta_value(f"oauth_{provider}_client_id") or "").strip():
            return True
        card = get_meta_value("oauth_providers_config")
        if card:
            cfg = _json.loads(card)
            entry = cfg.get(provider) if isinstance(cfg, dict) else None
            if isinstance(entry, dict):
                return bool(str(entry.get("client_id") or "").strip()
                            or str(entry.get("client_key") or "").strip())
        return False
    except Exception:
        return False  # 读不到配置 → 无法证明渠道可用 → 按未配置处理（搬家偏保守方向）
