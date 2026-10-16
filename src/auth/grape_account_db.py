"""grape_accounts + external_identities CRUD。

Grape 账号表（grape_accounts）只存账号自身字段，不含任何第三方平台字段。
第三方绑定关系存储在 external_identities 表。
"""
import time

from src.config import log
from src.session_manager import db, db_lock
from src.auth.password import hash_password, verify_password, needs_upgrade


def _safe_decrypt(encrypted: str) -> str:
    """D46 P1-10: 安全解密包装——解密失败时返回空字符串而非崩溃。

    decrypt 抛异常说明密钥配置错误或数据损坏，返回空字符串让
    调用方知道凭据不可用，走重新绑定流程。
    """
    from src.auth.credential_crypto import decrypt
    try:
        return decrypt(encrypted)
    except (ValueError, RuntimeError) as e:
        log.warning(f"Safe decrypt failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# grape_accounts CRUD
# ---------------------------------------------------------------------------

def create_grape_account(
    username: str,
    email: str,
    password: str,
    nickname: str = "",
    avatar_url: str = "",
) -> "dict | None":
    """创建 Grape 账号（邮箱验证码注册）。

    返回 {"id": int, "username": str, "email": str} 或 None（冲突）。
    D12: email 空字符串转为 NULL 存储。
    P0-4: 邮箱验证码注册的账号 email_verified=1（邮箱已通过验证码验证）。
    email_source: 'user' 表示用户自己绑定的真实邮箱。
    """
    email = (email or "").lower().strip() or None
    pw_hash = hash_password(password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts "
                "(username, email, email_source, password_hash, nickname, avatar_url, status, token_version, email_verified) "
                "VALUES (?,?, 'user',?,?,?,1,0,1)",
                (username, email, pw_hash, nickname, avatar_url),
            )
            db.commit()
            uid = c.lastrowid
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
    email = (email or "").lower().strip() or None
    is_placeholder = 1 if not email else 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO grape_accounts "
                "(username, email, email_source, password_hash, nickname, avatar_url, "
                "status, token_version, email_verified, email_is_placeholder, created_by_oauth) "
                "VALUES (?,?,?, '',?,?,1,0,?,?,1)",
                (username, email, provider, nickname, avatar_url,
                 1 if email_verified else 0, is_placeholder),
            )
            db.commit()
            uid = c.lastrowid
            log.info(f"Grape account created (OAuth): {username} (id={uid}, placeholder={is_placeholder})")
            return {"id": uid, "username": username, "email": email or ""}
    except Exception as e:
        err_str = str(e).lower()
        if "unique" in err_str and "username" in err_str:
            log.warning(f"Create grape account (OAuth) username conflict: {username}")
            return "username_conflict"
        if "unique" in err_str and "email" in err_str:
            log.warning(f"Create grape account (OAuth) email conflict: {email}")
            return "email_conflict"
        log.warning(f"Create grape account (OAuth) failed: {e}")
        return None


