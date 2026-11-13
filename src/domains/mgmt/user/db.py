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

import json, hashlib, re, time, os, warnings, threading

from src.kernel.session_manager import db, db_lock

from src.kernel.config import log

def _apply_runtime_state_compat(c, runtime_id: int, status: str, last_seen_at: str) -> None:
    """延迟绑定 apply_runtime_state（Issue #1496）。

    src/domains/runtime/engine/service.py 反向 import 本模块（get_task_token），模块级
    `from src.domains.runtime.engine.health import apply_runtime_state` 会形成
    user.db → runtime.health → runtime/__init__ → service → user.db 环；
    运行期（调用点）再解析则无环。
    """
    from src.domains.runtime.engine.health import apply_runtime_state
    apply_runtime_state(c, runtime_id, status, last_seen_at)

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
    """保存一次性 exchange code（60 秒过期）。

    P2-1: 懒清理——每次保存时顺带删除过期 exchange_codes。
    #535 UC14：10s→60s。原 10s 窗口对 SPA 首载（公网无缓存拉全量静态资源
    经常 >10s）是必然冲突——回调成功→前端加载完才兑票→票已过期→401→
    被踢回登录页。60s 是缓解（治本为 UC15 内联脚本落地即兑，见 main.py）。
    安全论证：code 为 sha256(urandom(32)) 高熵值，威胁模型是截获而非枚举；
    一次性原子消费（validate_exchange_code SELECT+DELETE rowcount）不变；
    兑换是 POST（链接预取/邮件扫描器只发 GET，不会预消费）。
    """
    import time as _t
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM exchange_codes WHERE expires < ?", (_t.time(),))
            # FR-14.2: INSERT + 冲突重试（不覆盖旧票）
            try:
                c.execute(
                    "INSERT INTO exchange_codes "
                    "(code, user_id, username, provider, token_version, is_new, created_at, expires) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (code, user_id, username, provider, token_version,
                     1 if is_new else 0, _t.time(), _t.time() + 60),
                )
            except Exception as _exc:
                # FR-CQ-5: 约束冲突时删旧票重试
                # 保留原有 hasattr(__cause__) 条件——它恒为 True 使所有异常都走重试。
                # 不能用 _is_constraint_error() 替代——sqlite3.IntegrityError
                # ('UNIQUE constraint failed') 不含 'integrity'/'duplicate'/'1062'，
                # 会被错误地 re-raise。保持原有行为不变。
                _is_constraint = hasattr(_exc, '__cause__') or 'integrity' in str(_exc).lower() or 'duplicate' in str(_exc).lower() or '1062' in str(_exc)
                if not _is_constraint:
                    raise
                c.execute("DELETE FROM exchange_codes WHERE code=?", (code,))
                c.execute(
                    "INSERT INTO exchange_codes "
                    "(code, user_id, username, provider, token_version, is_new, created_at, expires) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (code, user_id, username, provider, token_version,
                     1 if is_new else 0, _t.time(), _t.time() + 60),
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

# ====== 凭据表 ======
# 铁律（2026-11-07）：所有新增表必须有 UUID 主键 id——应用层 uuid7_str() 生成、
# TEXT 存储、36 字符标准形式（8-4-4-4-12，不混 32 hex）。存量无 id 表随各域
# 批次逐步补列回填。
_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS user_credentials (
    id TEXT PRIMARY KEY,           -- UUID（36 字符标准形式，应用层生成；N1 2026-11-07）
    user_id TEXT NOT NULL,                -- D105 第二批：grape_accounts.id（UUID TEXT）
    -- provider 值域三族（动态派生，非固定枚举；平台清单由平台卡驱动）：
    --   云凭据裸名:    huawei_cloud / codearts（settings.py 固定白名单）
    --   PAT:           <platform>_pat（gitcode_pat/github_pat/gitee_pat…，settings.py）
    --   OAuth 副本:    <platform>_oauth（gitcode_oauth/github_oauth…，refresh_oauth_token 迁移期双写）
    provider TEXT NOT NULL,
    -- A2 凭据类型（2026-11-13 对象模型归位）：provider 存纯平台名/纯凭据名，
    -- 类型独立成列——pat | oauth(deprecated) | cloud | api_key，''=未分类。
    -- 写入方向：新写一律带 kind；读取双读（新形态 → 旧复合键 provider=f"{p}_{kind}" 回退）。
    kind TEXT NOT NULL DEFAULT '',
    config_json MEDIUMTEXT NOT NULL,
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    UNIQUE (user_id, provider)    -- 复合键降级为唯一约束：保 upsert 语义
                                  -- 显式契约：一用户一平台一条；同平台多类型须先扩此唯一键（A2 收口后评估）
)
"""

def _legacy_credential_key(provider: str, kind: str) -> str:
    """A2 双写期的旧复合键：<provider>_<kind>（kind='' 时无旧键）。"""
    return f"{provider}_{kind}" if kind else ""


def set_credential(user_id: "int | str", provider: str, config: dict, kind: str = "") -> bool:
    """保存凭据（通道层加密，2026-11-07 N1）。

    A2（2026-11-13 对象模型归位）：provider 存纯名字（平台名 / 凭据名），类型走
    kind 列 —— 类型不再编码进 provider 字符串。双写期同时写旧复合键行
    （provider=f"{provider}_{kind}"，kind=''），保证滚动升级窗口里老实例读得到；
    收口迁移删除旧键行后关闭双写。
    """
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt
        from src.kernel.id_gen import uuid7_str
        plaintext = json.dumps(config, ensure_ascii=False)
        import time as _time
        now = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime())
        target = encrypt(plaintext)
        rows = [(uuid7_str(), user_id, provider, kind or "", target, now, now)]
        legacy = _legacy_credential_key(provider, kind)
        if legacy:
            rows.append((uuid7_str(), user_id, legacy, "", target, now, now))
        with db_lock:
            c = db.cursor()
            # QA R1（2026-11-13）：唯一键仍是 (user_id, provider)，同平台另一种 kind 的
            # 归位行会被本次 upsert 改写（kind 被覆盖、另一种凭据值丢失）。扩
            # (user_id, provider, kind) 需重建表（SQLite 旧 UNIQUE 约束在场），列入
            # A2 收口；在此之前显式告警，不做静默改写。
            c.execute("SELECT COALESCE(kind,'') FROM user_credentials "
                      "WHERE user_id=? AND provider=? AND COALESCE(kind,'') NOT IN ('', ?)",
                      (user_id, provider, kind or ""))
            _other_kind = c.fetchone()
            if _other_kind:
                log.warning(
                    f"set_credential: provider={provider!r} 已有 kind={_other_kind[0]!r} 的行，"
                    f"本次 kind={kind!r} 会覆盖它（UNIQUE(user_id, provider) 限制）")
            for row in rows:
                c.execute(
                    """INSERT INTO user_credentials (id, user_id, provider, kind, config_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(user_id, provider) DO UPDATE
                       SET kind=excluded.kind, config_json=excluded.config_json, updated_at=excluded.updated_at""",
                    row)
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Set credential failed: {e}")
        return False

def get_credentials(user_id: "int | str") -> list[dict]:
    """获取用户凭据（D32-补丁：从 user_credentials 表 + external_identities 读，不查 user_accounts）

    A2：返回 kind 维度；双写期旧复合键行（provider 形如 `<name>_pat/_oauth` 且
    kind='') 与对应的新形态行重复，这里过滤掉旧键行，调用方只看到归位形态。
    """
    result = []
    # 1. user_credentials 表（huawei_cloud/codearts/feishu 等非 OAuth 凭据）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider, COALESCE(kind,''), config_json, COALESCE(updated_at,'') "
                  "FROM user_credentials WHERE user_id=?", (user_id,))
        rows = c.fetchall()
    # QA R2（2026-11-13）：与 get_credential 同口径——归位行与旧复合键行按 updated_at
    # 取较新者。原实现"归位行存在就隐藏旧键行"，在回退窗口（旧实例只写旧键）会一直
    # 显示 stale 值，列表侧与单点读不一致。
    picked = {(str(p), str(k or "")): (str(u or ""), cfg) for p, k, cfg, u in rows}
    for (p, k), (upd, cfg_json) in list(picked.items()):
        if not k and (p.endswith("_pat") or p.endswith("_oauth")):
            base, tail = p.rsplit("_", 1)
            norm = picked.get((base, tail))
            if norm is None:
                continue  # 迁移前形态：没有归位行时旧键行照常展示
            if norm[0] >= upd:
                picked.pop((p, k))                       # 归位行不旧 → 隐藏旧键行
            else:
                picked[(base, tail)] = (upd, cfg_json)   # 旧键更新 → 顶替展示
                picked.pop((p, k))
    for (p, k), (_upd, cfg_json) in picked.items():
        try:
            cfg = _load_credential_json(cfg_json) if cfg_json else {}
            result.append({"provider": p, "kind": k or "", "config": cfg})
        except Exception:
            pass
    # 2. external_identities（gitcode/github OAuth token）
    # BUG-1 修复：get_user_identities 不含 access_token 列（防泄露到前端），
    # 改用 get_user_identities_with_tokens 读取解密后的 token。
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities_with_tokens
        for ident in get_user_identities_with_tokens(str(user_id)):
            provider = ident.get("provider", "")
            token = ident.get("access_token", "")
            if provider and token:
                result.append({"provider": provider, "config": {"token": token}})
    except Exception as e:
        log.warning(f"get_credentials: read external_identities failed: {e}")
    return result

def get_credential(user_id: "int | str", provider: str, kind: str = "") -> dict | None:
    """获取指定凭据（读侧自动解密，历史明文行兼容）。

    A2 双读序：① 归位形态 (provider, kind) → ② 旧复合键 f"{provider}_{kind}" →
    ③ 该 provider 的未分类行（历史 kind=''）。滚动升级期三种形态都可能存在。

    A2 收口前补丁（2026-11-13）：三态按 **updated_at 取较新者**，不再单按优先级命中。
    理由：回退窗口里旧实例只写 `<p>_pat` 行，归位行会变成 stale 副本；只认归位形态
    就会把旧值读回来（用户看到"凭据莫名回退"）。同 updated_at 时归位形态优先，保持
    结果确定性。
    """
    cands = [(provider, kind or "", 0)]
    if kind:
        cands += [(f"{provider}_{kind}", "", 1), (provider, "", 2)]
    pick = None  # (updated_at, -priority, config_json)
    with db_lock:
        c = db.cursor()
        for prov, k, prio in cands:
            c.execute("SELECT config_json, COALESCE(updated_at,'') FROM user_credentials "
                      "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                      (user_id, prov, k))
            row = c.fetchone()
            if row and row[0]:
                cand = (str(row[1] or ""), -prio, row[0])
                if pick is None or cand[:2] > pick[:2]:
                    pick = cand
    if pick is None:
        return None
    return _load_credential_json(pick[2]) or None

def delete_credential(user_id: "int | str", provider: str, kind: str = "") -> bool:
    """删除凭据（A2：同时删归位行与双写期旧键行，避免残留可读副本）。"""
    try:
        keys = [(provider, kind or "")]
        legacy = _legacy_credential_key(provider, kind)
        if legacy:
            keys.append((legacy, ""))
        with db_lock:
            c = db.cursor()
            deleted = False
            for prov, k in keys:
                c.execute("DELETE FROM user_credentials "
                          "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                          (user_id, prov, k))
                deleted = deleted or c.rowcount > 0
            db.commit()
            return deleted
    except Exception as e:
        log.warning(f"Delete credential failed: {e}")
        return False

def backfill_credential_kind() -> dict:
    """A2 归位回填（幂等）：把 `<p>_pat` / `<p>_oauth` 复合键拆成 (provider=<p>, kind)。

    - 只处理 kind='' 的历史行；已有归位行（同 user_id + provider）时跳过（重复跑零写入）。
    - **保留旧复合键行**：滚动升级窗口里老实例仍按 `<p>_pat` 读；收口迁移再删。
    - 裸名凭据（huawei_cloud/codearts）：名字即凭据名，不存在类型编码，保持 kind=''。
    返回 {"pat": n, "oauth": n, "skipped": n} 供启动日志/预检。
    """
    stats = {"pat": 0, "oauth": 0, "skipped": 0}
    try:
        from src.kernel.id_gen import uuid7_str
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, user_id, provider, COALESCE(kind,''), config_json "
                      "FROM user_credentials")
            rows = c.fetchall()
            for _row_id, user_id, provider, kind, cfg in rows:
                if kind:
                    continue
                target_provider, target_kind = provider, ""
                for suffix, k in (("_pat", "pat"), ("_oauth", "oauth")):
                    if provider.endswith(suffix):
                        base = provider[:-len(suffix)]
                        if base:
                            target_provider, target_kind = base, k
                        break
                if not target_kind:
                    continue  # 裸名凭据（huawei_cloud/codearts 等）：名字即凭据，不存在类型编码
                c.execute("SELECT 1 FROM user_credentials WHERE user_id=? AND provider=? LIMIT 1",
                          (user_id, target_provider))
                if c.fetchone():
                    stats["skipped"] += 1
                    continue
                now = __import__("time").strftime("%Y-%m-%d %H:%M:%S",
                                                  __import__("time").gmtime())
                c.execute(
                    "INSERT INTO user_credentials (id, user_id, provider, kind, config_json, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (uuid7_str(), user_id, target_provider, target_kind, cfg, now, now))
                stats[target_kind] += 1
            db.commit()
        if any(stats.values()):
            log.info(f"credential kind backfill: {stats}")
        return stats
    except Exception as e:
        log.warning(f"backfill_credential_kind failed: {e}")
        raise  # 迁移约定：抛错=不盖章，下次启动重试


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
    host_step TEXT DEFAULT '',       -- Issue #2126: 托管失败步骤（invite_bot / create_webhook / ...）
    host_error TEXT DEFAULT '',      -- Issue #2126: 托管失败可读原因（不含 token/内部 URL）
    host_updated_at TEXT DEFAULT '', -- Issue #2126: host_status 最近更新时间
    bot_gitcode_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    bot_username TEXT DEFAULT '',
    host_user_id TEXT DEFAULT '',         -- Issue #372: 路径 B 托管用户 ID（无全局 bot token 时；UUID TEXT，D105 后恒 str，''=未配置）
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

def _cleanup_usage_log_retention() -> None:
    """N4-p2：usage_log 保留期清理（启动链 force=True 重启即对齐；运行期
    由 log_usage 惰性触发 + 30 天节流承接）。异常全吞（清理失败不阻断启动）。"""
    try:
        from src.kernel.persistence.credential_usage_log import cleanup_expired_retention
        _rc_n = cleanup_expired_retention(force=True)
        if _rc_n:
            log.info(f"usage_log retention cleanup: {_rc_n} rows")
    except Exception:
        pass


def init_db():
    """初始化用户相关表（幂等）。

    迁移段全部收敛到独立一次性模块（生命周期：上线=调用行，下线=删文件+删行）：
    - src/domains/mgmt/user/legacy_column_migrations.py：存量列补齐/重映射/孤儿清理/user_accounts 死表收敛
    - src/domains/mgmt/user/credentials_backfill_n1.py：N1 凭据 UUID 主键补列 + 明文加密回填
    每个模块幂等（重跑零写入），各自 docstring 记录下线判据。
    （合并重同步：weekly-08 s122 模块化结构恢复；模块覆盖面已逐标记核验，
    重复用户清理随 D32 user_accounts 死表收敛退役。）
    """
    with db_lock:
        db.execute(_USER_ACCOUNTS_DDL)
        db.execute(_CREDENTIALS_DDL)
        db.execute(_MANAGED_REPOS_DDL)
        # 兼容旧列迁移（幂等）：托管诊断列（Issue #2126）——CREATE TABLE 只影响新库
        # 域拆分库（arch 四进程）：managed_repos 归属 mgmt 基表，runtime/insight 侧是
        # 跨库视图——视图上 ALTER 必然 1347（视图侧不能 ALTER）。视图进程直接跳过，
        # 列归 owner 基表管（与迁移 fn 的视图守卫同款，避免每次启动刷 WARNING）。
        _mr_is_view = False
        from src.kernel.session_manager import _is_mysql_backend as _mr_is_mysql
        if _mr_is_mysql():
            try:
                _row = db.cursor().execute(
                    "SELECT TABLE_TYPE FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='managed_repos'").fetchone()
                _mr_is_view = bool(_row and str(_row[0]).upper() == "VIEW")
            except Exception:
                pass
        if not _mr_is_view:
            for _col in ("host_step", "host_error", "host_updated_at"):
                try:
                    db.execute(f"ALTER TABLE managed_repos ADD COLUMN {_col} TEXT DEFAULT ''")
                except Exception as _e:
                    # 幂等：列已存在属预期；其它失败需留痕（#2126 透明化方向，不外抛中断启动）
                    log.warning(f"managed_repos 补充诊断列失败 col={_col}: {_e}")
        db.execute(_OAUTH_STATES_DDL)
        from src.domains.mgmt.user.legacy_column_migrations import run as _legacy_migrations
        _legacy_migrations()
        from src.domains.mgmt.user.credentials_backfill_n1 import run as _n1_backfill
        _n1_backfill()
        # repo_key 统一整改迁移（managed_repos 去 user_id + user_repos 加前缀/is_join）
        _migrate_repo_key_tables()
        # D32 死表收敛（2026-11-07 起）：原「从 users/bot_accounts 回填 user_accounts」
        # 写入已退役——账号唯一权威表是 grape_accounts，回填只会在死表留影子数据。
        # 守卫：tests/test_user_accounts_write_freeze_guard.py（GC1/2/3 零写入）。
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
        # D32 死表收敛：原「同一 username 只保留最小 id」的 user_accounts DELETE 清扫
        # 已退役（ours@64144d06a 口径）——grape_accounts 唯一性由 UNIQUE 约束保证，
        # 死表不再需要去重写入。孤儿凭据清理保留（锚 grape_accounts，非死表）：
        try:
            from src.kernel.mysql_compat import get_db_backend as _get_be
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
    _cleanup_usage_log_retention()

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), "skills", "custom")

def ensure_skill_dir(user_id: int, skill_name: str) -> str:
    """确保技能目录存在，返回路径（个人技能）"""
    path = os.path.join(SKILL_DIR, str(user_id), skill_name)
    os.makedirs(path, exist_ok=True)
    return path

def ensure_repo_skill_dir(repo_key: str, skill_name: str) -> str:
    """确保仓库技能目录存在，返回路径（skills/repo/{owner}/{repo}/{skill}）"""
    from src.kernel.repo_key import from_repo_key
    path = os.path.join(os.path.dirname(SKILL_DIR), "repo", from_repo_key(repo_key), skill_name)
    os.makedirs(path, exist_ok=True)
    return path

def get_skill_by_id(skill_id, *, raise_on_error: bool = False) -> dict | None:
    """按 skill 表主键 id 查询 skill 信息（含 skill_name、skill_path、scope、agent_type、content_hash）

    Issue #1758: raise_on_error=True 时数据库异常上抛，供单一 scope 判官按
    fail-closed 策略处理；默认 False 维持既有吞异常返回 None 的兼容语义。
    """
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
        if raise_on_error:
            raise
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

def get_skill_hash_by_key(skill_name: str, scope: str, owner_id: str = "") -> tuple[int, str, str, int]:
    """按 (skill_name, scope, owner_id) 精确读取技能 hash（Issue #444 读时校验用）。

    (skill_name, scope, owner_id) 受 skill 表 UNIQUE 约束，唯一确定一行——
    避免 user scope 多 owner 同名时 LIMIT 1 命中不确定行（需求 backlog F3）。
    official 技能 owner_id 为空串。返回 (id, content_hash, file_path, is_active)，
    id=0 表示不存在；is_active=0 表示行已被软删除。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, content_hash, file_path, is_active FROM skill "
                "WHERE skill_name=? AND scope=? AND owner_id=? LIMIT 1",
                (skill_name, scope or "official", owner_id or ""))
            row = c.fetchone()
            if not row:
                return (0, "", "", 0)
            return (row[0], row[1] or "", row[2] or "", row[3] if row[3] is not None else 1)
    except Exception as e:
        log.warning(f"get_skill_hash_by_key failed: {e}")
        return (0, "", "", 0)