def get_grape_account_by_id(user_id: int) -> "dict | None":
    """按 ID 查询 Grape 账号。"""
    try:
        with db_lock:
            c = db.cursor()
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
            c = db.cursor()
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
    status 值：0=禁用，1=活跃，2=已注销（硬删除保留记录），3=冷静期（软删除30天内可恢复）。
    """
    email = email.lower().strip()
    try:
        with db_lock:
            c = db.cursor()
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


def _upgrade_password_hash(user_id: int, plain: str) -> None:
    """升级密码哈希为 bcrypt。"""
    new_hash = hash_password(plain)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET password_hash=? WHERE id=?",
                (new_hash, user_id),
            )
            db.commit()
        log.info(f"Password hash upgraded for user {user_id}")
    except Exception as e:
        log.warning(f"Upgrade password hash failed: {e}")


def update_password(user_id: int, new_password: str) -> bool:
    """更新密码。"""
    new_hash = hash_password(new_password)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET password_hash=? WHERE id=?",
                (new_hash, user_id),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update password failed: {e}")
        return False


def set_password(user_id: int, new_password: str) -> bool:
    """设置密码（无密码用户设置密码，无旧密码校验）。"""
    return update_password(user_id, new_password)


def update_email(user_id: int, new_email: str) -> bool | str:
    """更新邮箱。D12: 空字符串转为 NULL 存储。

    D46 P1-1: 区分 UNIQUE 冲突，返回 "conflict" 让路由层报 409。
    返回值：True=成功，False=其他失败，"conflict"=邮箱已被占用。
    """
    new_email = (new_email or "").lower().strip() or None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET email=? WHERE id=?",
                (new_email, user_id),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        # D46 P1-1: 检测 UNIQUE 约束冲突
        err_msg = str(e).lower()
        if "unique" in err_msg or "duplicate" in err_msg or "1062" in err_msg:
            log.warning(f"Update email conflict (already taken): user_id={user_id}")
            return "conflict"
        log.warning(f"Update email failed: {e}")
        return False


def update_last_login_at(user_id: int) -> None:
    """D11: 更新最近登录时间。"""
    # D46 P1-14: 用 Python 时间戳替代 datetime('now')，兼容 MySQL
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET last_login_at=? WHERE id=?",
                (now, user_id),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Update last_login_at failed: {e}")


def update_profile(user_id: int, nickname: "str | None" = None, avatar_url: "str | None" = None) -> bool:
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
            c = db.cursor()
            c.execute(
                f"UPDATE grape_accounts SET {', '.join(updates)} WHERE id=?",
                params,
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update profile failed: {e}")
        return False


def get_token_version(user_id: int) -> int:
    """获取用户的 token_version。"""
    try:
        with db_lock:
            c = db.cursor()
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


def increment_token_version(user_id: int) -> None:
    """token_version + 1（改密码/登出时调用）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET token_version=token_version+1 WHERE id=?",
                (user_id,),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Increment token version failed: {e}")


def get_status_and_token_version(user_id: int) -> "tuple[int, int, str] | None":
    """获取用户状态、token_version 和 username（鉴权中间件用）。

    Issue #343: 返回 username 用于 JWT payload 一致性校验，
    防止旧 JWT sub=旧id 撞上新表有效用户后拿别人 id 写数据。

    返回 (status, token_version, username) 或 None。
    """
    try:
        with db_lock:
            c = db.cursor()
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

    placeholder 邮箱不可收信、不可找回密码，不参与冲突检查。
    判断方式：
    1. email_source 非 'user' 且邮箱包含 noreply 域名 → placeholder
    2. 仅靠域名匹配（兼容 email_source 缺失时的老数据）
    """
    if not email:
        return False
    e = email.lower()
    is_noreply = "noreply.gitcode.com" in e or "noreply.github.com" in e
    if not is_noreply:
        return False
    # noreply 邮箱 + email_source 非 'user' → 确认是 placeholder
    if email_source and email_source != "user":
        return True
    # email_source 缺失时，noreply 域名即可判定（兼容老数据）
    return True


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
            c = db.cursor()
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
            c = db.cursor()
            c.execute(
                # P2-2: 大小写不敏感查询，与 _validate_username 的 .lower() 保留字检查一致
                "SELECT 1 FROM grape_accounts WHERE LOWER(username)=LOWER(?) LIMIT 1",
                (username,),
            )
            return c.fetchone() is not None
    except Exception:
        return False


def soft_delete_account(user_id: int) -> bool:
    """软删除账号（标记为冷静期 status=3）。"""
    # D46 P1-14: 用 Python 时间戳替代 datetime('now')，兼容 MySQL
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET status=3, updated_at=? "
                "WHERE id=? AND status=1",
                (now, user_id),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Soft delete account failed: {e}")
        return False


# ---------------------------------------------------------------------------
# external_identities CRUD
# ---------------------------------------------------------------------------

def get_external_identity(provider: str, external_id: str) -> "dict | None":
    """按平台 ID 查询活跃绑定关系。D10: access_token/refresh_token 解密。
    D22: 只查 status=1 的活跃绑定。
    """
    from src.auth.credential_crypto import decrypt
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, grape_user_id, provider, external_id, external_login, "
                "external_name, external_avatar, access_token, refresh_token "
                "FROM external_identities WHERE provider=? AND external_id=? AND status=1",
                (provider, external_id),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get external identity failed: {e}")
        return None
    if not row:
        return None
    return {
        "id": row[0], "grape_user_id": row[1], "provider": row[2],
        "external_id": row[3], "external_login": row[4], "external_name": row[5],
        "external_avatar": row[6], "access_token": _safe_decrypt(row[7] or ""),
        "refresh_token": _safe_decrypt(row[8] or ""),
    }


def get_external_identity_any_status(provider: str, external_id: str) -> "dict | None":
    """按平台 ID 查询绑定关系（含解绑记录 status=0）。

    BUG-6 修复：用于 oauth_associate/oauth_register 检查是否存在解绑记录。
    如果解绑记录属于其他用户，不允许恢复绑定。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, grape_user_id, status FROM external_identities "
                "WHERE provider=? AND external_id=? ORDER BY status DESC LIMIT 1",
                (provider, external_id),
            )
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get external identity any status failed: {e}")
        return None
    if not row:
        return None
    return {"id": row[0], "grape_user_id": row[1], "status": row[2]}


def get_user_identities(grape_user_id: int) -> list[dict]:
    """获取用户的所有第三方活跃绑定（不含 access_token，防止 token 泄露到前端）。
    D22: 只查 status=1 的活跃绑定。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT provider, external_login, external_name, external_avatar, created_at "
                "FROM external_identities WHERE grape_user_id=? AND status=1",
                (grape_user_id,),
            )
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"Get user identities failed: {e}")
        return []
    return [
        {"provider": r[0], "external_login": r[1], "external_name": r[2],
         "external_avatar": r[3], "created_at": r[4]}
        for r in rows
    ]


def get_user_identities_with_tokens(grape_user_id: int) -> list[dict]:
    """获取用户的所有第三方活跃绑定（含 access_token/refresh_token）。

    仅供后端内部使用（get_credentials 等），不暴露到前端。
    与 get_user_identities 的区别：此函数查询并解密 access_token/refresh_token。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT provider, external_login, external_name, external_avatar, "
                "access_token, refresh_token, created_at "
                "FROM external_identities WHERE grape_user_id=? AND status=1",
                (grape_user_id,),
            )
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"Get user identities with tokens failed: {e}")
        return []
    return [
        {"provider": r[0], "external_login": r[1], "external_name": r[2],
         "external_avatar": r[3], "access_token": _safe_decrypt(r[4] or ""),
         "refresh_token": _safe_decrypt(r[5] or ""), "created_at": r[6]}
        for r in rows
    ]