def get_skill_hash_by_id(skill_id: int) -> tuple[int, str, str, int]:
    """按主键 id 读取技能 hash（Issue #444 id 锚点校验：校验层跟 id 走）。

    返回 (id, content_hash, file_path, is_active)；id=0 表示不存在（行被删/
    删除重传换 id），调用方应强制失效缓存而非降级通过。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, content_hash, file_path, is_active FROM skill WHERE id=?",
                (skill_id,))
            row = c.fetchone()
            if not row:
                return (0, "", "", 0)
            return (row[0], row[1] or "", row[2] or "", row[3] if row[3] is not None else 1)
    except Exception as e:
        log.warning(f"get_skill_hash_by_id failed: {e}")
        return (0, "", "", 0)

def get_skill_hashes_by_name_scope(skill_name: str, scope: str) -> list[tuple[str, str]]:
    """按 (skill_name, scope) 取全部匹配行的 (owner_id, content_hash)（Issue #444 P1-1）。

    repo scope 的 DB owner_id 带 provider 前缀（gitcode:/github:/gitlab:），而对象键
    owner 段无前缀——调用侧（storage 读时校验）需拿全部行后在 Python 侧去前缀比对，
    覆盖全部 provider 口径。返回空列表表示无行。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT owner_id, content_hash FROM skill "
                "WHERE skill_name=? AND scope=? AND is_active=1",
                (skill_name, scope or "official"))
            return [(r[0] or "", r[1] or "") for r in c.fetchall()]
    except Exception as e:
        log.warning(f"get_skill_hashes_by_name_scope failed: {e}")
        return []

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

def resolve_skill_dispatch(skill_name: str, skill_id: int = 0,
                           repo_key: str = "", user_id: str | int = "",
                           scene_id: str = "", node_tag: str = "") -> dict:
    """派发技能解析（Issue #979）：skill_id 权威直查 → 名字降级 + warning。

    - skill_id 非零且 skill 表命中（is_active=1）→ 直接返回该档（显式绑定，
      三档同名下锁定用户选择的档位）；
    - skill_id 缺失（老场景仅名字）或失效（技能被删/停用）→ 按
      resolve_skill_id 现有规则（user→repo→official）降级解析，并打
      warning（含场景/节点/降级原因），行为与既有名字派发一致；
    - 两者都解析不到 → skill_id=0（调用方按 #657 scope 门禁/内置包兜底）。

    返回 {skill_id, skill_name, skill_path, degraded, skill}。
    """
    out = {"skill_id": 0, "skill_name": skill_name or "",
           "skill_path": skill_name or "", "degraded": False, "skill": None}
    if not skill_name and not skill_id:
        return out
    if skill_id:
        row = get_skill_by_id(skill_id)
        if row:
            _name = row.get("skill_name") or skill_name or ""
            out.update(skill_id=int(skill_id), skill_name=_name,
                       skill_path=row.get("skill_path") or _name, skill=row)
            return out
        log.warning(
            f"resolve_skill_dispatch: skill_id={skill_id} invalid/inactive "
            f"(scene={scene_id or '-'} node={node_tag or '-'} "
            f"skill_name='{skill_name or ''}'), degrade to name resolution")
    elif skill_name:
        log.warning(
            f"resolve_skill_dispatch: node without skill_id "
            f"(scene={scene_id or '-'} node={node_tag or '-'} "
            f"skill_name='{skill_name}'), degrade to name resolution "
            f"(user→repo→official)")
    sid = resolve_skill_id(skill_name, repo_key=repo_key, user_id=user_id)
    if sid:
        out["skill_id"] = int(sid)
        out["degraded"] = True
        row = get_skill_by_id(sid)
        out["skill"] = row
        if row:
            out["skill_name"] = row.get("skill_name") or skill_name
            out["skill_path"] = row.get("skill_path") or out["skill_name"]
    else:
        out["degraded"] = True
    return out

def get_skill_scope_info(skill_name: str, repo_key: str = "",
                         *, raise_on_error: bool = False) -> dict:
    """技能 scope 与场景（repo 级）scope 匹配判定（Issue #657）。

    按 repo → official → user（任意 owner）顺序查 skill 表：
    - 命中 repo（owner_id=repo_key）或 official → matched=True（repo 级场景可引用）；
    - 仅 user scope 存在 → found=True, matched=False
      （scope 不匹配：repo 级场景不可引用 user 级技能，无论 owner 是谁）；
    - 全部未命中 → found=False（技能不在 skill 表，如官方内置包形态，
      由调用方走内置包兜底/派发侧门禁，维持存量场景兼容）。

    返回 {found, matched, scope, skill_id, owner_id}。
    Issue #1758: raise_on_error=True 时数据库异常上抛（供单一 scope 判官
    fail-closed）；默认 False 维持吞异常返回 found=False 的兼容语义。
    """
    info = {"found": False, "matched": False, "scope": "", "skill_id": 0, "owner_id": ""}
    if not skill_name:
        return info
    try:
        with db_lock:
            c = db.cursor()
            if repo_key:
                row = c.execute(
                    "SELECT id, owner_id FROM skill WHERE skill_name=? AND scope='repo' "
                    "AND owner_id=? AND is_active=1 LIMIT 1",
                    (skill_name, repo_key)).fetchone()
                if row:
                    info.update(found=True, matched=True, scope="repo",
                                skill_id=row[0], owner_id=row[1] or "")
                    return info
            row = c.execute(
                "SELECT id FROM skill WHERE skill_name=? AND scope='official' "
                "AND is_active=1 LIMIT 1", (skill_name,)).fetchone()
            if row:
                info.update(found=True, matched=True, scope="official",
                            skill_id=row[0], owner_id="")
                return info
            row = c.execute(
                "SELECT id, owner_id FROM skill WHERE skill_name=? AND scope='user' "
                "AND is_active=1 LIMIT 1", (skill_name,)).fetchone()
            if row:
                info.update(found=True, matched=False, scope="user",
                            skill_id=row[0], owner_id=row[1] or "")
                return info
    except Exception as e:
        if raise_on_error:
            raise
        log.warning(f"get_skill_scope_info failed: {e}")
    return info

def get_skill_path(skill_id) -> str:
    """按 skill_id 返回 skill 的实际 SKILL.md 绝对路径（official/user/repo scope 处理）

    目录根按 scope 区分（与 ensure_skill_dir / ensure_repo_skill_dir 写入端一致）：
      - user  → {SKILL_DIR}/{owner_id}/{skill_path}/SKILL.md  （自定义技能目录）
      - repo  → {SKILL_DIR 同级}/repo/{owner}/{repo}/{skill_path}/SKILL.md
      - official → SKILLS_DIR（.grape/skills）随仓库分发的官方技能
    Issue #699 收口：写入端在 skills/custom，读取端曾误拼到 .grape/skills/custom →
    本地技能恒判缺失（#630 物化闸门误拦），此处对齐。
    """
    skill = get_skill_by_id(skill_id)
    if not skill:
        return ""
    from src.kernel.config import SKILLS_DIR
    sp = skill.get("skill_path") or skill.get("skill_name") or ""
    if skill.get("scope") == "user":
        return os.path.join(SKILL_DIR, str(skill.get("owner_id") or ""), sp, "SKILL.md")
    if skill.get("scope") == "repo":
        from src.kernel.repo_key import from_repo_key
        return os.path.join(SKILLS_DIR, "repo", from_repo_key(skill.get("owner_id") or ""), sp, "SKILL.md")
    # 官方技能：优先按 configs/skill-description.json 的 path 解析（skills/<agent_type>/<skill>/SKILL.md），
    # 兼容旧平铺布局（skills/<skill>/SKILL.md）
    from src.kernel.config import resolve_official_skill_path
    resolved = resolve_official_skill_path(skill.get("skill_name") or sp)
    if resolved:
        return resolved
    return os.path.join(SKILLS_DIR, sp, "SKILL.md")

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
                     category_id: int = 0, content_hash: str = "") -> int | None:
    """OBS 模式技能记录 upsert（存在则更新固定 key，不存在则插入；v2.5 file_path + by 字段）。

    scope: user/repo；owner: user_id 或 repo_key。
    Issue #179: 新增 category_id 参数。
    Issue #444: 新增 content_hash 参数（UPDATE/INSERT 补写 content_hash 列，
    读时校验据此感知跨进程覆盖更新）；返回值改为 int | None（技能主键 id，
    失败返回 None——0/None 均为 falsy，既有 `if not ok` 判断语义不变）。
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
                    "agent_type=?, description=?, category_id=?, updated_by=?, is_active=1, "
                    "content_hash=?, updated_at=datetime('now') WHERE id=?",
                    (object_key, skill_name, agent_type or "", description or "",
                     category_id, created_by or "", content_hash or "", row[0]))
                db.commit()
                return int(row[0])
            cur = c.execute(
                "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                "file_path, scope, owner_id, created_by, updated_by, is_active, category_id, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                (skill_name, skill_name, description or "", agent_type or "",
                 object_key, scope, owner, created_by or "", created_by or "", category_id,
                 content_hash or ""))
            db.commit()
            return int(cur.lastrowid or 0)
    except Exception as e:
        log.warning(f"upsert_skill_obs failed: {e}")
        return None

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
                from src.kernel.repo_key import from_repo_key
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

def add_managed_repo(repo_key: str, repo_owner: str, repo_name: str, webhook_id: int = 0,
                     bot_gitcode_token: str = "", gitcode_webhook_token: str = "",
                     bot_username: str = "", dev_scene: str = "",
                     github_webhook_token: str = "", bot_github_token: str = "",
                     org_id: int = 0, host_user_id: str = "", webhook_secret: str = "",
                     **extra_cols) -> bool:
    """写入仓库级托管配置（repo_key 带平台前缀，一仓库一行 upsert）。

    org_id > 0 时绑定到指定组织；org_id=0 时为全局（兼容旧逻辑）。
    host_user_id: Issue #372 路径 B 托管时写入当前用户 ID（无全局 bot token 场景）。
    webhook_secret: Issue #372 路径 B webhook 验证密钥（随机生成，与 token 解耦）。
    """
    from src.kernel.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo_key)
    full_name = from_repo_key(repo_key)
    # D44/S64：动态平台列（config-only 平台卡派生列，如 bot_gitee_token）
    # ——identifier 白名单校验，非法列名拒绝写入。
    # S74/D49：响亮失败（raise）——原静默 return False，而全仓 10 个调用点
    # 零返回值检查（S64 事故形状："托管成功"但配置未落库）。
    import re as _re_amr
    _extra = {}
    for _k, _v in (extra_cols or {}).items():
        if not _re_amr.match(r'^[a-z_]+$', _k or ""):
            log.warning(f"add_managed_repo: illegal extra column {_k!r}, rejected")
            raise ValueError(f"add_managed_repo: illegal extra column {_k!r}")
        _extra[_k] = _v
    # S82/D58：双写移除——grape_t_repo_credentials 成为平台凭据唯一权威写目标，
    # managed_repos 平台凭据列冻结（存量值已由 backfill 迁入；读取方
    # S80b 起全部 rc 优先）。凭据写收拢为单事务单写。
    _CRED_COLS = ("bot_gitcode_token", "gitcode_webhook_token",
                  "bot_github_token", "github_webhook_token",
                  "bot_gitee_token", "gitee_webhook_token",
                  "bot_gitlab_token", "gitlab_webhook_token")
    # F4/D106（凭据存储终态化第一批）：归一逻辑键——卡 token_storage_field
    # 为空的平台（gitee/gitlab 终态）调用方传 'bot'/'webhook'；列名键
    # （存量调用方/gitcode/github）与逻辑键统一经 _cred_type_of 归一映射。
    # 两种键形态都只落 grape_t_repo_credentials（cred_type ∈ {'bot','webhook'}，
    # 平台由 repo_key 前缀承载），不触碰 managed_repos 列。
    _LOGICAL_CRED_KEYS = ("bot", "webhook")

    def _cred_type_of(_key: str) -> str:
        # bot 列家族（bot_/bot）→ 'bot'；webhook 列家族 → 'webhook'
        if _key == "bot" or _key.startswith("bot_"):
            return "bot"
        return "webhook"

    _cred_writes = []
    for _k in list(_extra.keys()):
        if _k in _CRED_COLS or _k in _LOGICAL_CRED_KEYS:
            _cred_writes.append((_k, _extra.pop(_k) or ""))
    for _k, _v in (("bot_gitcode_token", bot_gitcode_token),
                   ("gitcode_webhook_token", gitcode_webhook_token),
                   ("bot_github_token", bot_github_token),
                   ("github_webhook_token", github_webhook_token)):
        if _v:
            _cred_writes.append((_k, _v))
    # D90-N2①（2026-11-07）：grape_t_repo_credentials cred_type 值域守卫——bot 级专属
    # （_CRED_COLS 白名单），禁 *_pat 形态（用户 PAT 权威=user_credentials 用户级，
    # 两存储面粒度/消费链不同、互不同步）。拒绝+抛错（现状零此类写入，不会误伤）。
    # 同口径覆盖 _extra：*_pat 键（PAT 语义）在本函数任何路径（rc 写/动态列）都非法。
    _bad_ct = [k for k, _ in _cred_writes if k not in _CRED_COLS and k not in _LOGICAL_CRED_KEYS]
    _bad_ct += [k for k in _extra if k.endswith("_pat")]
    _bad_ct = sorted(set(_bad_ct))
    if _bad_ct:
        raise ValueError(
            f"grape_t_repo_credentials cred_type 越界（bot 级专属，禁 *_pat）: {_bad_ct}——"
            f"用户 PAT 请走 set_credential(user_id, '<p>_pat')；新增 bot 类型须改 "
            f"_CRED_COLS 并走契约评审")
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
                _existing_host = existing[0] or ""
                # R14.15: 路径 A 成功时（调用方传 bot_token 非空 + host_user_id 空）显式清空，
                # 防止旧的 host_user_id 残留导致读取层继续走用户 token 降级。
                # 路径 B rebind 时（调用方传 host_user_id 空 + bot_token 空）保留已有值。
                if str(host_user_id or "") not in ("", "0"):
                    _final_host = host_user_id
                elif bot_gitcode_token or bot_github_token:
                    # 路径 A：显式传了 bot token → 清空 host_user_id（''=未配置哨兵，TEXT 化口径）
                    _final_host = ""
                else:
                    # 未传 bot token → 保留已有 host_user_id（路径 B rebind / scene 变更等）
                    _final_host = _existing_host
                # R14.12: webhook_secret 空串时保留已有值（防覆盖路径 B 密钥）
                _final_secret = webhook_secret if webhook_secret else (existing[1] or "")
                # S82：凭据列冻结——UPDATE 不再写 managed_repos 凭据列
                # （凭据唯一权威=grape_t_repo_credentials，存量值已 backfill 迁入）
                # D44/S64：动态列——未传的 extra 列保留已有值（与基线列保留语义一致）
                _extra_set, _extra_vals, _extra_cols_sql = {}, [], []
                if _extra:
                    try:
                        _ex_row = c.execute(
                            f"SELECT {', '.join(_extra.keys())} FROM managed_repos WHERE repo_key=?",
                            (repo_key,)).fetchone()
                    except Exception:
                        _ex_row = None
                    _ex_existing = dict(zip(_extra.keys(), _ex_row or ()))
                    for _k, _v in _extra.items():
                        _final_v = _v if _v else (_ex_existing.get(_k) or "")
                        _extra_set[_k] = _final_v
                        _extra_cols_sql.append(f"{_k}=?")
                        _extra_vals.append(_final_v)
                try:
                    c.execute(
                        "UPDATE managed_repos SET repo_owner=?, repo_name=?, full_name=?, webhook_id=?, dev_scene=?, "
                        "bot_username=?, org_id=?, host_user_id=?, webhook_secret=?"
                        f"{', ' + ', '.join(_extra_cols_sql) if _extra_cols_sql else ''} WHERE repo_key=?",
                        (repo_owner, repo_name, full_name, webhook_id, dev_scene or "",
                         bot_username, org_id, _final_host, _final_secret, *_extra_vals, repo_key))
                except Exception:
                    # 列不存在时降级（兼容未迁移的库）
                    c.execute(
                        "UPDATE managed_repos SET repo_owner=?, repo_name=?, full_name=?, webhook_id=?, dev_scene=?, "
                        "bot_username=?, org_id=? WHERE repo_key=?",
                        (repo_owner, repo_name, full_name, webhook_id, dev_scene or "",
                         bot_username, org_id, repo_key))
            else:
                # 无记录——INSERT（S82：凭据列冻结，写 ''，凭据唯一权威=grape_t_repo_credentials）
                try:
                    c.execute(
                        "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id, host_user_id, webhook_secret"
                        f"{', ' + ', '.join(_extra.keys()) if _extra else ''}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                        f"{', ' + ', '.join(['?'] * len(_extra)) if _extra else ''})",
                        (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene or "", "", "", bot_username, "", "", org_id, host_user_id, webhook_secret, *(_extra.values())))
                except Exception:
                    # 列不存在时降级
                    c.execute(
                        "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, bot_github_token, org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene or "", "", "", bot_username, "", "", org_id))
            # S82/D98/D99/F4：grape_t_repo_credentials 单写（唯一权威；cred_type 归一
            # 值域 {'bot','webhook'}，列名/逻辑键经 _cred_type_of 归一映射，
            # N3 value 加密；upsert 两步法防 UNIQUE 冲突）。
            for _k, _cv in _cred_writes:
                _upsert_repo_credential(c, repo_key, _cred_type_of(_k), _cv)
            # F4/D106 修订①（评审新增）：写 rc 成功后，同一事务内置空
            # managed_repos 对应平台 token 列（渐进收敛——每次凭据轮换
            # 自动完成一条迁移；只置空本次实际写过的 cred 种类对应的列，
            # 防误清未回填的另类历史值）。SQL 精确限定 UPDATE managed_repos
            # ——meta 表存在同名全局键 gitcode_webhook_token，严禁误伤。
            # 列存在性守卫：冷启动库无这些列，PRAGMA 探测后跳过。
            if _cred_writes:
                try:
                    _mr_cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
                    from src.kernel.repo_key import parse_repo_key as _prk_amr
                    _provider = _prk_amr(repo_key)[0]
                    _written_types = {_cred_type_of(_k) for _k, _ in _cred_writes}
                    _blank_cols = []
                    if "bot" in _written_types and f"bot_{_provider}_token" in _mr_cols:
                        _blank_cols.append(f"bot_{_provider}_token")
                    if "webhook" in _written_types and f"{_provider}_webhook_token" in _mr_cols:
                        _blank_cols.append(f"{_provider}_webhook_token")
                    if _blank_cols:
                        c.execute(
                            "UPDATE managed_repos SET "
                            + ", ".join(f"{_b}=''" for _b in _blank_cols)
                            + " WHERE repo_key=?", (repo_key,))
                except Exception as _be:
                    # 置空失败不阻断凭据主写（收敛是渐进目标；下次轮换再试）
                    log.warning(f"add_managed_repo: legacy column blank-out skipped ({_be})")
            db.commit()
            return True
    except Exception as e:
        # S74/D49 响亮失败：SQL/凭据写入硬失败一律 raise（原静默 return False
        # 与全仓调用点零返回值检查叠加，造成"托管成功但未落库"事故形状）。
        log.warning(f"Add managed repo failed: {e}")
        raise

def set_repo_scene(repo_key: str, scene_id: str):
    """写入仓库开发场景（managed_repos.dev_scene），幂等。

    issue-39 方案 B：无「default」场景，scene_id 为空即清空（仓库未配置场景）。
    """
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key
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

def set_scene_recommend_config(repo_key: str, fallback_scene: str, rules: list) -> bool:
    """写入仓库智能推荐配置（managed_repos.scene_recommend_config），幂等。

    写入时自检：过滤 scene_id 不在当前场景列表中的规则。
    #487 失败显式化：
    - 场景列表为空 → raise ValueError（调用方映射 4xx，不再静默丢保存）；
    - UPDATE 0 行 → INSERT OR IGNORE 兜底补行后再 UPDATE；
    - 返回 True=已落库，False=兜底后仍未写入（调用方显式报错）。
    """
    from src.kernel.repo_key import to_repo_key, parse_repo_key
    repo_key = to_repo_key(repo_key)
    import json

    # 自检：过滤无效 scene_id
    from src.api.pipeline_config import get_scenes
    scenes = get_scenes(repo_key=repo_key)
    if not scenes:
        raise ValueError(f"仓库 {repo_key} 场景列表为空，无法保存智能推荐配置")
    valid_ids = {s["id"] for s in scenes}
    rules = [r for r in (rules or []) if isinstance(r, dict) and r.get("scene_id") in valid_ids]
    if fallback_scene and fallback_scene not in valid_ids:
        fallback_scene = scenes[0]["id"]
        log.info(f"Set scene recommend config: fallback invalid, reset to {fallback_scene}")

    cfg = json.dumps({"fallback_scene": fallback_scene or "", "rules": rules or []}, ensure_ascii=False)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?", (cfg, repo_key))
            if not c.rowcount:
                # #487 upsert 兜底：仓库行缺失时补行（repo_key 主键幂等）再写入
                _provider, _owner, _name = parse_repo_key(repo_key)
                c.execute("INSERT OR IGNORE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                          "VALUES (?,?,?,?)",
                          (repo_key, _owner, _name, f"{_owner}/{_name}"))
                c.execute("UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?", (cfg, repo_key))
                if not c.rowcount:
                    db.rollback()
                    log.warning(f"Set scene recommend config failed: no managed_repos row for {repo_key}")
                    return False
            db.commit()
        log.info(f"Scene recommend config saved: repo={repo_key} fallback={fallback_scene} rules={len(rules)}")
        return True
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(f"Set scene recommend config failed: {e}")
        return False

def set_host_status(repo_key: str, status: str = "", step: str = "", error: str = ""):
    """写入仓库托管状态与诊断信息（managed_repos.host_status），幂等（方案 F / Issue #2126）。

    空串=托管完整；bot_missing=bot 未成功加入；webhook_missing=webhook 未创建；partial=流程部分执行。
    step/error 记录失败步骤与可读原因（不含 token/内部 URL）；status 为空串时清空诊断字段。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    _status = status or ""
    if not _status:
        step, error = "", ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE managed_repos SET host_status=?, host_step=?, host_error=?, "
                "host_updated_at=datetime('now') WHERE repo_key=?",
                (_status, step or "", error or "", repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Set host status failed: {e}")



def get_host_status(repo_key: str) -> str:
    """读取仓库托管状态（managed_repos.host_status），无记录返回空串。"""
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        try:
            if org_id and personal_org:
                c.execute(
                    "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                    "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                    "COALESCE(u.is_join,0), COALESCE(u.is_admin,0), COALESCE(m.host_status,'') "
                    "FROM managed_repos m "
                    "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                    "WHERE u.repo_key IS NOT NULL "
                    "AND (m.org_id=? OR m.org_id IN (SELECT id FROM organization WHERE is_personal=1)) "
                    "ORDER BY m.full_name", (user_id, org_id))
            elif org_id:
                c.execute(
                    "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                    "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                    "COALESCE(u.is_join,0), COALESCE(u.is_admin,0), COALESCE(m.host_status,'') "
                    "FROM managed_repos m "
                    "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                    "WHERE u.repo_key IS NOT NULL AND m.org_id=? "
                    "ORDER BY m.full_name", (user_id, org_id))
            else:
                c.execute(
                    "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
                    "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
                    "COALESCE(u.is_join,0), COALESCE(u.is_admin,0), COALESCE(m.host_status,'') "
                    "FROM managed_repos m "
                    "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
                    "WHERE u.repo_key IS NOT NULL "
                    "ORDER BY m.full_name", (user_id,))
        except Exception as e:
            log.warning(f"get_managed_repos join failed: {e}")
            c.execute(
                "SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, "
                "bot_gitcode_token, gitcode_webhook_token, bot_username, created_at, 0, 0, "
                "COALESCE(host_status,'') "
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
            "host_status": (r[11] or "") if len(r) > 11 else "",
        } for r in rows]


def update_managed_repo_admin(user_id: int, repo_key: str, is_admin: bool) -> bool:
    """更新该用户在 user_repos 的管理员状态（用户级）"""
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    _wh_url = _webhook_url_for_provider(get_webhook_url(), provider)
    if not _wh_url:
        log.warning(f"_rebuild_webhook_with_bot_token: webhook_url not configured for {repo_key}")
        return ""
    try:
        owner, repo_name = split_owner_repo(repo_key)
    except Exception:
        log.warning(f"_rebuild_webhook_with_bot_token: cannot split repo_key {repo_key}")
        return ""
    # S58/D36：删旧/建新一律走适配器端口（create_provider），无平台分支
    from src.kernel.token_resolver import create_provider
    _old_hook_id = _get_managed_webhook_id(repo_key)
    if _old_hook_id:
        try:
            create_provider(provider).remove_webhook(bot_token, owner, repo_name, _old_hook_id)
        except Exception as _re:
            log.warning(f"_rebuild_webhook_with_bot_token: remove old webhook failed for {repo_key}: {_re}")
            # 删旧失败 → 不继续建新（旧 webhook 仍在，新建会创建第二个）
            # 返回空串：调用方会清 webhook_secret 并写 bot token，
            # 旧 webhook 的实际密码（旧 secret/token）与 DB 新值不匹配 → webhook 403
            # 这是 loud fail（有日志），用户可 repair_host 恢复
            return ""
    # 用 bot token 建新 webhook（凭据形态由各 provider 端口内聚）
    try:
        return str(create_provider(provider).setup_repo_webhook(
            bot_token, owner, repo_name, _wh_url))
    except Exception as _se:
        log.error(f"_rebuild_webhook_with_bot_token: setup new webhook FAILED for {repo_key}: {_se}")
        return ""