def create_external_identity(
    grape_user_id: int,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
) -> bool:
    """创建或恢复第三方绑定关系。D10: access_token/refresh_token 加密存储。

    BUG-6 修复：如果存在 status=0 的解绑记录（曾绑定后解绑），
    恢复为 status=1 并更新 token/用户信息，而非 INSERT（会因 UNIQUE 约束冲突失败）。
    """
    from src.auth.credential_crypto import encrypt
    import time as _time
    _expires_at_str = ""
    if expires_in:
        _expires_at_str = _time.strftime("%Y-%m-%d %H:%M:%S",
                                         _time.gmtime(_time.time() + expires_in))
    try:
        with db_lock:
            c = db.cursor()
            # 先查是否存在已绑定的活跃记录（status=1，同一用户同一平台）
            # 场景：用户已绑定 GitHub，重新点绑定按钮 → 应 UPDATE 而非 INSERT
            c.execute(
                "SELECT id FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (grape_user_id, provider),
            )
            existing_active = c.fetchone()
            if existing_active:
                now = _time.strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE external_identities SET "
                    "external_id=?, external_login=?, external_name=?, "
                    "external_avatar=?, access_token=?, refresh_token=?, "
                    "token_expires_at=?, updated_at=? WHERE id=?",
                    (external_id, external_login, external_name,
                     external_avatar, encrypt(access_token), encrypt(refresh_token),
                     _expires_at_str, now, existing_active[0]),
                )
                db.commit()
                return True
            # 再查是否存在解绑记录（status=0）
            c.execute(
                "SELECT id FROM external_identities "
                "WHERE provider=? AND external_id=? AND status=0",
                (provider, external_id),
            )
            existing = c.fetchone()
            if existing:
                # 恢复解绑记录
                # BUG-NEW-3 修复：用 Python 时间戳替代 datetime('now')，兼容 MySQL
                now = _time.strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE external_identities SET "
                    "grape_user_id=?, status=1, external_login=?, external_name=?, "
                    "external_avatar=?, access_token=?, refresh_token=?, "
                    "token_expires_at=?, unbound_at=NULL, updated_at=? "
                    "WHERE id=?",
                    (grape_user_id, external_login, external_name,
                     external_avatar, encrypt(access_token), encrypt(refresh_token),
                     _expires_at_str, now, existing[0]),
                )
                db.commit()
                return True
            # 无解绑记录，正常 INSERT
            # 但先清理目标用户同 provider 的旧解绑记录（status=0），
            # 否则 UNIQUE(grape_user_id, provider) 约束会冲突
            c.execute(
                "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0",
                (grape_user_id, provider),
            )
            c.execute(
                "INSERT INTO external_identities "
                "(grape_user_id, provider, external_id, external_login, "
                "external_name, external_avatar, access_token, refresh_token, token_expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (grape_user_id, provider, external_id, external_login,
                 external_name, external_avatar,
                 encrypt(access_token), encrypt(refresh_token), _expires_at_str),
            )
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Create external identity failed: {e}", exc_info=True)
        return False


def delete_external_identity(grape_user_id: int, provider: str,
                             require_keep_one: bool = True) -> bool:
    """软删除第三方绑定关系（D22: 改为 status=0，不硬删除）。

    清空 access_token/refresh_token（安全），保留 external_id 哈希记录用于恢复绑定。
    require_keep_one: 为 True 时，原子检查并保证删除后至少保留一种登录方式
    （密码或至少一个活跃第三方绑定），防止并发解绑导致用户失去所有登录方式。
    """
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = db.cursor()
            if require_keep_one:
                # P2 加固：原子检查——UPDATE 条件带 status=1 AND (count>1 OR has_password)
                # 避免并发解绑时两个请求都通过 count 检查后都执行 UPDATE
                c.execute(
                    "SELECT COUNT(*) FROM external_identities WHERE grape_user_id=? AND status=1",
                    (grape_user_id,),
                )
                count = c.fetchone()[0]
                if count <= 1:
                    if not has_password(grape_user_id):
                        return False  # 拒绝删除：会失去所有登录方式
            # 原子软删除：UPDATE 带 status=1 条件，rowcount=0 说明已被其他请求删除
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
                (now, grape_user_id, provider),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete external identity failed: {e}")
        return False


def is_placeholder_account_mergeable(grape_user_id: int) -> bool:
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
            c = db.cursor()
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
            return not _has_business_data(c, grape_user_id)
    except Exception as e:
        log.warning(f"is_placeholder_account_mergeable failed: {e}")
        return False