def update_managed_repo_bot_token(repo_key: str, bot_token: str) -> dict:
    """写入仓库级 bot token（管理员手动设置 bot token 用）。

    R14.14: 同时清 host_user_id=0 + webhook_secret=''。
    R14.14: 按 repo_key 前缀分列写入。
    R14.16: 统一走 _update_repo_bot_token + _rebuild_webhook_with_bot_token，
    确保手动设置 token 后 webhook 也用 bot token 重建（同 auto_upgrade 入口）。
    R14.19: 先检查仓库存在再重建 webhook，返回 rowcount 语义（404 检测不失效）。
    FR-20.11: 返回 {"ok": bool, "warning": str, "related_repos": list} 供 API 传 warning 给前端。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # 先检查仓库是否存在于 managed_repos（不存在返回 False，调用方走 404）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            if not c.fetchone():
                return {"ok": False, "warning": "", "related_repos": []}
    except Exception as e:
        log.warning(f"update_managed_repo_bot_token: check existence failed: {e}")
        return {"ok": False, "warning": "", "related_repos": []}
    # F4b/D38/S58 复原（r3 集成树合并回归修复）：provider 从 repo_key 前缀
    # 解析（parse_repo_key）——原硬编码 github/gitcode 把 gitee/gitlab 仓
    # 错路由到 gitcode 列；解析失败回退存量二值口径。
    # S58/D36 守卫口径：provider 一律 repo_key 前缀派生（parse_repo_key 是纯函数，
    # 裸 key 自带存量默认；不再保留平台字面量二次回退）
    from src.kernel.repo_key import parse_repo_key as _prk_umr
    provider = _prk_umr(repo_key)[0]
    # FR-6.2: 检测共用 bot token 的关联仓库（信任域影响提示）
    _related_repos = []
    try:
        # D38/S58：列名走适配器卡派生（内置卡=存量列名，config-only 平台=bot_<p>_token）
        from src.kernel.token_resolver import create_provider
        _bot_token_col = create_provider(provider).token_storage_field()
        import re as _re
        # F4/D106：卡列空平台（gitee/gitlab 终态）无列可查——改查 grape_t_repo_credentials
        # （rc 唯一权威；密文+明文双形态比对与列路径同口径）。
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _bot_token_col or ""):
            _bot_token_col = ""
        with db_lock:
            c = db.cursor()
            # N3：列值已加密——比对值须同形态（明文兼容期列里可能新旧混合，
            # 两种形态都查：密文等值 + 明文等值）
            _enc = _encrypt_repo_secret(bot_token)
            if _bot_token_col:
                _q = (f"SELECT repo_key FROM managed_repos "
                      f"WHERE ({_bot_token_col}=? OR {_bot_token_col}=?) AND repo_key!=?")
                c.execute(_q, (_enc, bot_token, repo_key))
            else:
                _q = ("SELECT repo_key FROM grape_t_repo_credentials "
                      "WHERE cred_type='bot' AND (value=? OR value=?) "
                      "AND repo_key LIKE ? AND repo_key!=?")
                c.execute(_q, (_enc, bot_token, f"{provider}:%", repo_key))
            _related_repos = [r[0] for r in c.fetchall()]
    except Exception:
        pass
    if _related_repos:
        log.info(f"update_managed_repo_bot_token: {repo_key} bot token shared with {len(_related_repos)} repos: {_related_repos}")
    # 重建 webhook（删旧 + 建新），失败时仍写 DB（webhook_id 为空，用户可手动 repair）
    _new_hook_id = _rebuild_webhook_with_bot_token(repo_key, bot_token, provider)
    _warning = ""
    if not _new_hook_id:
        log.warning(f"update_managed_repo_bot_token: webhook rebuild failed for {repo_key}, "
                    f"DB written but webhook may be stale — user should run repair_host")
        _warning = "webhook 重建失败，请到仓库页执行重检修复"
    # 统一入口：写 token + 清 host_user_id + 清 webhook_secret + 写 webhook_id
    _update_repo_bot_token(repo_key, bot_token, provider,
                           webhook_token=bot_token, webhook_id=_new_hook_id)
    return {"ok": True, "warning": _warning, "related_repos": _related_repos}

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

def resolve_managed_repo_key(repo_owner: str, repo_name: str) -> str:
    """owner/repo → 带前缀 repo_key（按 managed_repos 候选集探针解析真实前缀）。

    P9 抽取自 get_bot_token_by_repo 的探针段——单仓 bot-token 端点与
    token 读取链共用同一解析，避免两处前缀口径漂移。
    """
    from src.kernel.repo_key import to_repo_key, repo_key_candidates
    repo_key = to_repo_key(f"{repo_owner}/{repo_name}")
    # 实弹验证 2026-11-10：裸 to_repo_key 默认 gitcode: 前缀，gitee/github 仓查错行。
    # 候选集探针按 managed_repos 解析真实前缀（幂等）。
    try:
        _rks = tuple(repo_key_candidates(f"{repo_owner}/{repo_name}"))
        if _rks:
            with db_lock:
                _row = db.cursor().execute(
                    f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))}) LIMIT 1",
                    _rks).fetchone()
            if _row and _row[0]:
                repo_key = _row[0]
    except Exception:
        pass
    return repo_key


def repo_has_bot_credential(repo_key: str) -> bool:
    """P9：per-repo bot 凭据存在性判定（repo_key 前缀 → 卡派生，读序=卡列→rc 单表→
    存量列回落，与 get_bot_token_by_repo 同一实现，消除双口径）。

    - 卡 token_storage_field 非空（gitcode/github 存量列）→ 查 managed_repos 对应列；
    - 卡 token_storage_field 为空（gitee/gitlab 终态，无物理列）→ 查
      grape_t_repo_credentials（repo_key × cred_type='bot'）；
    - 【存量列回落】rc 未命中/表不存在（老部署冷启动库无 rc 表）时，若
      managed_repos 存在遗留列 bot_<provider>_token（gitee 老数据形态，live-w10
      实测 gitee bot 凭据落在旧列），读该列——与白名单行式化「行式双读存量列」
      同原则：终态口径不能让存量数据失真。
    - 路径 B 仓库（host_user_id > 0）无仓库级 bot token——同 get_bot_token_by_repo 口径。
    """
    return bool(_repo_bot_token_value(repo_key))


def _repo_bot_token_value(repo_key: str) -> str:
    """仓库 bot token 读序实现（卡列 → rc 单表 → 存量列回落）。

    repo_has_bot_credential（存在性）与 get_bot_token_by_repo（取值）共用，
    单一读序单一来源。读不到返回 ''。
    """
    # R12.12 同口径：路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）
    if str(get_host_user_id(repo_key) or "") not in ("", "0"):
        return ""
    from src.kernel.repo_key import parse_repo_key
    provider = parse_repo_key(repo_key or "")[0] or "gitcode"
    _col = ""
    try:
        from src.kernel.token_resolver import create_provider
        _col = create_provider(provider).token_storage_field()
    except Exception:
        _col = ""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _col or ""):
        _col = ""
    with db_lock:
        c = db.cursor()
        # ① 卡声明列（gitcode/github 终态）
        if _col:
            try:
                c.execute(
                    f"SELECT {_col} FROM managed_repos "
                    f"WHERE repo_key=? AND {_col}!='' LIMIT 1", (repo_key,))
                row = c.fetchone()
                if row:
                    return _decrypt_repo_secret(row[0] or "")
            except Exception:
                pass
        # ② rc 单表（空卡列平台终态；老库无表→吞异常走③）
        try:
            c.execute(
                "SELECT value FROM grape_t_repo_credentials "
                "WHERE repo_key=? AND cred_type='bot' LIMIT 1", (repo_key,))
            row = c.fetchone()
            if row:
                return _decrypt_repo_secret(row[0] or "")
        except Exception:
            pass
        # ③ 存量列回落（空卡列平台的老数据形态：gitee bot 凭据落 bot_gitee_token 列）
        _legacy = f"bot_{provider}_token"
        if _legacy != _col:
            try:
                c.execute(
                    f"SELECT {_legacy} FROM managed_repos "
                    f"WHERE repo_key=? AND {_legacy}!='' LIMIT 1", (repo_key,))
                row = c.fetchone()
                if row:
                    return _decrypt_repo_secret(row[0] or "")
            except Exception:
                pass
        return ""


def get_bot_token_by_repo(repo_owner: str, repo_name: str) -> str:
    """按 owner/repo 查仓库 bot token（内部按带前缀 repo_key 命中）。

    R12.12: 路径 B 仓库（host_user_id > 0）守卫下沉到此处——
    所有调用方（scenes/feishu_async/chat/agents/session_store/settings 等 12 处）
    自动继承路径 B 跳过逻辑，与 get_repo_token_with_refresh / get_repo_bot_token 对齐。
    """
    repo_key = resolve_managed_repo_key(repo_owner, repo_name)
    # P9 收口：取值与存在性同一实现（卡列→rc 单表→存量列回落，含路径 B 守卫），
    # 消灭本函数写死的 bot_gitcode_token 列读（gitee/gitlab 老列数据此前恒空）
    return _repo_bot_token_value(repo_key)

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
    from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
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
            # D92：文件 fallback 命中必须告警——文件是隐形的第二权威源，
            # 静默生效会让"meta 被误删/迁移未完成"不可见（与 D91 GlobalBot
            # 缺失告警同一原则：配置态可观测）。命中≠错误（迁移期合法），
            # 但下线判定需要知道还有多少流量走文件。
            if val:
                from src.kernel.config import log
                log.warning(
                    f"global bot token from FILE fallback (meta key={key} empty, "
                    f"migrated={_migrated}): 隐形第二权威源命中——若迁移已完成请检查 "
                    f"meta 值；下线文件 fallback 前需确认此告警不再出现"
                )
    if val:
        _global_bot_cache[key] = (val, now + _GLOBAL_BOT_CACHE_TTL)
        return val
    # D22：空值不缓存——admin 写库后 60s 内不再被陈旧空缓存顶住
    # （空值读取直接穿透 DB，代价可忽略；文件 fallback 结果同样不缓存空）
    _global_bot_cache.pop(key, None)
    return ""

def _invalidate_global_bot_cache():
    """清除全局 bot token 缓存（admin 改配置后调用）。"""
    _global_bot_cache.clear()

def _read_gbt_file_field(field: str) -> str:
    """从 global-bot-token.json 读取指定字段（文件 fallback 用）。"""
    import json, os
    try:
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
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
        _gbt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
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
    """统一入口：github 卡 bot_username 优先 → meta 兜底（D15，2026-11-12）。

    B 层收敛：GitHub bot 身份归仓库平台卡字段（get_platform_card("github").bot_username），
    存量 meta global_bot_github_username（+文件 fallback）保留为卡空时的兜底
    （兼容窗口）。消费方（event_pipeline self-comment / repos 解绑协作者）零改动。

    Issue #345: DB 是权威源。文件 fallback 确保迁移失败或集群竞态时系统仍可用。
    """
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _card = get_platform_card("github")
        if _card is not None and _card.bot_username:
            return _card.bot_username
    except Exception:
        pass
    return _cached_get_meta("global_bot_github_username",
                            file_fallback_fn=lambda: _read_gbt_file_field("github_bot_username"))

def get_global_bot_usernames() -> list[str]:
    """统一入口：各卡 bot_username 纯卡派生并集（D16→D22，2026-11-12 拍板）。

    self-comment 检测的 bot 名单 = list_vcs_platforms() 各卡 bot_username
    非空并集（新平台卡配 bot 自动进名单）。meta global_bot_usernames 兜底
    读链按用户拍板退役；存量 meta 键成死配置，清理挂账下批。
    """
    out: list[str] = []
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms, get_platform_card
        for _p in list_vcs_platforms():
            _card = get_platform_card(_p)
            if _card is not None and _card.bot_username and _card.bot_username not in out:
                out.append(_card.bot_username)
    except Exception:
        pass
    return out

def _get_global_bot_token() -> str:
    """读取全局 GitCode bot token——向后兼容包装。

    Issue #345: 内部改为调用统一入口 get_global_gitcode_bot_token()。
    保留原函数名避免大量调用方改动。
    """
    return get_global_gitcode_bot_token()

def get_gitcode_token(repo_key: str = "", user_id: str = "") -> str:
    """统一获取 GitCode API Token。

    Issue #372: 内部改为调 get_repo_token_with_refresh（统一入口），
    保留 user_id 参数兼容原有调用方。

    - repo_key 非空 → get_repo_token_with_refresh（四级 fallback + 用户 token 降级）
    - str(user_id or "") not in ("", "0") → user token（通过 get_user_gitcode_token 从 external_identities 读）
    """
    if repo_key:
        return get_repo_token_with_refresh(repo_key)
    if str(user_id or "") not in ("", "0"):
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
    from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    resolver = TokenResolver(provider)
    # R18：跳过第 1 级 session token（DevopsAgentSessionToken）——D101 方案三该级
    # 退役后 resolver 策略已为 [RepoBot, Global, User]，原 strategies[1:] 切片会
    # 误跳 RepoBot 级（bot token 静默丢失）。按类名过滤，两代 resolver 均正确。
    resolver.strategies = [s for s in resolver.strategies
                           if type(s).__name__ != "DevopsAgentSessionToken"]
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
                from src.domains.mgmt.user.db import get_github_token
                return get_github_token(repo_key=repo_key) or ""
            else:
                from src.domains.mgmt.user.db import get_repo_token_with_refresh
                return get_repo_token_with_refresh(repo_key) or ""
        except Exception:
            pass
    return ""

def get_host_user_id(repo_key: str) -> "str | int":
    """Issue #372: 查 managed_repos.host_user_id（路径 B 托管用户 ID）。

    D105 后 id 域为 uuid 字符串：返回 str(host_user_id)（D105 uuid TEXT），
    未配置/仓库不存在返回 0（历史哨兵；调用方统一用
    str(x or "") not in ("", "0") 判定，''/'0'/0 均视为未配置）。
    """
    from src.kernel.repo_key import to_repo_key
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
            if row and str(row[0] or "") not in ("", "0"):
                return str(row[0])
    except Exception as e:
        log.warning(f"get_host_user_id: query managed_repos failed: {e}")
    return 0

def get_webhook_secret(repo_key: str) -> str:
    """Issue #372: 查 managed_repos.webhook_secret（webhook 验证密钥，与 token 解耦）。

    返回 webhook_secret 字符串，空字符串表示未配置。
    """
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import to_repo_key
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
        # S85/D60③/S59：provider 一律由 repo_key 前缀派生（parse_repo_key，
        # 不枚举平台字面量）——原二元 `github if startswith("github:") else
        # gitcode` 会把 gitee:/gitlab: 仓折叠成 gitcode，host 用户名查询静默落空。
        from src.kernel.repo_key import parse_repo_key
        provider = parse_repo_key(repo_key)[0]
    host_user_id = get_host_user_id(repo_key)
    if str(host_user_id or "") not in ("", "0"):
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
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key or not login:
        return
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET host_vcs_username=? WHERE repo_key=? AND (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0')",
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
    from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).get_token(repo_key)


def get_repo_token_for(repo_key: str) -> str:
    """仓库 token 统一读入口（2026-11-13 if/else 收口配套）：

    单一入口=provider.resolve_repo_token（GenericVcsProvider 基类能力）：
    TokenResolver 标准策略链 + 卡能力位 owner_identity_token_fallback 派生的
    owner-identity 回落（github 卡声明，新平台卡内声明即得）。
    消费方（qa_gate/issues 路由/issue_vcs_index/event_pipeline）不再按
    平台名分叉选 getter——本函数自身也零平台分支。
    """
    from src.kernel.token_resolver import create_provider_from_repo_key
    _prov = create_provider_from_repo_key(repo_key)
    try:
        return _prov.resolve_repo_token(repo_key) or ""
    except Exception as e:
        log.warning(f"get_repo_token_for failed (repo_key={repo_key}): {e}")
        return ""

def _is_user_token_repo(repo_key: str) -> bool:
    """Issue #372 R2.4: 判断仓库是否走路径 D（用户 token 降级）。

    前三级（devops_agent_session / managed_repos.bot_gitcode_token / 全局 bot）
    全为空且 host_user_id > 0 → True。用于 task 创建时区分 token 来源——
    用户 OAuth token 会过期，不应写入 task 行，应由运行时实时获取。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return False
    # R22.2.5: 路径 B 仓库（host_user_id > 0）跳过 session token 查询——
    # 与 DevopsAgentSessionToken._fetch 的 host_user_id 守卫对齐（R22.2.4）。
    # 防止路径 A→B 降级后 session token 残留导致 _is_user_token_repo 误返回 False。
    _hid = get_host_user_id(repo_key)
    if str(_hid or "") not in ("", "0"):
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
    # 路径 D: host_user_id 非空哨兵 → 用户 token
    return str(_hid or "") not in ("", "0")

def _is_github_user_token_repo(repo_key: str) -> bool:
    """Issue #372 R2.4: 判断 GitHub 仓库是否走用户 token 降级路径。

    GitHub get_github_token fallback：managed_repos.bot_github_token →
    owner external_identities → 全局 → host_user_id 用户 token。
    前三级全为空且 host_user_id > 0 → True。
    """
    from src.kernel.repo_key import to_repo_key, parse_repo_key
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
            from src.kernel.repo_key import parse_repo_key as _prk
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
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
    # 4. host_user_id 非空哨兵 → 用户 token
    return str(_hid or "") not in ("", "0")

def get_github_token(repo_key: str = "") -> str:
    """统一获取 GitHub API Token（对应 get_gitcode_token 的 GitHub 版）。

    R18: 内部委托 TokenResolver（GitHubProvider），保留 D32-补丁 fallback。
    D32-补丁：TokenResolver 返回空时查仓库 owner 的 GitHub identity（标准策略链不含此层）。

    - repo_key 非空 → bot token：managed_repos.bot_github_token → 全局 → 用户 token 降级
    - 无 repo_key → 空（用户级 token 走 get_user_github_token）
    """
    from src.kernel.token_resolver import TokenResolver, create_provider
    token = TokenResolver(create_provider("github")).get_token(repo_key)
    if token:
        return token
    # D32-补丁：TokenResolver 返回空时，查仓库 owner 的 GitHub identity
    try:
        from src.kernel.repo_key import parse_repo_key, to_repo_key
        repo_key = to_repo_key(repo_key, provider="github")
        if not repo_key:
            return ""
        _prov, owner, _name = parse_repo_key(repo_key)
        if owner:
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
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
            # D43/S63：列名与前缀子句卡/目录派生——token 列走适配器；
            # 默认平台（内置卡序首）含裸键（无前缀存量数据），其余平台按前缀
            from src.kernel.token_resolver import create_provider as _cp_pb
            _tok_col = _cp_pb(provider).token_storage_field()
            _prefix_clause = _path_b_prefix_clause(provider)
            # F4/D106: card column empty (gitee/gitlab final state) — path B determination changed to
            # "no non-empty bot row in rc" (NOT EXISTS; decryption comparison is not usable in SQL,
            # empty-value rows are treated as absent, consistent with the read-side rc semantics).
            if not _tok_col:
                c.execute(
                    "SELECT repo_key, repo_owner, repo_name, host_user_id FROM managed_repos "
                    f"WHERE (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0') "
                    f"AND {_prefix_clause} "
                    "AND NOT EXISTS (SELECT 1 FROM grape_t_repo_credentials rc "
                    "  WHERE rc.repo_key = managed_repos.repo_key AND rc.cred_type='bot' "
                    "  AND rc.value IS NOT NULL AND rc.value != '')"
                )
                rows = c.fetchall()
                return [
                    {"repo_key": r[0], "repo_owner": r[1], "repo_name": r[2], "host_user_id": r[3]}
                    for r in (rows or [])
                ]
            c.execute(
                "SELECT repo_key, repo_owner, repo_name, host_user_id FROM managed_repos "
                f"WHERE ({_tok_col} IS NULL OR {_tok_col}='') "
                f"AND (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0') AND {_prefix_clause}"
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
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # FR-23.1: webhook_id 列是 INTEGER DEFAULT 0，空串写 MySQL 整型列报 1366
    # 统一转 int：空串/None/0 → 0（无 webhook），非零字符串 → int
    _wh_id = 0
    if webhook_id:
        try:
            _wh_id = int(webhook_id)
        except (ValueError, TypeError):
            _wh_id = 0
    try:
        from src.kernel.token_resolver import create_provider
        _prov = create_provider(provider)
        _tok_col = _prov.token_storage_field()
        _wt_col = _prov.webhook_token_field()
        import re as _re
        # F4/D106（凭据存储终态化第一批）：卡字段为空（gitee/gitlab 终态）时
        # 无物理列可写——列写分支跳过，凭据单写 rc（'bot' + 'webhook' 两行，
        # webhook 值沿用列时代语义 webhook_token or bot_token），并在同一事务
        # 置空存量库遗留平台列（渐进收敛，SQL 精确限定 UPDATE managed_repos，
        # 严禁触碰 meta 同名全局键）。有列平台（gitcode/github）行为不变。
        _card_cols = bool(_tok_col and _wt_col)
        if _card_cols:
            for _col in (_tok_col, _wt_col):
                if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _col or ""):
                    raise ValueError(f"非法存储列名: {_col!r}")
        with db_lock:
            c = db.cursor()
            if _card_cols:
                # R14.1: 同步写入 <p>_webhook_token=bot_token, webhook_id, 清 webhook_secret
                # R16.3.3: 清 bot_token_status（N3：列值通道加密）
                c.execute(
                    f"UPDATE managed_repos SET {_tok_col}=?, host_user_id='', webhook_secret='', "
                    f"bot_token_status='', {_wt_col}=?, webhook_id=? WHERE repo_key=?",
                    (_encrypt_repo_secret(bot_token), _encrypt_repo_secret(webhook_token or bot_token),
                     _wh_id, repo_key),
                )
            else:
                # F4/D106：rc 单写平台的基线列更新（token 值不进列）
                c.execute(
                    "UPDATE managed_repos SET host_user_id='', webhook_secret='', "
                    "bot_token_status='', webhook_id=? WHERE repo_key=?",
                    (_wh_id, repo_key),
                )
            # D98→D99（2026-11-08）：补写 rc——手动设置/auto_upgrade 路径 A 的
            # rc 权威面同步（any_repo_credential_for_repos 等 rc-only 读者）。
            # cred_type 归一='bot'（D99 值域，无平台名）；两步法 upsert
            # （_upsert_repo_credential，唯一约束竞态 retry UPDATE）。
            # F4/D106：rc 单写平台（卡列空）同时落 'webhook' 行（对齐列时代
            # {_wt_col}=webhook_token or bot_token 语义）；有列平台 webhook 值
            # 仍在列（回落面不变），rc webhook 行按 D99 收口语义仅在非空时补。
            _upsert_repo_credential(c, repo_key, "bot", bot_token)
            # D99 收口补漏：webhook token 同步写 rc（cred_type='webhook'，N3 加密）——
            # 全局换发链同仓换 webhook token，rc 权威面不能只覆盖 bot 一类
            _wv = webhook_token or bot_token or ""
            if _wv:
                _upsert_repo_credential(c, repo_key, "webhook", _wv)
            # F4/D106（评审新增①）：rc 单写平台（卡列空）写 rc 成功后，同一事务
            # 置空 managed_repos 存量平台 token 列（渐进收敛；列不存在即跳过——
            # 冷启动库零命中）。SQL 精确限定 UPDATE managed_repos，严禁触碰
            # meta（meta 有同名全局键 gitcode_webhook_token，非本域）。
            if not _card_cols:
                try:
                    _mr_cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
                    _blank = [f"bot_{provider}_token", f"{provider}_webhook_token"]
                    _blank = [b for b in _blank if b in _mr_cols]
                    if _blank:
                        c.execute(
                            "UPDATE managed_repos SET "
                            + ", ".join(f"{_b}=''" for _b in _blank)
                            + " WHERE repo_key=?", (repo_key,))
                except Exception as _be:
                    log.warning(f"_update_repo_bot_token: legacy column blank-out skipped ({_be})")
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
            # D39/S57：host token 统一入口——provider 由本函数参数携带
            # （gitee 不折叠 gitcode；github/gitcode 与 D37 等价锁覆盖）
            host_user_id = repo["host_user_id"]
            host_token = ""
            from src.api.deps import get_user_token_for
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
            acc = get_grape_account_by_id(host_user_id)
            if acc and acc.get("username"):
                host_token = get_user_token_for(acc["username"], host_user_id, provider=provider) or ""
            if not host_token:
                skipped += 1
                continue  # host user token 过期，跳过

            # 邀请 bot 为协作者（D39/S59：走适配器端口，权限默认值/
            # 邀请-接受形态由各 provider 内聚——Gitee 系 maintainer 邀请制、
            # GitHub push 邀请制、generic 直加平台无确认步骤；调用方零平台分支）
            owner = repo["repo_owner"]
            repo_name = repo["repo_name"]
            from src.kernel.token_resolver import create_provider
            _prov = create_provider(provider)
            if not _prov.add_collaborator(host_token, owner, repo_name, bot_username):
                skipped += 1
                log.warning(f"auto_upgrade: add_collaborator failed for {repo['repo_key']} (provider={provider}), skipping")
                continue
            # R12.2/R12.8: 邀请制平台需显式 accept，失败则 skip 不写 token
            if not _prov.accept_collaborator_invite(bot_token, owner, repo_name):
                skipped += 1
                log.warning(f"auto_upgrade: accept_collaborator_invite failed for {repo['repo_key']} (provider={provider}), skipping")
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
    # FR-10.2: 升级完成通知——记审计日志，受影响仓库 host 可通过日志感知语义变更
    if upgraded > 0:
        log.info(f"auto_upgrade_path_b_repos: {upgraded} repos upgraded from path B to path A for {provider}. "
                 f"Host comments will no longer be self-loop filtered. Webhook rebuilt with bot token.")

def get_agent_bot_token(repo_key: str) -> str:
    """获取 Agent 执行的 GitCode Bot Token（兼容包装，统一走 get_gitcode_token）"""
    return get_gitcode_token(repo_key=repo_key)

def get_user_gitcode_token(user_id: str) -> str:
    """获取用户的 GitCode API token（薄委托 get_user_platform_token，S87/D63）。

    D105 ID-conversion：user_id 为 grape_accounts TEXT 主键（uuid 串），
    防御性接受 int 兼容形态，由 get_user_platform_token 统一判空。
    """
    return get_user_platform_token(user_id, "gitcode")

def get_user_github_token_by_id(user_id: str) -> str:
    """获取用户的 GitHub API token（薄委托 get_user_platform_token，S87/D63）。

    Issue #343: 原名 get_user_github_token(user_id: int) 与 deps.py 的
    get_user_github_token(username: str) 同名不同签名，极易混淆。
    改名为 get_user_github_token_by_id 消除歧义。
    保留 get_user_github_token 作为别名向后兼容（内部调用）。
    """
    return get_user_platform_token(user_id, "github")

def get_user_github_token(user_id: str) -> str:
    """向后兼容别名，委托给 get_user_github_token_by_id。"""
    return get_user_github_token_by_id(user_id)

def _get_username_by_id(user_id: int) -> str:
    """根据 user_id 查 username（D32-补丁：查 grape_accounts，不查 user_accounts）。"""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
        acc = get_grape_account_by_id(user_id)
        return acc["username"] if acc else ""
    except Exception:
        return ""

def refresh_gitcode_oauth_token(user_id: int) -> str | None:
    """用 refresh_token 刷新 GitCode OAuth access_token（Issue #52）。

    D32-补丁：委托给通用 refresh_oauth_token(user_id, "gitcode")，消除代码重复。
    """
    return refresh_oauth_token(user_id, "gitcode")

def get_webhook_token_by_repo(full_name: str, provider: str = "") -> str:
    """根据 full_name（owner/repo）查找 repo 的预期 webhook token（按 provider 分流）。

    Issue #372: 优先返回 webhook_secret（路径 B 随机密钥，与 token 解耦），
    为空时 fallback 到 gitcode_webhook_token / github_webhook_token（路径 A 兼容）。
    扫雷⑦（中危④）：provider 给定时精确平台行单查（gitcode:/github:/gitee:…），
    跨平台同名仓不互串预期 token；secret 列 → 平台 token 列（gitcode/github）→
    grape_t_repo_credentials('webhook') 逐级回落，全链只碰本平台 repo_key。
    """
    from src.kernel.repo_key import to_repo_key
    with db_lock:
        c = db.cursor()
        if provider:
            _pk = to_repo_key(full_name, provider=provider)
            try:
                c.execute("SELECT webhook_secret FROM managed_repos WHERE repo_key=? LIMIT 1", (_pk,))
                row = c.fetchone()
            except Exception:
                row = None
            if row and row[0]:
                return row[0]
            # 2026-11-13 if/else 收口：平台 webhook token 存量列经卡能力位
            # webhook_token_field 派生（空=无列平台，gitee/gitlab F4 终态）——
            # 消除 `provider in (gitcode,github)` 二元枚举与列名映射硬编码；
            # 有列平台（gitcode/github 派生列名）行为不变。
            try:
                from src.domains.runtime.vcs.platform_card import get_platform_card
                _wt_col = (getattr(get_platform_card(provider), "webhook_token_field", "") or "") \
                    if provider else ""
                if _wt_col:
                    c.execute(
                        f"SELECT {_wt_col} FROM managed_repos "
                        "WHERE repo_key=? LIMIT 1", (_pk,))
                    row = c.fetchone()
                    if row and row[0]:
                        return _decrypt_repo_secret(row[0])
            except Exception:
                pass
            return get_repo_credential_value(_pk, "webhook")
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
                # S85/D60②：列冻结补漏——S82 后新仓凭据只在 rc
                _rc_w = get_repo_credential_value(gc_key, "webhook")  # D99 归一（平台由 repo_key 承载）
                if _rc_w:
                    return _rc_w
                return ""
            # N3：列值通道加密，读侧解密
            return _decrypt_repo_secret(row[1] or row[0] or "")
        if not row:
            # S85/D60②：列冻结补漏（gitcode/github 侧）——S82 后新仓凭据只在 rc
            _rc_w = get_repo_credential_value(gc_key, "webhook")  # D99 归一
            if _rc_w:
                return _rc_w
            return ""
        # 优先 webhook_secret（路径 B），fallback 到 webhook_token（路径 A）
        webhook_secret = row[0] or ""
        if webhook_secret:
            return webhook_secret
        # GitHub 仓库有 github_webhook_token，GitCode 有 gitcode_webhook_token
        # N3：列值通道加密，读侧解密；空值时 S85 列冻结补漏——rc 回落
        _tok = _decrypt_repo_secret(row[2] or row[1] or "")
        if not _tok:
            _tok = get_repo_credential_value(gc_key, "webhook")  # D99 归一
        return _tok