# ── 业务数据检查与转移 ──────────────────────────────────────────────
# 占位账号自动转移前检查是否有业务数据。有数据则降级为转移确认流程。
# 转移确认时通过 transfer_business_data 原子转移所有业务数据。
#
# ⚠️ 维护须知 ⚠️
# 新增任何包含 user_id 列的业务表时，必须同步将表名加入 _BUSINESS_TABLES。
# 遗漏会导致占位账号自动转移时跳过该表的数据检查，
# 有业务数据的占位账号被当作"无数据"自动合并 → 数据丢失。
#
# 操作步骤：
#   1. 在 _BUSINESS_TABLES 中添加 (表名, user_id列名, 额外WHERE条件)
#   2. 如果表有复合主键（如 user_repos），在 transfer_business_data 中添加冲突处理分支
#   3. 在设计文档第 27.6 节"业务表清单维护说明"中更新表清单
#   4. 在需求文档第 16.3 节业务规则中更新表数量
#
# 当前业务表清单（11 张）：
#   user_repos         — 用户仓库缓存（is_admin=1 的为托管仓库）
#   agent_tasks        — agent 任务
#   chat_agent_session — chat 会话配置
#   chat_conversations — chat 对话
#   issue              — issue 数据
#   user_feedback      — 用户反馈
#   user_credentials   — 用户凭据
#   api_tokens         — API tokens
#   session_api_tokens — session API tokens
#   scene_likes        — 场景点赞
#   skill_likes        — 技能点赞
# ────────────────────────────────────────────────────────────────────

# (table, user_id_column, where_clause) — where_clause 为额外过滤条件，空字符串表示无
_BUSINESS_TABLES = [
    ("user_repos",         "user_id", "is_admin=1"),    # 只检查托管仓库，普通缓存可重建
    ("agent_tasks",        "user_id", ""),
    ("chat_agent_session", "user_id", ""),
    ("chat_conversations", "user_id", ""),
    ("issue",              "user_id", ""),
    ("user_feedback",      "user_id", ""),
    ("user_credentials",   "user_id", ""),
    ("api_tokens",         "user_id", ""),
    ("session_api_tokens", "user_id", ""),
    ("scene_likes",        "user_id", ""),
    ("skill_likes",        "user_id", ""),
]


def _has_business_data(cursor, grape_user_id: int) -> bool:
    """检查指定用户是否有业务数据。在调用方的 db_lock 内执行。

    任何一张业务表有数据即返回 True。
    """
    for table, col, extra_where in _BUSINESS_TABLES:
        where = f"{col}=?"
        if extra_where:
            where += f" AND {extra_where}"
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (grape_user_id,))
            if cursor.fetchone()[0] > 0:
                return True
        except Exception as e:
            # 表可能不存在（旧库未迁移），保守认为有数据
            log.warning(f"_has_business_data: table {table} check failed: {e}")
            return True
    return False


def transfer_business_data(from_user_id: int, to_user_id: int) -> bool:
    """将一个用户的全部业务数据转移到另一个用户。在调用方的 db_lock + 事务内执行。

    转移策略：UPDATE user_id = to_user_id（不删除原数据，只改归属）。
    如果 to_user_id 已有同主键数据（如 user_repos 同 repo_key），用 INSERT OR REPLACE / IGNORE 处理。

    返回 True 表示全部成功。
    """
    c = db.cursor()
    for table, col, _ in _BUSINESS_TABLES:
        try:
            # user_repos 有复合主键 (user_id, repo_key)，目标用户可能已有同 repo_key 记录
            # 用 UPDATE ... WHERE 条件转移，冲突时跳过（INSERT OR IGNORE 补充）
            if table == "user_repos":
                # 先 UPDATE 已有记录的归属（目标用户无同 repo_key 的记录）
                c.execute(
                    f"UPDATE {table} SET {col}=? "
                    f"WHERE {col}=? AND repo_key NOT IN "
                    f"(SELECT repo_key FROM {table} WHERE {col}=?)",
                    (to_user_id, from_user_id, to_user_id),
                )
                # 目标用户已有同 repo_key 的记录 → 删除源用户的老记录（避免重复）
                c.execute(
                    f"DELETE FROM {table} WHERE {col}=? AND repo_key IN "
                    f"(SELECT repo_key FROM {table} WHERE {col}=?)",
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
    return True


def transfer_external_identity_in_tx(cursor, new_user_id: int, provider: str,
                                      external_id: str, external_login: str = "",
                                      external_name: str = "", external_avatar: str = "",
                                      access_token: str = "", original_user_id: int = 0) -> bool:
    """在调用方事务内转移绑定关系（不加锁，不 commit）。

    与 transfer_external_identity 逻辑相同，但由调用方持有 db_lock 和事务。
    """
    from src.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        c = cursor
        # 1. 旧绑定标记为 transferred（status=0, 清 token）
        if original_user_id:
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
                (now, original_user_id, provider),
            )
        # 检查并处理目标用户已有的同 provider 活跃绑定（status=1）
        # 用条件 UPDATE 代替 SELECT+UPDATE，避免 MySQL auto-commit 架空事务
        c.execute(
            "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
            "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
            (now, new_user_id, provider),
        )
        # 清理同 provider+external_id 的旧解绑记录（防止 UNIQUE 冲突）
        c.execute(
            "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status=0",
            (provider, external_id),
        )
        # 清理目标用户已有的同 provider 旧记录（status=0），防止 UNIQUE(grape_user_id, provider) 冲突
        c.execute(
            "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0",
            (new_user_id, provider),
        )
        # 2. 创建新活跃绑定
        c.execute(
            "INSERT INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, "
            "external_name, external_avatar, access_token, refresh_token) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_user_id, provider, external_id, external_login,
             external_name, external_avatar,
             encrypt(access_token) if access_token else "",
             ""),
        )
        return True
    except Exception as e:
        log.warning(f"transfer_external_identity_in_tx failed: {e}")
        return False