def remove_managed_repo(repo_key: str) -> dict | None:
    """删除仓库级托管行，返回被删除的记录（含 webhook_id）或 None；同时复位所有用户 is_join。

    R19.3.4: 删除仓库时清理 devops_agent_session 中的 token 字段。
    """
    from src.kernel.repo_key import from_repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT full_name, webhook_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if not row:
                return None
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("UPDATE user_repos SET is_join=0 WHERE repo_key=?", (repo_key,))
            # D101 方案三（T5）：解绑清理段删除——repo_gitcode_token/repo_github_token
            # 列已退役（无写入方），无物可清；token_status 语义随 session 级消亡。
            db.commit()
            return {"full_name": from_repo_key(repo_key), "webhook_id": row[1]}
    except Exception as e:
        log.warning(f"Remove managed repo failed: {e}")
        return None

def get_all_managed_repos() -> list[dict]:
    """Admin 用：获取所有托管记录（repo_key 带前缀，full_name 无前缀）"""
    from src.kernel.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, created_at, host_status"
                  " FROM managed_repos ORDER BY full_name")
        return [{"repo_key": r[0], "repo_owner": r[1], "repo_name": r[2],
                 "full_name": from_repo_key(r[0]), "webhook_id": r[4], "created_at": r[5],
                 "host_status": r[6] or ""} for r in c.fetchall()]

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
        from src.kernel.config import read_server_config
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

def _server_base_url_configured_value() -> str:
    """读取显式配置的对外基址（server-config.json > meta），空串表示未配置。

    Issue #455：与 get_server_base_url 不同，本函数不做 localhost 兜底，
    用于判断「用户是否显式配置了对外可访问基址」。
    """
    try:
        from src.kernel.config import read_server_config
        _sb = (read_server_config().get("server_base_url") or "").strip().rstrip("/")
        if _sb:
            return _sb
    except Exception:
        pass
    try:
        return (get_meta_value("server_base_url") or "").strip().rstrip("/")
    except Exception:
        return ""

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")

def is_server_base_url_configured() -> bool:
    """对外基址是否已显式配置（非空且非本机回环地址）。

    Issue #455：未配置时 OAuth 回调/安装命令会指向 localhost，第三方平台无法回访，
    应在启动日志与健康检查中给出告警。
    Issue #471 E1（QA Required）：配置检查纳入拆分项——浏览器面认
    browser_callback_url、worker 面认 worker_connect_url，任一拆分项
    已配置（非回环）即视为已配置（双地址部署只配拆分项不再误报）。
    """
    if _split_face_public("browser_callback_url") or _split_face_public("worker_connect_url"):
        return True
    base = _server_base_url_configured_value()
    if not base:
        return False
    _host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return _host not in _LOOPBACK_HOSTS

def _split_face_public(key: str) -> bool:
    """拆分配置项（browser_callback_url/worker_connect_url）是否已配置且非回环。"""
    _v = _split_url_configured_value(key)
    if not _v:
        return False
    _host = _v.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return _host not in _LOOPBACK_HOSTS

def _split_face_warning(key: str) -> str:
    """单个拆分项指向回环时的告警文案（面向对应语义：浏览器回调/worker 回连）。"""
    _v = _split_url_configured_value(key)
    _host = _v.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    _usage = "浏览器回调" if key == "browser_callback_url" else "worker 回连"
    return (
        f"{key} 当前指向本机回环地址 {_v}，{_usage}对外不可达；"
        f"请在 configs/server-config.json 配置对外可访问地址后重启。"
    )

def get_server_base_url_warning() -> str:
    """未显式配置对外基址时的告警文案；已配置返回空串。

    Issue #471 E1（QA Required）：告警按面拆分——
    - 任一拆分项（browser_callback_url / worker_connect_url）或 server_base_url
      已配置且非回环 → 无告警；
    - 拆分项/旧值指向回环 → 按对应面输出回环告警（引导改对应配置项）；
    - 全未配置 → 保留「未配置」告警（提示配 server_base_url 或两个拆分项均可）。
    """
    # 逐面检查拆分项：指向回环的面对出回环告警
    for _key in ("browser_callback_url", "worker_connect_url"):
        if _split_url_configured_value(_key) and not _split_face_public(_key):
            return _split_face_warning(_key)
    # 任一面已公网配置 → 无告警（含拆分项，双地址部署不误报）
    if is_server_base_url_configured():
        return ""
    base = _server_base_url_configured_value()
    if base:
        return (
            "server_base_url 当前指向本机回环地址 " + base +
            "，OAuth 回调与安装命令对第三方平台不可达；"
            "请在 configs/server-config.json 配置对外可访问基址（如 https://grape.example.com）后重启。"
        )
    return (
        "未配置对外可访问基址（server_base_url 或拆分项 browser_callback_url/worker_connect_url）："
        "OAuth 回调与安装命令将默认指向 http://localhost:3000，"
        "第三方平台无法回访导致登录/托管不可用；"
        "请在 configs/server-config.json 配置 server_base_url（如 https://grape.example.com）"
        "或分别配置 browser_callback_url / worker_connect_url 后重启。"
        "配置示例见 configs/server-config.example.json。"
    )

def get_webhook_url() -> str:
    """返回服务端 webhook 地址（GitCode 事件回调注册用，含 /webhook/gitcode 后缀）。

    优先取 configs/server-config.json 显式配置；无则 meta.webhook_url；再兜底空串
    （不设 localhost 兜底，让 repos.py「webhook_url 未配置即拒」守卫生效）。
    """
    try:
        from src.kernel.config import read_server_config
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
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), "configs")

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
                # 写入冻结铁律（2026-11-07）：原 DELETE default 账户语句已删除——
                # 行留着无害（零业务读），配置已迁移即达成目的。守卫: GC1-GC3。
                db.commit()
                if migrated:
                    log.info("Migrated OAuth config from default account to meta table")
                else:
                    log.info("Default account config checked (no config to migrate)")
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
            # A2：显式写 kind 列 —— DDL 的 DEFAULT '' 在 MySQL 严格模式下会被
            # _strip_text_default 剥掉（TEXT 不能带默认），不写列会报 1364。
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, kind, config_json, updated_at) VALUES (?,?,?,?,datetime('now'))",
                      (user_id, provider_key, "", json.dumps({
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
        from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
        from src.domains.mgmt.auth.credential_crypto import decrypt
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
    cred = get_credential(user_id, provider, kind="oauth")
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
    import urllib.request, urllib.error, urllib.parse, json as _json

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
    from src.domains.mgmt.auth.providers import get_provider
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
    except urllib.error.HTTPError as e:
        # P-6⑤ 失败二分（D90 决议）：invalid_grant/400 = 凭据彻底失效 → status=4
        #（重新登录引导）；其余（网络/5xx）走 R21.4.2 暂时性重试。
        body = ""
        try:
            body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        if e.code == 400 or "invalid_grant" in body:
            log.warning(f"refresh_oauth: invalid_grant/400 → credential expired flow "
                        f"user={user_id}, provider={provider}")
            try:
                with db_lock:
                    db.execute(
                        "UPDATE external_identities SET status=4, updated_at=datetime('now') "
                        "WHERE grape_user_id=? AND provider=? AND status=1",
                        (user_id, provider))
                    db.commit()
            except Exception as _me:
                log.warning(f"mark expired failed: {_me}")
            return None
        log.warning(f"refresh_oauth: refresh failed (transient) for user={user_id}, provider={provider}: {e}")
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
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
                )
                resp2 = urllib.request.urlopen(req2, timeout=10)
                token_data = _json.loads(resp2.read())
            except Exception as e2:
                log.warning(f"refresh_oauth: retry also failed for user={user_id}, provider={provider}: {e2}")
                return None
        else:
            return None
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
    from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
    update_external_identity_tokens(
        user_id, provider,
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=new_expires_in,
    )

    # 5. 同步更新 user_credentials（迁移期保留，上层 deps.py 读 fetched_at/token_expires_at 判断过期）
    # Issue #343: cred 不存在时创建新条目，否则预刷新永远跳过（user_credentials 无 gitcode_oauth → 死路）
    cred = get_credential(user_id, provider, kind="oauth")
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
    set_credential(user_id, provider, new_cred, kind="oauth")

    log.info(f"refresh_oauth: success for user={user_id}, provider={provider}")
    return new_access

# 启动时初始化 + 迁移
init_db()

migrate_accounts_from_json()

# 迁移旧路径 skills 到新路径
_OLD_SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "skills", "custom")

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

def init_user_repos_table():
    with db_lock:
        db.execute(_USER_REPOS_DDL)
        db.commit()

def save_user_repos(user_id: int, repos: list[dict]):
    """批量写入用户仓库缓存（repos 元素含 full_name/owner/name/...，full_name 无前缀）

    is_join 从用户自己的主动托管记录（mark_user_repo_join 写入的 is_join=1 AND is_admin=1）继承，
    不再查询全局 managed_repos，防止 A 托管的仓库污染 B 的下拉框。
    """
    from src.kernel.repo_key import to_repo_key
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
    from src.kernel.repo_key import from_repo_key
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
    from src.kernel.repo_key import parse_repo_key
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


def invalidate_user_repos(user_id: int):
    """把用户仓库缓存标记为过期（保留缓存行与 is_join 状态）。

    写操作（托管 / 模板建仓 / 解除托管）成功后调用，令下一次 /repos/member
    直接回源 VCS 刷新，避免新增或移除的仓库被 30 分钟 TTL 挡在缓存之外。
    synced_at 置 NULL 后 get_user_repos_synced_at 返回空串，命中 _is_expired。
    """
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_repos SET synced_at=NULL WHERE user_id=?", (user_id,))
        db.commit()


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

def save_backend_agents(agents: list[dict], scope: str = "global", owner_id: str = "", host_name: str = ""):
    """[兼容别名] 见 save_agent_runtimes"""
    return save_agent_runtimes(agents, scope, owner_id, host_name)

_NAIVE_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")

# Issue #512 维护方评审意见②：迁移扫描进程内节流——同一进程两次全表扫描间隔 ≥ 该阈值（秒），
# 避免每轮本机直连重探（10min）都全表 SELECT。monotonic 时钟不受墙钟跳变影响；
# 判断与标记在专用锁内原子完成（与 db_lock 同语义的互斥保障），并发刷新不会重复扫描。
_AGENT_RUNTIMES_MIGRATE_MIN_INTERVAL = 3600

_agent_runtimes_migrate_throttle_lock = threading.Lock()

_agent_runtimes_migrate_last_mono = 0.0

def migrate_agent_runtimes_utc_caliber(tolerance_seconds: int = 60, throttle_seconds: int | None = None) -> int:
    """幂等修正 agent_runtimes 中"超前当前 UTC"的行（Issue #512）。

    根因：本机直连刷新链路历史写 SQL 字面量 datetime('now')，MySQL backend 经
    mysql_compat 翻译为 NOW()（CST 本地墙钟、无时区），与心跳链路 UTC ISO
    （datetime.now(timezone.utc).isoformat()）同表混存，last_seen_at/updated_at
    超前 UTC 约 8h，健康窗口误判为"未来时间"。

    规则（仅 MySQL backend 生效；SQLite 下 naive 已是 UTC，无超前问题）：
    - 对 last_seen_at/updated_at 中满足 naive 格式（无 Z/无 offset）且按 UTC 解释后
      超前当前 UTC（容差 tolerance_seconds）的值，按 normalize_event_time 语义
      （mysql=CST(+08:00) → UTC）改写为带 +00:00 的 UTC ISO；
    - 幂等：改写后值带时区偏移，不再命中 naive 正则，重复执行不重复改写；
    - 不超前 / 非 naive / 带 offset 的行一概不动（避免误伤正常离线行）；
    - 进程内节流（throttle_seconds，默认 _AGENT_RUNTIMES_MIGRATE_MIN_INTERVAL=3600s）：
      同一进程两次全表扫描间隔低于阈值时直接返回 0 不扫描；传 0 可禁用（测试/手动强制迁移）。

    挂载点：save_agent_runtimes 开头（本机直连周期重探与手动刷新均经此入口）。
    """
    from src.api.time_utils import _db_backend, normalize_event_time
    from datetime import datetime as _dt, timezone as _tz

    if _db_backend() != "mysql":
        return 0
    global _agent_runtimes_migrate_last_mono
    _interval = _AGENT_RUNTIMES_MIGRATE_MIN_INTERVAL if throttle_seconds is None else throttle_seconds
    with _agent_runtimes_migrate_throttle_lock:
        _now_mono = time.monotonic()
        if _now_mono - _agent_runtimes_migrate_last_mono < _interval:
            return 0
        _agent_runtimes_migrate_last_mono = _now_mono
    now_ts = _dt.now(_tz.utc).timestamp()
    changed = 0
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT id, last_seen_at, updated_at FROM agent_runtimes")
            for rid, last_seen, updated in c.fetchall():
                new_last, new_updated = last_seen, updated
                for col, val in (("last_seen_at", last_seen), ("updated_at", updated)):
                    if not val or not isinstance(val, str):
                        continue
                    raw = val.strip()
                    if not _NAIVE_DT_RE.match(raw):
                        continue
                    try:
                        ts_utc = _dt.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=_tz.utc).timestamp()
                    except Exception:
                        continue
                    if ts_utc > now_ts + tolerance_seconds:
                        norm = normalize_event_time(raw) or raw
                        if col == "last_seen_at":
                            new_last = norm
                        else:
                            new_updated = norm
                if new_last != last_seen or new_updated != updated:
                    c.execute(
                        "UPDATE agent_runtimes SET last_seen_at=?, updated_at=? WHERE id=?",
                        (new_last, new_updated, rid))
                    changed += 1
            db.commit()
        except Exception as e:
            log.warning(f"migrate_agent_runtimes_utc_caliber failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
    return changed

def save_agent_runtimes(agents: list[dict], scope: str = "global", owner_id: str = "", host_name: str = ""):
    """批量写入 agent runtimes 缓存（scope: global/repo/user）

    幂等 upsert：按 name+scope+owner_id+host_name 匹配，存在则更新（保留 id），不存在才插入。
    保证跨刷新 id 稳定，供各表 runtime_id 引用。
    host_name：本机直连引擎（credential_id=0）归属的主控机 hostname，区分多台主控机。

    Issue #512：本机直连刷新链路的时间口径统一为 UTC（带 +00:00），与心跳链路一致——
    不再写 SQL 字面量 datetime('now')（MySQL 下经 mysql_compat 翻译为 NOW()=CST 墙钟，
    导致 last_seen_at 超前 UTC 约 8h），改为 Python 参数化 UTC 时间戳写入
    last_seen_at/updated_at；写前先幂等迁移存量超前行。
    """
    from datetime import datetime as _dt, timezone as _tz

    _now_iso = _dt.now(_tz.utc).isoformat()
    try:
        migrate_agent_runtimes_utc_caliber()
    except Exception as _me:
        log.warning(f"migrate_agent_runtimes_utc_caliber skipped: {_me}")
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
                    # Issue #1496：status+last_seen_at 走统一写入口（同事务含遗留
                    # health 列派生同步），其余展示/配置字段仍随本 upsert 原地更新。
                    _rid0 = row[0]
                    _apply_runtime_state_compat(c, _rid0, "online", _now_iso)
                    c.execute(
                        "UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, healthy=?, "
                        "pip_package=?, updated_at=?, "
                        "local_skills=?, skill_scan_result=?, host_name=? WHERE id=?",
                        (a.get("display_name", name),
                         1 if a.get("installed") else 0, a.get("cli_path", ""),
                         a.get("version", ""), 1 if a.get("healthy") else 0,
                         a.get("pip_package", ""), _now_iso,
                         _ls_json, _detail_json, host_name, _rid0)
                    )
                else:
                    # 方案1（engine-selection-design.md §4.3）：本机直连（credential_id=0）是独立受管资源。
                    # refresh 仅持久化"真实安装"的本机 CLI（settings.py 已过滤 installed），即使同名已有
                    # 凭据绑定行（远端 worker），也创建本机直连行；未安装则跳过（防 -cred0 幽灵占位，
                    # 见 runtime-admin-two-view-design §6.2）。
                    if not a.get("installed"):
                        continue
                    c.execute(
                        "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, local_skills, skill_scan_result, host_name, last_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, a.get("display_name", name),
                         1, a.get("cli_path", ""),
                         a.get("version", ""), 1 if a.get("healthy") else 0,
                         a.get("pip_package", ""), scope, owner_id,
                         _ls_json, _detail_json, host_name, _now_iso, _now_iso)
                    )
                    rid = c.lastrowid or 0
                    # 新本机直连引擎默认写入 "all" 标签（与远端 worker 注册行为对齐）：
                    # 全广播模式下无 worker 时任务由本机直连认领，缺标签会导致任务永久卡 queued。
                    # 已有 capabilities 行（用户手动配置过）则不覆盖。
                    c.execute(
                        "SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (rid,))
                    if not c.fetchone():
                        _now_ec = _now_iso
                        c.execute(
                            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                            "VALUES (?, 'all', 1, 50, 1, ?, ?)",
                            (rid, _now_ec, _now_ec))
                        from src.domains.mgmt.routes.settings import _sync_agent_tags
                        _sync_agent_tags(rid, c)
            # 清理本次 scope 中已不在列表里的 runtime（忽略大小写）。
            # 仅清理未绑定凭据的本机行（credential_id=0）；凭据绑定的远端/边端注册行一律保留
            #（见 multi-runtime-instance-design §5.1 防误删）。
            current_names = [(a.get("name", "") or "").strip().lower() for a in agents if a.get("name")]
            if current_names:
                placeholders = ",".join("?" for _ in current_names)
                c.execute(f"DELETE FROM agent_runtimes WHERE scope=? AND owner_id=? AND credential_id=0 AND host_name=? AND lower(name) NOT IN ({placeholders})", (scope, owner_id, host_name, *current_names))
            # 去重：同 name+scope+owner_id+org_id+host_name+cred=0 的重复行只保留 id 最小的一条
            # （upsert 已 claim 旧行并写入 host_name，此处清理未被 claim 的重复行。
            #  host_name 必须进 GROUP BY：本机直连引擎按主控机隔离（engine-selection-design.md §4.3），
            #  002 的行与 001 同名不同机，缺 host_name 分组会把 002 的行当重复删掉——
            #  见 chat-engine-visibility-20261027 排障）
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id=0 AND id NOT IN "
                "(SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 "
                "GROUP BY lower(name), scope, owner_id, COALESCE(org_id, 0), COALESCE(host_name, ''))"
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
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, worker_version, org_id, spawn_fail_degraded_at, detached_from_scenes_at, probe_spawn_fail_count FROM agent_runtimes ORDER BY name")
        else:
            c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, worker_version, org_id, spawn_fail_degraded_at, detached_from_scenes_at, probe_spawn_fail_count FROM agent_runtimes WHERE scope=? AND (owner_id=? OR owner_id='') ORDER BY name", (scope, owner_id))
        return _rows_to_runtimes(c.fetchall())

def _rows_to_runtimes(rows) -> list[dict]:
    """将 DB 行列表转为 runtime dict 列表（供 get_agent_runtimes / paged 复用）。"""
    # AIShell 显示名（2026-10-13）：出口统一转换 name/display_name（内部键不变）
    from src.domains.runtime.engine.instances import to_display as _td
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
         "org_id": r[22] or 0,
         "spawn_fail_degraded_at": r[23] or "",
         "detached_from_scenes_at": r[24] or "",
         "probe_spawn_fail_count": int(r[25] or 0) if len(r) > 25 else 0}
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
    _KNOWN = {"opencode", "codearts", "hermes", "codex", "cursor", "atomcode"}
    if name in _KNOWN:
        return True
    try:
        from src.domains.runtime.engine.executor import discover_backends
        if any((b.get("name") or "").strip().lower() == name for b in discover_backends()):
            return True
    except Exception:
        pass
    try:
        from src.domains.runtime.engine.executor import _BACKENDS
        if name in _BACKENDS:
            return True
    except Exception:
        pass
    return False