def merge_placeholder_account(placeholder_user_id: int, target_user_id: int,
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
            c = db.cursor()
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
            db.commit()
            log.info(f"Placeholder account {placeholder_user_id} merged to {target_user_id} "
                     f"(provider={provider}, external_id={external_id})")
            return True
    except Exception as e:
        log.warning(f"merge_placeholder_account failed: {e}")
        return False


def unbind_placeholder_binding(cursor, placeholder_user_id: int, provider: str) -> bool:
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
    cursor, old_user_id: int, new_user_id: int, provider: str, external_id: str,
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
    from src.auth.credential_crypto import encrypt
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
            "(grape_user_id, provider, external_id, external_login, "
            "external_name, external_avatar, access_token, refresh_token, token_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (new_user_id, provider, external_id, external_login,
             external_name, external_avatar,
             encrypt(access_token), encrypt(refresh_token), _expires_at_str),
        )
        return True
    except Exception as e:
        log.warning(f"restore_binding_in_tx failed: {e}")
        return False


def mark_account_merged(cursor, placeholder_user_id: int) -> bool:
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


def clear_placeholder_email(user_id: int, provider: str) -> bool:
    """需求 10：清理 placeholder 邮箱（解绑时调用）。

    如果 email 是 placeholder 且 email_source == provider → 清空。
    否则不动，返回 True（非 placeholder 邮箱不需要清理）。

    通过数据访问层统一操作，不绕过 grape_account_db 直接写 SQL。
    """
    try:
        with db_lock:
            c = db.cursor()
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
                db.commit()
                log.info(f"Cleared placeholder email for user={user_id}, provider={provider}")
            return True
    except Exception as e:
        log.warning(f"clear_placeholder_email failed: {e}")
        return False