def _local_engine_info(name: str) -> tuple[bool, str]:
    """按引擎类型名返回本机是否已安装 + cli_path（discover_backends 真实探测，见 engine-selection §7.3）。"""
    try:
        from src.domains.runtime.engine.executor import discover_backends
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
                from src.domains.runtime.engine.local_probe import probe_local_engines
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
        from src.kernel.featured_order import get_featured_order
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
    from src.kernel.mysql_compat import get_db_backend
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
    from src.kernel.repo_key import from_repo_key
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
    from src.kernel.featured_order import get_featured_order

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

def get_browser_callback_url() -> str:
    """浏览器回调基址（OAuth 回调 / 附件 URL / 前端展示用）。

    Issue #471 E1：优先 browser_callback_url（server-config.json > meta），
    未配置回退 server_base_url（向前兼容）。
    """
    _v = _split_url_configured_value("browser_callback_url")
    if _v:
        return _v
    return get_server_base_url()

def get_worker_connect_url() -> str:
    """worker 回连基址（devstage RUNTIME_SERVER_URL / 任务 server_url / 远程 worker 地址）。

    Issue #471 E1：优先 worker_connect_url（server-config.json > meta），
    未配置回退 server_base_url（向前兼容）。双地址部署（浏览器公网 ≠ worker 可达）
    时必须单独配置本项，否则设备永远接不进来。
    """
    _v = _split_url_configured_value("worker_connect_url")
    if _v:
        return _v
    return get_server_base_url()

def get_business_api_url_explicit() -> str:
    """显式配置的 task_business_api_url（未配置返回空串，不回落 server_base_url）。

    Issue #1577：用于解析优先级「显式配置 > server_base_url > 本机回环」，
    本机 runtime 也要尊重显式业务基址配置（QA 复审 Spec 偏离项）。
    """
    return _split_url_configured_value("task_business_api_url")

def get_business_api_url() -> str:
    """主控业务 API 基址（任务内 grape 业务命令 issues / git-repo 等全量端点用）。

    Issue #1577：与 worker 回连基址（get_worker_connect_url）语义拆分——
    任务派发时 `GRAPE_SERVER_URL` 指向业务 API（本项），worker runtime 基址由
    `RUNTIME_SERVER_URL` 独立承载。优先 task_business_api_url（server-config.json
    > meta），未配置回退 server_base_url（单地址部署与改造前等价）。
    """
    _v = get_business_api_url_explicit()
    if _v:
        return _v
    return get_server_base_url()

def _split_url_configured_value(key: str) -> str:
    """读取拆分配置项（server-config.json > meta），空串表示未配置。

    Issue #471 E1：browser_callback_url / worker_connect_url 双写两级来源，
    未配置返回空串（调用方回退 server_base_url）。
    """
    try:
        from src.kernel.config import read_server_config
        _v = str(read_server_config().get(key) or "").strip().rstrip("/")
        if _v:
            return _v
    except Exception:
        pass
    try:
        return str(get_meta_value(key) or "").strip().rstrip("/")
    except Exception:
        return ""

def get_url_split_warning() -> str:
    """Issue #471 E1：拆分配置项错误的明确报错引导；配置正确返回空串。"""
    warnings = []
    for _key in ("browser_callback_url", "worker_connect_url"):
        _v = _split_url_configured_value(_key)
        if not _v:
            continue
        _host = _v.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if _host in _LOOPBACK_HOSTS:
            _usage = "浏览器回调" if _key == "browser_callback_url" else "worker 回连"
            warnings.append(
                f"{_key} 当前指向本机回环地址 {_v}，{_usage}对外不可达；"
                f"请在 configs/server-config.json 配置对外可访问地址后重启。"
            )
    return " ".join(warnings)

import json, hashlib, re, time, os, threading, warnings


def resolve_official_checker_skill(agent_type: str) -> dict | None:
    """Issue #617：按 agent_type 回退官方检查器技能（skill 按名未命中时的兜底）。

    场景节点引用了官方库不存在的技能名（如 skill-batch-validator）时，materialize
    按名必然失败。此处按节点 agent_type 在 skill 表取 is_active=1 且 scope='official'
    的官方技能兜底：
    - 优先取技能名含 validator/check/review/lint/inspect 的“检查器”技能（id 最小）；
    - 否则取该 agent_type 下 id 最小的官方技能。
    返回技能 dict（含 id/skill_name/skill_path/file_path）或 None。
    """
    if not agent_type:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, skill_name, display_name, description, agent_type, skill_path, "
                "file_path, scope, owner_id, content_hash FROM skill "
                "WHERE agent_type=? AND is_active=1 AND scope='official' ORDER BY id",
                (agent_type,))
            rows = c.fetchall()
        if not rows:
            return None
        pick = next((r for r in rows
                     if re.search(r"valid|check|review|lint|inspect", r[1] or "", re.IGNORECASE)),
                    rows[0])
        return {
            "id": pick[0], "skill_name": pick[1], "display_name": pick[2] or pick[1],
            "description": pick[3] or "", "agent_type": pick[4] or "",
            "skill_path": pick[5] or pick[1], "file_path": pick[6] or "",
            "scope": pick[7] or "official", "owner_id": pick[8] or "",
            "content_hash": pick[9] or "",
        }
    except Exception as e:
        log.warning(f"resolve_official_checker_skill failed for {agent_type}: {e}")
        return None

def _upsert_repo_credential(c, repo_key: str, cred_type: str, value: str) -> None:
    """D98/D99/N4：grape_t_repo_credentials 幂等 upsert（UPDATE → rowcount==0 → INSERT）。

    供 add_managed_repo / _update_repo_bot_token 等写入口共用；
    id 首写 uuid7（主键，更新不碰），value 走 _encrypt_repo_secret。
    F4/D106 修订①：原裸 INSERT 撞 UNIQUE(repo_key, cred_type)（S82 基线
    3 红根因），同仓二次轮换 500——两步法即本助手函数。
    """
    from src.kernel.id_gen import uuid7_str
    _now = time.strftime("%Y-%m-%d %H:%M:%S")
    _enc = _encrypt_repo_secret(value or "")
    _cur = c.execute(
        "UPDATE grape_t_repo_credentials SET value=?, updated_at=? WHERE repo_key=? AND cred_type=?",
        (_enc, _now, repo_key, cred_type),
    )
    if _cur.rowcount == 0:
        try:
            c.execute(
                "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value, updated_at) "
                "VALUES (?,?,?,?,?)",
                (uuid7_str(), repo_key, cred_type, _enc, _now),
            )
        except Exception:
            # 并发插入撞唯一约束 → retry UPDATE
            c.execute(
                "UPDATE grape_t_repo_credentials SET value=?, updated_at=? WHERE repo_key=? AND cred_type=?",
                (_enc, _now, repo_key, cred_type),
            )

def repo_cred_write_keys(tok_col: str, wh_col: str) -> "tuple[str, str]":
    """F4/D106（凭据存储终态化第一批）：托管写路径的凭据 kwargs 键归一。

    卡派生列名非空（gitcode/github）→ 保留列名键（存量调用形态，进
    add_managed_repo 后路由 rc）；卡字段为空（gitee/gitlab 终态）→ 归一
    逻辑键 'bot'/'webhook'（同一路由）。防止空串键进 add_managed_repo 的
    identifier 白名单被拒。"""
    return (tok_col or "bot", wh_col or "webhook")