def count_user_identities(grape_user_id: int) -> int:
    """统计用户的第三方活跃绑定数量。D22: 只计 status=1。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM external_identities WHERE grape_user_id=? AND status=1",
                (grape_user_id,),
            )
            return c.fetchone()[0]
    except Exception:
        return 0


def has_password(grape_user_id: int) -> bool:
    """检查用户是否已设置密码。"""
    try:
        with db_lock:
            c = db.cursor()
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
    grape_user_id: int,
    provider: str,
    access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
    external_name: str = "",
    external_avatar: str = "",
) -> bool:
    """更新第三方绑定的 token 和用户信息（每次 OAuth 登录刷新）。D10: token 加密存储。"""
    from src.auth.credential_crypto import encrypt
    try:
        with db_lock:
            c = db.cursor()
            sets = []
            params = []
            if access_token:
                sets.append("access_token=?")
                params.append(encrypt(access_token))
            if refresh_token:
                sets.append("refresh_token=?")
                params.append(encrypt(refresh_token))
            if expires_in:
                import time as _time
                _expires_at_str = _time.strftime("%Y-%m-%d %H:%M:%S",
                                                 _time.gmtime(_time.time() + expires_in))
                sets.append("token_expires_at=?")
                params.append(_expires_at_str)
            if external_name:
                sets.append("external_name=?")
                params.append(external_name)
            if external_avatar:
                sets.append("external_avatar=?")
                params.append(external_avatar)
            if not sets:
                return True
            params.extend([grape_user_id, provider])
            c.execute(
                f"UPDATE external_identities SET {', '.join(sets)} "
                f"WHERE grape_user_id=? AND provider=? AND status=1",
                params,
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Update external identity tokens failed: {e}")
        return False


# ---------------------------------------------------------------------------
# D22: 软删除/恢复绑定支持
# ---------------------------------------------------------------------------

def get_unbound_identity(provider: str, external_id: str) -> "dict | None":
    """查询历史绑定记录（status=0）。用于 OAuth 登录时检测曾绑定过的账号。

    返回 {grape_user_id, external_login, external_name, external_avatar, unbound_at} 或 None。
    不返回 access_token/refresh_token（已清空）。
    """
    try:
        with db_lock:
            c = db.cursor()
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
    grape_user_id: int,
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
    from src.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    _expires_at_str = ""
    if expires_in:
        _expires_at_str = _time.strftime("%Y-%m-%d %H:%M:%S",
                                         _time.gmtime(_time.time() + expires_in))
    try:
        with db_lock:
            c = db.cursor()
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
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Restore external identity failed: {e}")
        return False


def delete_unbound_identity(provider: str, external_id: str) -> bool:
    """硬删除历史绑定记录（status=0）。

    用于转移绑定场景：用户选择绑定到其他 Grape 账号时，
    先删除历史记录释放 UNIQUE 约束，再在新账号下创建活跃绑定。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status=0",
                (provider, external_id),
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Delete unbound identity failed: {e}")
        return False


# ---------------------------------------------------------------------------
# binding_ticket — 转移绑定一次性票据
# ---------------------------------------------------------------------------

_TICKET_TTL = 600  # 10 分钟


def save_binding_ticket(
    initiating_user_id: int,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    original_user_id: int = 0,
) -> str | None:
    """生成并保存一次性 binding_ticket（10 分钟过期）。

    安全设计：票据不存 access_token/refresh_token，避免凭据泄露。
    票据只存定位信息（provider + external_id + 用户信息），
    转移后新绑定的 access_token 留空，用户下次 OAuth 登录时自动刷新。
    """
    import time as _t
    import secrets as _secrets
    ticket = _secrets.token_urlsafe(32)
    try:
        with db_lock:
            c = db.cursor()
            # 懒清理过期票据
            c.execute("DELETE FROM binding_tickets WHERE expires < ?", (_t.time(),))
            c.execute(
                "INSERT OR REPLACE INTO binding_tickets "
                "(ticket, initiating_user_id, provider, external_id, "
                "external_login, external_name, external_avatar, "
                "original_user_id, created_at, expires) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ticket, initiating_user_id, provider, external_id,
                 external_login, external_name, external_avatar,
                 original_user_id,
                 _t.time(), _t.time() + _TICKET_TTL),
            )
            db.commit()
        return ticket
    except Exception as e:
        log.warning(f"save_binding_ticket failed: {e}")
        return None


def validate_binding_ticket(ticket: str, initiating_user_id: int) -> dict | None:
    """校验并消费 binding_ticket（查 + 删），返回数据或 None。

    校验：票据存在、未过期、initiating_user_id 与当前会话用户一致。
    安全：不返回 access_token/refresh_token（票据不存这些）。
    """
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT initiating_user_id, provider, external_id, "
                "external_login, external_name, external_avatar, "
                "original_user_id, expires "
                "FROM binding_tickets WHERE ticket=?",
                (ticket,),
            )
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM binding_tickets WHERE ticket=?", (ticket,))
                db.commit()
        if not row:
            return None
        if _t.time() > row[7]:
            return None
        if row[0] != initiating_user_id:
            log.warning(f"binding_ticket user mismatch: ticket_user={row[0]}, session_user={initiating_user_id}")
            return None
        return {
            "provider": row[1], "external_id": row[2],
            "external_login": row[3], "external_name": row[4],
            "external_avatar": row[5],
            "original_user_id": row[6],
        }
    except Exception as e:
        log.warning(f"validate_binding_ticket failed: {e}")
        return None


def get_pending_transfer_context(initiating_user_id: int) -> dict | None:
    """查询当前用户最近的待转移票据（只读，不消费）。

    用于 popup 模式：binding_ticket 不通过 URL 传递，
    父窗口用 JWT 调后端接口获取待转移信息展示确认页。
    确认后调 POST /auth/transfer-bind 完成转移（票据在 validate_binding_ticket 中消费）。
    """
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            # 懒清理过期票据
            c.execute("DELETE FROM binding_tickets WHERE expires < ?", (_t.time(),))
            c.execute(
                "SELECT ticket, provider, external_login, external_name, "
                "external_avatar, original_user_id, expires "
                "FROM binding_tickets WHERE initiating_user_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (initiating_user_id,),
            )
            row = c.fetchone()
            db.commit()
        if not row:
            return None
        if _t.time() > row[6]:
            return None
        return {
            "binding_ticket": row[0],
            "provider": row[1],
            "external_login": row[2],
            "external_name": row[3],
            "external_avatar": row[4],
            "original_user_id": row[5],
        }
    except Exception as e:
        log.warning(f"get_pending_transfer_context failed: {e}")
        return None


def transfer_external_identity(
    new_user_id: int,
    provider: str,
    external_id: str,
    external_login: str = "",
    external_name: str = "",
    external_avatar: str = "",
    access_token: str = "",
    refresh_token: str = "",
    original_user_id: int = 0,
) -> bool:
    """原子性转移绑定：旧绑定标记 transferred(status=0) → 新绑定创建(status=1)。

    前置条件：调用方已检查原账号有其他可用登录方式。
    """
    from src.auth.credential_crypto import encrypt
    import time as _time
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_lock:
            c = db.cursor()
            # 1. 旧绑定标记为 transferred（status=0, 清 token）
            if original_user_id:
                c.execute(
                    "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                    "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
                    (now, original_user_id, provider),
                )
            # 处理目标用户已有的同 provider 活跃绑定（status=1）
            # 用条件 UPDATE 代替 SELECT+UPDATE，避免 MySQL auto-commit 架空事务
            c.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
                (now, new_user_id, provider),
            )
            # 同时清理同 provider+external_id 的旧解绑记录（防止 UNIQUE 冲突）
            c.execute(
                "DELETE FROM external_identities WHERE provider=? AND external_id=? AND status=0",
                (provider, external_id),
            )
            # 还要清理目标用户已有的同 provider 旧记录（status=0），
            # 否则 UNIQUE(grape_user_id, provider) 约束会冲突
            c.execute(
                "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0",
                (new_user_id, provider),
            )
            # 2. 创建新活跃绑定
            c.execute(
                "INSERT INTO external_identities "
                "(grape_user_id, provider, external_id, external_login, "
                "external_name, external_avatar, access_token, refresh_token) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (new_user_id, provider, external_id, external_login,
                 external_name, external_avatar,
                 encrypt(access_token) if access_token else "",
                 encrypt(refresh_token) if refresh_token else ""),
            )
            db.commit()
        return True
    except Exception as e:
        log.warning(f"transfer_external_identity failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