def managed_repo_exists(repo_key: str) -> bool:
    """判定 repo_key 是否已存在于 managed_repos（Issue #671：A2A 回退/自愈链路共用）。"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    if not repo_key:
        return False
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            return bool(c.fetchone())
    except Exception as e:
        log.warning(f"Managed repo exists check failed: {e}")
        return False

def _webhook_url_for_provider(base_url: str, provider: str) -> str:
    """webhook 回调 URL 的平台段由平台卡 webhook_path 决定（数据驱动，无平台字面量）。"""
    if not base_url:
        return base_url
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _card = get_platform_card(provider)
        _path = getattr(_card, "webhook_path", "") if _card is not None else ""
    except Exception:
        _path = ""
    if not _path:
        return base_url
    import urllib.parse as _up
    _u = _up.urlsplit(base_url)
    return _up.urlunsplit((_u.scheme, _u.netloc, _path, "", ""))

def clear_repo_bot_token(repo_key: str) -> bool:
    """R15.5 清除仓库级 bot token 的 db 层统一入口（F4/D106）。

    有列平台（gitcode/github）：清列（存量语义）；卡列空平台
    （gitee/gitlab 终态）：清 rc 权威行（value=''）。同时清
    bot_token_status。返回仓库行是否存在（调用方 404 判定）。
    SQL 精确限定 managed_repos / grape_t_repo_credentials，不触碰 meta
    （meta 有同名全局键 gitcode_webhook_token，非本域）。
    """
    import re as _re_crbt
    from src.kernel.repo_key import to_repo_key, parse_repo_key
    # db/db_lock 晚绑定（调用时取 session_manager 属性——路由层测试按
    # patch("src.kernel.session_manager.db") 打桩，模块级早期绑定会漏进真实库）
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
    repo_key = to_repo_key(repo_key)
    provider = parse_repo_key(repo_key)[0]
    try:
        from src.kernel.token_resolver import create_provider
        _col = create_provider(provider).token_storage_field()
    except Exception:
        _col = ""
    _has_col = bool(_re_crbt.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _col or ""))
    _now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _sm_dl:
            c = _sm_db.cursor()
            if _has_col:
                c.execute(
                    f"UPDATE managed_repos SET {_col}='', bot_token_status='' "
                    f"WHERE repo_key=?", (repo_key,))
                found = c.rowcount > 0
            else:
                c.execute(
                    "UPDATE grape_t_repo_credentials SET value='', updated_at=? "
                    "WHERE repo_key=? AND cred_type='bot'", (_now, repo_key))
                c.execute(
                    "UPDATE managed_repos SET bot_token_status='' WHERE repo_key=?",
                    (repo_key,))
                found = c.rowcount > 0
            _sm_db.commit()
        return found
    except Exception as e:
        log.warning(f"clear_repo_bot_token failed: {e}")
        return False

def get_business_api_port() -> str:
    """本机业务 API 端口（单机多进程部署时与 runtime 端口解耦，Issue #1731）。

    优先 task_business_api_port（server-config.json > meta），其次环境变量
    `GRAPE_BUSINESS_PORT`；未配置返回空串，由调用方回退 `_local_port()` 兼容默认
    （GRAPE_PORT > GRAPE_MAIN_PORT > 3000）。runtime 端口可能指向 runtime 网关
    （如 8083）而非业务 API（如 8081），故业务端口需独立配置。
    """
    _v = _split_url_configured_value("task_business_api_port")
    if _v:
        return _v
    return str(os.environ.get("GRAPE_BUSINESS_PORT") or "").strip()

def get_task_service_api_url_explicit(service: str) -> str:
    """显式配置的按服务任务基址（vcs/issue/a2a）；未配置返回空串。

    Issue #1858：同一 base_url 无法同时承载 8081 业务 API 与 8082 A2A 时，
    运维可分别配置 `task_vcs_api_url` / `task_issue_api_url` / `task_a2a_api_url`。
    """
    key = _TASK_SERVICE_API_KEYS.get((service or "").strip().lower(), "")
    return _split_url_configured_value(key) if key else ""

def get_task_gateway_base_url() -> str:
    """可选聚合网关基址（按 /vcs /issue /a2a 前缀路由三面）；未配置返回空串。

    Issue #1858：网关就绪时作为默认分服务变量的替代（二者择一，显式服务键优先）。
    """
    return _split_url_configured_value("task_gateway_base_url")

def _load_credential_json(raw: str) -> dict:
    """读侧统一解密+反序列化（N1 通道加密配套）。

    优先 decrypt()（密文格式 {kid}:{nonce}:{ct}），解密产物/明文行统一
    json.loads。decrypt 的明文兼容（无冒号分隔符原样返回）天然覆盖历史
    明文行——读路径零迁移。解析失败返回 {}（与原 json.loads 失败语义一致，
    不抛不阻断）。"""
    from src.domains.mgmt.auth.credential_crypto import decrypt
    try:
        return json.loads(decrypt(raw))
    except Exception:
        return {}

def _rebuild_webhook_url_for_provider(webhook_url: str, provider: str) -> str:
    """D12/W3：按目标平台重建 webhook 回调 URL。

    历史实现是字符串替换（把 /webhook/gitcode 替换成 /webhook/github）——
    新平台没有对应的替换规则。改为：剥掉旧平台路径后缀，按目标平台卡
    webhook_path 重建；无卡平台回退历史替换行为。
    """
    if not webhook_url:
        return webhook_url
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
    except Exception:
        card = None
    if card is None:
        # D43/S63：无卡平台不做字符串替换（内置三平台恒有卡，此分支仅未知
        # provider 保守原样返回）
        return webhook_url
    base = webhook_url
    if "/webhook/" in base:
        base = base.split("/webhook/")[0]
    return base.rstrip("/") + card.webhook_path

def get_global_bot_token_for(provider: str) -> str:
    """统一入口（D15 抽象来源链）：任意平台的全局 bot token。

    meta key 按 provider 派生（与 VCSProvider.global_bot_meta_key 同模式）；
    文件 fallback 仅存量两平台（历史迁移兼容）；config-only 平台走 meta。
    """
    key = f"global_bot_{provider}_token"
    field = _GLOBAL_BOT_FILE_FIELDS.get(provider)
    if field:
        return _cached_get_meta(key, file_fallback_fn=lambda: _read_gbt_file_field(field))
    return _cached_get_meta(key)

def get_global_gitee_bot_username() -> str:
    """统一入口：DB meta 表读取（60s 缓存）。

    扫雷⑦（中危②）：gitee 全局 bot username 单值源——与 token 链
    get_global_bot_token_for("gitee")（meta 键 global_bot_gitee_token）同构，
    单 bot 账号单值形态（参照 github，非 gitcode 多账号 pool）。
    未配置返回空串（resolve_bot_username 回落卡 bot_identity）。
    """
    return _cached_get_meta("global_bot_gitee_username")

def get_global_bot_usernames_for(provider: str) -> list[str]:
    """S87/D63：按 provider 的全局 bot username 集合（self-loop 判定统一口径）。

    github=独立 meta 键单值（global_bot_github_username）；gitee=专属单值
    （global_bot_gitee_username，未配置回落 gitcode usernames 集合——存量
    config-only 同源语义保持）；其余平台=gitcode usernames 集合。
    D15 抽象不变量（test_guard_no_new_platform_branches）：按来源链/名单数据
    派生，src/ 不得出现平台名 == 字面分支。
    """
    _src = GLOBAL_BOT_USERNAME_SOURCES.get(provider)
    if _src is None:
        return get_global_bot_usernames()
    _vals = [u for u in _src() if u]
    if _vals and provider in _BOT_USERNAME_SINGLE_VALUE_PROVIDERS:
        return _vals[:1]
    if _vals:
        return _vals
    # 单值源未配置：回落名单内平台走默认 pool（gitcode 同源语义），其余保持空
    if provider in _BOT_USERNAME_FALLBACK_DEFAULT_POOL:
        return get_global_bot_usernames()
    return []

def _single(has_fallback: bool):
    def _mark(fn):
        fn._falls_back_to_pool = has_fallback
        return fn
    return _mark

def _path_b_prefix_clause(provider: str) -> str:
    """D43/S63：路径 B 查询的 repo_key 前缀子句（目录派生）。

    默认平台（内置卡声明序首，存量=gitcode）匹配自身前缀或裸键（无前缀
    存量数据）；其余平台（github/gitee 等）精确前缀。
    """
    try:
        from src.domains.runtime.vcs.platform_card import _DEFAULT_CARDS_RAW
        _default = next(iter(_DEFAULT_CARDS_RAW))
    except Exception:
        _default = ""
    if provider == _default:
        return f"(repo_key LIKE '{provider}:%' OR repo_key NOT LIKE '%:%')"
    return f"repo_key LIKE '{provider}:%'"

def get_user_platform_token(user_id: str, provider: str) -> str:
    """S87/D63：孪生函数泛化——任意平台的用户级 API token（user_credentials）。

    get_user_gitcode_token / get_user_github_token_by_id 均为本函数薄委托
    （名字保留向后兼容，Issue #343 歧义消解历史见各自 docstring）。
    """
    if not user_id or str(user_id or "") in ("", "0") or not provider:
        return ""
    try:
        creds = get_credentials(user_id)
        for c in creds:
            if c.get("provider") == provider:
                return c.get("config", {}).get("token", "") or ""
    except Exception as e:
        log.warning(f"get_user_platform_token failed for user={user_id} provider={provider}: {e}")
    return ""

def get_webhook_secret_by_repo_key_any(full_name: str, provider: str = "") -> str:
    """按任意 provider 前缀的 repo_key 查 webhook_secret（D14 通用面，只读）。

    遍历 managed_repos 中 repo_key = "<provider>:full_name" 的所有平台行，
    返回第一个非空 webhook_secret；无命中返回空串。存量平台行为由调用方
    （get_webhook_token_by_repo）的后续分流兜底，不受本函数影响。

    扫雷⑦（中危④）：provider 给定时收窄为该平台精确 repo_key 单查——
    跨平台同名仓（github:o/r 与 gitlab:o/r 同时托管）不再互串 secret。
    provider 为空保持存量任意 provider 遍历（兼容旧调用方）。
    """
    _rk = ""
    if provider:
        from src.kernel.repo_key import to_repo_key as _trk_ws
        _rk = _trk_ws(full_name, provider=provider)
    # S81：ORM 路径（ManagedRepoRepository.find_by_full_name_any_provider）
    try:
        from src.kernel.persistence.repositories import managed_repo_repository as _mrr
        from src.kernel.persistence.uow import uow as _uow
        with _uow() as session:
            if _rk:
                # 串仓隔离：精确 repo_key 单查（不遍历其他平台行）
                return _mrr.get_webhook_secret(session, _rk) or ""
            for d in _mrr.find_by_full_name_any_provider(session, full_name):
                if d.webhook_secret:
                    return d.webhook_secret
    except Exception as e:
        log.warning(f"get_webhook_secret_by_repo_key_any ORM failed: {e}")
    return ""

def any_repo_credential_for_repos(repo_keys: "list[str]") -> bool:
    """S82/D58：任一仓库在 grape_t_repo_credentials 有非空凭据（has_bot 闸门 rc 感知）。"""
    keys = [k for k in (repo_keys or []) if k]
    if not keys:
        return False
    try:
        with db_lock:
            ph = ",".join(["?"] * len(keys))
            row = db.cursor().execute(
                f"SELECT 1 FROM grape_t_repo_credentials WHERE repo_key IN ({ph}) "
                "AND value IS NOT NULL AND value != '' LIMIT 1", keys).fetchone()
            return bool(row)
    except Exception as e:
        log.warning(f"any_repo_credential_for_repos failed: {e}")
        return False

def get_repo_credential_meta(repo_key: str, cred_type: str) -> "dict | None":
    """D90-N4：读 rc 凭据 + 审计锚点（id）一次查询。

    返回 {"value": 解密后明文, "id": 行 UUID}；无行返回 None。
    供 RepoBotToken 命中审计用（value 与 id 同事务一致）。
    """
    try:
        with db_lock:
            row = db.cursor().execute(
                "SELECT value, id FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type=?",
                (repo_key, cred_type)).fetchone()
            if not row:
                return None
            return {"value": _decrypt_repo_secret(row[0] or ""),
                    "id": row[1] or ""}
    except Exception as e:
        log.warning(f"get_repo_credential_meta failed: {e}")
        return None

def _decrypt_repo_secret(raw: str) -> str:
    """D90-N3：repo 凭据读侧统一解密（bot token 通道加密配套）。

    decrypt 的明文兼容（无冒号分隔符原样返回）覆盖历史明文行——
    向前兼容：存量库零迁移可读。空串直通。失败抛错（D46 P1-10 同口径，
    解密失败调用方必须知道）。"""
    if not raw:
        return raw
    from src.domains.mgmt.auth.credential_crypto import decrypt
    return decrypt(raw)

def _encrypt_repo_secret(plaintext: str) -> str:
    """D90-N3：repo 凭据写侧统一加密（空串直通——空=无凭据，非机密）。"""
    if not plaintext:
        return plaintext
    from src.domains.mgmt.auth.credential_crypto import encrypt
    return encrypt(plaintext)

def get_repo_credential_value(repo_key: str, cred_type: str) -> str:
    """S80b：读 grape_t_repo_credentials 单表值（S82 起 rc 唯一权威、无条件；
    空串=无值，调用方回落 managed_repos 列=历史回落）。
    N3：返回前统一解密（历史明文行经 decrypt 明文兼容原样返回）。"""
    try:
        with db_lock:
            row = db.cursor().execute(
                "SELECT value FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type=?",
                (repo_key, cred_type)).fetchone()
            return _decrypt_repo_secret(row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"get_repo_credential_value failed: {e}")
        return ""


"""用户系统数据库：users/credentials/vms/skills 表管理

D32 迁移完成后 user_accounts 表为死表，不再被业务代码读取。
Issue #1588 B 批：老表包装函数（create_user/get_user/get_user_by_username/list_users/
delete_user/update_password/verify_user/get_token_version/increment_token_version/
get_user_by_gitcode_id/link_gitcode_user/get_all_user_accounts/get_user_account/
save_user_account/delete_user_account 等 16 个包装、其私有别名与字段常量）
已整体删除——新代码一律使用 src/domains/mgmt/auth/grape_account_db.py：
  账号 CRUD → create_grape_account/get_grape_account_by_id/get_grape_account_by_username
  密码/校验 → update_password/verify_grape_account
  token_version → grape_account_db.get_token_version/increment_token_version
  外部绑定 → create_external_identity/get_external_identity
"""

_SPLIT_KEY_USAGE = {
    "browser_callback_url": "浏览器回调",
    "worker_connect_url": "worker 回连",
    "task_business_api_url": "任务业务 API",
}

# Issue #1858：任务环境按服务注入基址——业务面（VCS / Issue）与控制面（A2A）分离。
# 显式配置键（server-config.json > meta）；未配置由 prompt_builder 按聚合网关前缀
# 或业务/回连基址回退，且来源在 env 中显式记录（不乱冒充）。
_TASK_SERVICE_API_KEYS = {
    "vcs": "task_vcs_api_url",
    "issue": "task_issue_api_url",
    "a2a": "task_a2a_api_url",
}

_GLOBAL_BOT_FILE_FIELDS = {"gitcode": "gitcode_api_token", "github": "github_bot_token"}

GLOBAL_BOT_USERNAME_SOURCES = {
    "github": _single(has_fallback=False)(lambda: [get_global_github_bot_username()]),
    "gitee": _single(has_fallback=True)(lambda: [get_global_gitee_bot_username()]),
    "gitcode": get_global_bot_usernames,
}

# S87/D63：单值源平台 & 回落默认 pool——从 GLOBAL_BOT_USERNAME_SOURCES 的
# 结构派生：callable 值=单值源；值属性 _falls_back_to_pool=True=未配置时回落
# gitcode 池（github 独立键不回落，见 SOURCES 声明处的属性标记）。
# 新增平台在 SOURCES 一处声明即被两个 frozenset 正确归类，零平台字面枚举。
_BOT_USERNAME_SINGLE_VALUE_PROVIDERS = frozenset(
    k for k, v in GLOBAL_BOT_USERNAME_SOURCES.items()
    if hasattr(v, "_falls_back_to_pool"))

_BOT_USERNAME_FALLBACK_DEFAULT_POOL = frozenset(
    k for k, v in GLOBAL_BOT_USERNAME_SOURCES.items()
    if callable(v) and getattr(v, "_falls_back_to_pool", False))


def get_host_detail(repo_key: str) -> dict:
    """读取仓库托管状态及诊断（Issue #2126），无记录或未迁移时返回空值。"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    empty = {"status": "", "step": "", "error": "", "updated_at": ""}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT host_status, host_step, host_error, host_updated_at "
                "FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if not row:
                return empty
            return {
                "status": str(row[0] or ""),
                "step": str(row[1] or ""),
                "error": str(row[2] or ""),
                "updated_at": str(row[3] or ""),
            }
    except Exception as e:
        log.warning(f"Get host detail failed: {e}")
        return empty

