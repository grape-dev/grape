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
# 运行域表的读写一律走该域 public 的具名查询/原语（三库独立后无视图可读，本文件不留外域 SQL）
from src.domains.runtime import public as _rt
# grape_t_repo_credentials 归 insight（不是 runtime）：仓库凭据唯一权威读写口在该域 public
from src.domains.insight import public as _insight_public

from src.kernel.config import log
from src.kernel.tags import MAX_TAGS, encode_tags, parse_tags


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

    连接归属统一由 kernel.route_of("mgmt") 决定：
      本域进程 / 单库拓扑 → 主库（零回归）；三库跨域 → GRAPE_DSN_MGMT；
      三库缺 DSN → 抛 DomainUnavailable（不再静默回落主库）。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        if db_registry.route_of("mgmt") == "domain":
            _conn = db_registry.new_connection("mgmt")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        # 跨域读却拿不到 mgmt DSN → 会在进程库里读同名 VIEW（列快照过期即 1054）。
        # 单库/共库拓扑下库名相同，不会告警。
        db_registry.warn_if_cross_domain_fallback("mgmt")
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
        if db_registry.route_of("mgmt") == "domain":
            return None
        from src.kernel.session_manager import db as _main
        return _main.commit()

    def rollback(self):
        from src.kernel import db_registry
        if db_registry.route_of("mgmt") == "domain":
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

def _apply_runtime_state_compat(c, runtime_id: int, status: str, last_seen_at: str) -> None:
    """延迟绑定 apply_runtime_state（Issue #1496）。

    src/domains/runtime/engine/service.py 反向 import 本模块（get_task_token），模块级
    `from src.domains.runtime.engine.health import apply_runtime_state` 会形成
    user.db → runtime.health → runtime/__init__ → service → user.db 环；
    运行期（调用点）再解析则无环。
    """
    from src.domains.runtime.public import apply_runtime_state
    apply_runtime_state(c, runtime_id, status, last_seen_at)

# ====== 用户账户表（合并 users + bot_accounts） ======
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def batch_update_last_active(uids: list) -> None:
    """批量更新用户最后活跃时间——定时任务调用，分批每 1000 个"""
    if not uids:
        return
    with db_lock:
        c = _mg_db.cursor()
        # D32-补丁：更新 grape_accounts.last_active_at（不写 user_accounts 老表）
        for i in range(0, len(uids), 1000):
            batch = uids[i:i+1000]
            placeholders = ",".join(["?"] * len(batch))
            c.execute(
                f"UPDATE grape_accounts SET last_active_at=datetime('now') WHERE id IN ({placeholders})",
                batch,
            )
        _mg_db.commit()

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
            c = _mg_db.cursor()
            c.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            _mg_db.commit()
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
            c = _mg_db.cursor()
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
            _mg_db.commit()
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
            c = _mg_db.cursor()
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
            _mg_db.commit()
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
            c = _mg_db.cursor()
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
                    _mg_db.commit()
                    return None
            _mg_db.commit()
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
            c = _mg_db.cursor()
            c.execute("DELETE FROM exchange_codes WHERE expires < ?", (_t.time(),))
            _mg_db.commit()
            return c.rowcount
    except Exception:
        return 0

def validate_oauth_state(state: str, provider: str = "", consume: bool = True) -> dict | None:
    """校验 OAuth state（薄委托：实现见 auth_guards.OAuthStateStore.validate）。

    P3-5 口径：先比 provider 再 DELETE（不匹配不消费），`consume=False` 只读校验，
    原子性靠 `DELETE ... WHERE state=? AND expires>?` 的 rowcount。本函数只为既有
    import 面（路由/测试）保留同名入口，判定与 SQL 都在 auth_guards 里。
    """
    from src.domains.mgmt.auth.auth_guards import OAuthStateStore

    return OAuthStateStore.validate(state, provider, consume=consume)


# ====== 凭据表 ======
# 铁律（2026-11-07）：所有新增表必须有 UUID 主键 id——应用层 uuid7_str() 生成、
# TEXT 存储、36 字符标准形式（8-4-4-4-12，不混 32 hex）。存量无 id 表随各域
# 批次逐步补列回填。
_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS user_credentials (
    -- A2b（2026-11-13）：主键改为 (user_id, provider, kind)。同平台的 PAT 与 OAuth
    -- 必须能共存——OAuth 刷新是现役写路径（refresh_oauth_token → kind='oauth'），
    -- 旧 UNIQUE(user_id, provider) 会把 PAT 归位行 upsert 成覆盖，A2 收口删旧键行后
    -- PAT 即丢失（验证库实弹复现）。id 因此退化为普通列，不再承担唯一性。
    -- 默认值写成 VARCHAR + 字面量：MySQL 上 TEXT 列不能带默认（1101），
    -- 翻译器会把 TEXT DEFAULT '' 静默剥掉 → 不写 id/created_at 的 INSERT 严格模式
    -- 报 1364（QA !1372 P3）。VARCHAR(191) 的字面量默认两栈全版本合法，
    -- id 是 UUID(36)、时间戳 19 字符，长度足够；reconcile 已把 VARCHAR(191) 归一为 TEXT，
    -- 不影响 schema drift。
    id VARCHAR(191) NOT NULL DEFAULT '',
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
    created_at VARCHAR(191) DEFAULT '',
    updated_at VARCHAR(191) DEFAULT '',
    PRIMARY KEY (user_id, provider, kind)   -- upsert 语义键：一用户一平台一类型一条
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
            c = _mg_db.cursor()
            # A2b（2026-11-13）：主键已含 kind，同平台 PAT/OAuth 各自一行，互不覆盖。
            # 原先这里有一处"跨 kind 覆盖"告警（唯一键未扩时期的兜底），扩键后不再可触发。
            for row in rows:
                c.execute(
                    """INSERT INTO user_credentials (id, user_id, provider, kind, config_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(user_id, provider, kind) DO UPDATE
                       SET config_json=excluded.config_json, updated_at=excluded.updated_at""",
                    row)
            _mg_db.commit()
            if (kind or "") == "pat":
                try:
                    from src.domains.platform.public import on_pat_saved
                    on_pat_saved(str(user_id), str(provider))
                except Exception as _hook_err:
                    log.warning(f"platform-link hook (pat saved) skipped: {_hook_err}")
            return True
    except Exception as e:
        log.warning(f"Set credential failed: {e}")
        return False

def get_credentials(user_id: "int | str", include_unreadable: bool = False) -> list[dict]:
    """获取用户凭据（D32-补丁：从 user_credentials 表 + external_identities 读，不查 user_accounts）

    A2：返回 kind 维度；双写期旧复合键行（provider 形如 `<name>_pat/_oauth` 且
    kind='') 与对应的新形态行重复，这里过滤掉旧键行，调用方只看到归位形态。

    include_unreadable=True（2026-11-18）：密文解不开/解析失败的行**也返回**，带
    ``credential_error``（decrypt_failed / parse_failed）+ 空 config。默认 False 保
    持老语义（调用方只看到"能用的凭据"），需要"配了但坏了"这个中间态的地方
    （平台关系/项目页/凭据页）显式打开——否则用户看到的是"你没配置"，而不是
    "你的配置坏了，请重新配置"。
    """
    result = []
    # 1. user_credentials 表（huawei_cloud/codearts/feishu 等非 OAuth 凭据）
    with db_lock:
        c = _mg_db.cursor()
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
            _err: dict = {}
            cfg = _load_credential_json(cfg_json, errors=_err) if cfg_json else {}
            entry = {"provider": p, "kind": k or "", "config": cfg}
            if _err:
                if not include_unreadable:
                    continue
                entry.update(_err)
            result.append(entry)
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

def get_credential(user_id: "int | str", provider: str, kind: str = "",
                   diagnostics: "dict | None" = None) -> dict | None:
    """获取指定凭据（读侧自动解密，历史明文行兼容）。

    A2 双读序：① 归位形态 (provider, kind) → ② 旧复合键 f"{provider}_{kind}" →
    ③ 该 provider 的未分类行（历史 kind=''）。滚动升级期三种形态都可能存在。

    A2 收口前补丁（2026-11-13）：三态按 **updated_at 取较新者**，不再单按优先级命中。
    理由：回退窗口里旧实例只写 `<p>_pat` 行，归位行会变成 stale 副本；只认归位形态
    就会把旧值读回来（用户看到"凭据莫名回退"）。同 updated_at 时归位形态优先，保持
    结果确定性。

    diagnostics（2026-11-18）：传入 dict 时，若密文解不开会写入
    ``credential_error=decrypt_failed``——调用方据此区分"没配置"与"配置坏了"。
    """
    cands = [(provider, kind or "", 0)]
    if kind:
        cands += [(f"{provider}_{kind}", "", 1), (provider, "", 2)]
    pick = None  # (updated_at, -priority, config_json)
    with db_lock:
        c = _mg_db.cursor()
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
    _err: dict = {}
    cfg = _load_credential_json(pick[2], errors=_err)
    if _err and diagnostics is not None:
        diagnostics.update(_err)
    return cfg or None

def delete_credential(user_id: "int | str", provider: str, kind: str = "") -> bool:
    """删除凭据（A2：同时删归位行与双写期旧键行，避免残留可读副本）。"""
    try:
        keys = [(provider, kind or "")]
        legacy = _legacy_credential_key(provider, kind)
        if legacy:
            keys.append((legacy, ""))
        with db_lock:
            c = _mg_db.cursor()
            deleted = False
            for prov, k in keys:
                c.execute("DELETE FROM user_credentials "
                          "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                          (user_id, prov, k))
                deleted = deleted or c.rowcount > 0
            _mg_db.commit()
            if (kind or "") == "pat" and deleted:
                try:
                    from src.domains.platform.public import on_pat_deleted
                    on_pat_deleted(str(user_id), str(provider))
                except Exception as _hook_err:
                    log.warning(f"platform-link hook (pat deleted) skipped: {_hook_err}")
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
            c = _mg_db.cursor()
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
                c.execute("SELECT 1 FROM user_credentials WHERE user_id=? AND provider=? "
                          "AND COALESCE(kind,'')=? LIMIT 1",
                          (user_id, target_provider, target_kind))
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
            _mg_db.commit()
        if any(stats.values()):
            log.info(f"credential kind backfill: {stats}")
        return stats
    except Exception as e:
        log.warning(f"backfill_credential_kind failed: {e}")
        try:
            _mg_db.rollback()      # 失败事务不能留在连接上（评审 M-1）
        except Exception:
            pass
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
    is_org_repo INTEGER DEFAULT 0,         -- 组织自动 provisioning 的仓库；禁止解绑/迁移
    linked_by TEXT DEFAULT '',             -- 关联到组织的操作用户（D105 后 user id 恒 TEXT）
    linked_at TEXT DEFAULT '',             -- 关联时间
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
        c = _mg_db.cursor()
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
                _mg_db.commit()
                try:
                    c.execute("DROP TABLE managed_repos_legacy")
                except Exception:
                    pass
                _mg_db.commit()
                if legacy_pairs:
                    try:
                        for uid, rk in legacy_pairs:
                            c.execute("UPDATE user_repos SET is_join=1 WHERE user_id=? AND repo_key=?", (uid, rk))
                        _mg_db.commit()
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
                    _mg_db.commit()
                    for (rk,) in m_joined:
                        c.execute("UPDATE user_repos SET is_join=1 WHERE repo_key=?", (rk,))
                    _mg_db.commit()
                except Exception:
                    pass
                try:
                    c.execute("DROP TABLE user_repos_legacy")
                except Exception:
                    pass
                _mg_db.commit()
            ur_cols2 = [r[1] for r in c.execute("PRAGMA table_info(user_repos)").fetchall()]
            if "is_join" not in ur_cols2:
                try:
                    c.execute("ALTER TABLE user_repos ADD COLUMN is_join INTEGER DEFAULT 0")
                    _mg_db.commit()
                except Exception:
                    pass
            if "created_at" not in ur_cols2:
                try:
                    c.execute("ALTER TABLE user_repos ADD COLUMN created_at TEXT DEFAULT ''")
                    _mg_db.commit()
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
                    _mg_db.commit()
                else:
                    c.execute(
                        f"UPDATE {_tbl} SET repo_key='gitcode:'||repo_key "
                        f"WHERE repo_key!='' AND repo_key NOT LIKE '%:%'"
                    )
                    _mg_db.commit()
            except Exception as e:
                from src.kernel.db_errors import missing_schema
                if missing_schema(e):
                    log.debug(f"Migrate {_tbl} repo_key prefix skipped (no local table): {e}")
                else:
                    log.warning(f"Migrate {_tbl} repo_key prefix failed: {e}")
        # skill 表 repo 级记录：owner_id 存 repo_key（scope='repo'）
        # skill 归运行域：独立拓扑下必须走 owner 连接，否则 1146
        try:
            # skill 归运行域：具名写查询经 runtime public 可移植原语（自有 DSN 优先，
            # 单库拓扑回落进程主库），不再直连 runtime 基表。
            from src.domains.runtime import public as _rt  # noqa: PLC0415
            _rt.exec_any("skill_repo_owner_prefix")
        except Exception as e:
            from src.kernel.db_errors import missing_schema
            if missing_schema(e):
                log.debug(f"Migrate skill repo_key prefix skipped (no local table): {e}")
            else:
                log.warning(f"Migrate skill repo_key prefix failed: {e}")

        # 幂等清理：迁移后遗留的 legacy 表删除（如 managed_repos_legacy / user_repos_legacy）
        try:
            for _leg in ("managed_repos_legacy", "user_repos_legacy"):
                _exists = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_leg,)).fetchone()
                if _exists:
                    c.execute(f"DROP TABLE IF EXISTS {_leg}")
                    _mg_db.commit()
                    log.info(f"Migrate repo_key: dropped leftover {_leg}")
        except Exception as e:
            log.warning(f"Cleanup legacy repo tables failed: {e}")

        # 幂等补充：managed_repos 已有表加 dev_scene 列（开发场景持久化）
        try:
            _mr_cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "dev_scene" not in _mr_cols:
                c.execute("ALTER TABLE managed_repos ADD COLUMN dev_scene TEXT DEFAULT 'default'")
                _mg_db.commit()
                log.info("Migrate repo_key: added managed_repos.dev_scene")
        except Exception as e:
            log.warning(f"Add managed_repos.dev_scene failed: {e}")

        # 幂等补充：managed_repos 加 scene_recommend_config 列（智能推荐配置）
        try:
            _mr_cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
            if "scene_recommend_config" not in _mr_cols:
                c.execute("ALTER TABLE managed_repos ADD COLUMN scene_recommend_config TEXT DEFAULT ''")
                _mg_db.commit()
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
    - src/domains/mgmt/user/legacy_column_migrations.py：存量列补齐 / 孤儿凭据清理
    - src/domains/mgmt/user/credentials_backfill_n1.py：N1 凭据 UUID 主键补列 + 明文加密回填
    每个模块幂等（重跑零写入），各自 docstring 记录下线判据。
    （合并重同步：weekly-08 s122 模块化结构恢复；模块覆盖面已逐标记核验，
    重复用户清理随 D32 user_accounts 死表收敛退役。）
    """
    with db_lock:
        # 三库拓扑：这 4 张都是 mgmt 域基表。旧设计假设非 owner 侧是跨库视图，视图已退役，
        # 现在非 owner 进程（runtime/insight）执行这些 DDL 会在自己的库里建出空壳副本
        # （2026-11-15：runtime 库被重建过 user_accounts / user_credentials /
        # managed_repos / oauth_states / user_repos，跨域读于是在空表上静默返回空结果）。
        from src.kernel import table_ownership as _own_tbl
        for _tbl, _ddl in (("user_credentials", _CREDENTIALS_DDL),
                           ("managed_repos", _MANAGED_REPOS_DDL),
                           ("oauth_states", _OAUTH_STATES_DDL)):
            if _own_tbl.should_skip(_tbl):
                log.info(f"[DDL_SKIP] `{_tbl}`: owner={_own_tbl.owner_of(_tbl)}，非本域进程不建表")
                continue
            db.execute(_ddl)
        # 兼容旧列迁移（幂等）：托管诊断列（Issue #2126）——CREATE TABLE 只影响新库
        # 域拆分库（arch 四进程）：managed_repos 归属 mgmt 基表，runtime/insight 侧是
        # 跨库视图——视图上 ALTER 必然 1347（视图侧不能 ALTER）。视图进程直接跳过，
        # 列归 owner 基表管（与迁移 fn 的视图守卫同款，避免每次启动刷 WARNING）。
        _mr_is_view = False
        from src.kernel.session_manager import _is_mysql_backend as _mr_is_mysql
        if _mr_is_mysql():
            try:
                _row = _mg_db.cursor().execute(
                    "SELECT TABLE_TYPE FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='managed_repos'").fetchone()
                _mr_is_view = bool(_row and str(_row[0]).upper() == "VIEW")
            except Exception:
                pass
        if not _mr_is_view and not _own_tbl.should_skip("managed_repos"):
            for _col in ("host_step", "host_error", "host_updated_at"):
                try:
                    db.execute(f"ALTER TABLE managed_repos ADD COLUMN {_col} TEXT DEFAULT ''")
                except Exception as _e:
                    # 幂等：列已存在属预期；其它失败需留痕（#2126 透明化方向，不外抛中断启动）
                    log.warning(f"managed_repos 补充诊断列失败 col={_col}: {_e}")
        # oauth_states 的建表已在上面的守卫循环里（owner=mgmt）；这里不再重复执行，
        # 否则非 owner 进程会绕过守卫，在自己的库里建出空壳（2026-11-15 实测：
        # worker/event 的 init_db() 只重建 oauth_states 一张，正是这一行）。
        from src.domains.mgmt.user.legacy_column_migrations import run as _legacy_migrations
        _legacy_migrations()
        from src.domains.mgmt.user.credentials_backfill_n1 import run as _n1_backfill
        _n1_backfill()
        # repo_key 统一整改迁移（managed_repos 去 user_id + user_repos 加前缀/is_join）
        _migrate_repo_key_tables()
        # D32 死表收敛（2026-11-07 起）：原「从 users/bot_accounts 回填 user_accounts」
        # 写入已退役——账号唯一权威表是 grape_accounts，回填只会在死表留影子数据。
        # 守卫：tests/test_user_accounts_write_freeze_guard.py（GC1/2/3 零写入）。
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
                _mg_db.commit()
                # Check if more rows remain (MySQL batch loop)
                if _is_mysql:
                    c = _mg_db.cursor()
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
            _mg_db.commit()
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
        _mg_db.commit()
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
        # 三库拆独立实例：skill 归运行域，具名查询经 runtime public 可移植原语读取
        # （自有 DSN 优先，单库拓扑回落进程主库），调用方零差异。
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            row = _rt.read_one_any("skill_row_by_id", (int(skill_id),))
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
        from src.kernel.db_errors import missing_schema
        if missing_schema(e):
            # worker 本地库没有 skill 表：agent 端 skill scope 解析按「无此技能」降级
            log.debug(f"get_skill_by_id skipped (no local table): {e}")
        else:
            log.warning(f"get_skill_by_id failed: {e}")
        return None

def get_skill_content_hash(skill_name: str, scope: str = "official") -> tuple[int, str, str]:
    """读取技能的主键 id、content_hash 和 file_path（变更检测用）。
    返回 (id, content_hash, file_path)，id=0 表示不存在。
    """
    import os as _os
    try:
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            row = _rt.read_one_any("skill_by_name_scope", (skill_name, scope or "official"))
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            row = _rt.read_one_any(
                "skill_hash_by_key",
                (skill_name, scope or "official", owner_id or ""))
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            row = _rt.read_one_any("skill_hash_by_id", (skill_id,))
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            rows = _rt.read_all_any("skill_hashes_by_name_scope",
                                    (skill_name, scope or "official"))
        return [(r[0] or "", r[1] or "") for r in rows]
    except Exception as e:
        log.warning(f"get_skill_hashes_by_name_scope failed: {e}")
        return []

def update_skill_content_hash(skill_id: int, content_hash: str, file_path: str = "") -> bool:
    """按主键 id 更新技能的 content_hash 和 file_path（OBS 同步后调用）"""
    try:
        # P3-5f：skill 在 runtime 域；lib 不可用/同域进程时回落原 SQL
        from src.kernel.domain_read import runtime_lib as _rt_lib_sch
        from src.kernel import cross_write as _xw_sch
        _sch_n: dict = {}
        _sch_done = False
        _rt_sch = _rt_lib_sch(foreign_only=True)
        if _rt_sch is not None:
            _rt_sch_ok = _rt_sch

            def _apply_sch() -> None:
                _sch_n["n"] = _rt_sch_ok.update_skill_content(int(skill_id), content_hash, file_path)

            _sch_done = _xw_sch.submit_if_lib(
                _rt_sch_ok, "runtime", "skill_content_update",
                f"skill-content:{skill_id}:{(content_hash or '')[:12]}",
                {"skill_id": int(skill_id), "content_hash": content_hash, "file_path": file_path},
                _apply_sch, log)
        if _sch_done:
            return int(_sch_n.get("n") or 0) > 0
        from src.domains.runtime import public as _rt_sch_any  # noqa: PLC0415
        with db_lock:
            if file_path:
                _n_upd = _rt_sch_any.exec_any(
                    "skill_update_content_hash", (content_hash, file_path, skill_id))
            else:
                _n_upd = _rt_sch_any.exec_any(
                    "skill_update_content_hash_only", (content_hash, skill_id))
            _mg_db.commit()
            return _n_upd > 0
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            if user_id:
                row = _rt.read_one_any("skill_id_user_owner", (skill_name, user_id))
                if row:
                    log.info(f"resolve_skill_id: '{skill_name}' hit user scope (owner={user_id})")
                    return row[0]
            if repo_key:
                row = _rt.read_one_any("skill_id_repo_owner", (skill_name, repo_key))
                if row:
                    log.info(f"resolve_skill_id: '{skill_name}' hit repo scope (owner={repo_key})")
                    return row[0]
            row = _rt.read_one_any("skill_id_official", (skill_name,))
            if row:
                return row[0]
    except Exception as e:
        log.warning(f"resolve_skill_id failed: {e}")
    return 0

def resolve_skill_dispatch(skill_name: str, skill_id: int = 0,
                           repo_key: str = "", user_id: str | int = "",
                           scene_id: str = "", node_tag: str = "",
                           agent_type: str = "") -> dict:
    """派发技能解析（Issue #979）：skill_id 权威直查 → 名字降级 + warning。

    - skill_id 非零且 skill 表命中（is_active=1）→ 直接返回该档（显式绑定，
      三档同名下锁定用户选择的档位）；
    - skill_id 缺失（老场景仅名字）或失效（技能被删/停用）→ 按
      resolve_skill_id 现有规则（user→repo→official）降级解析，并打
      warning（含场景/节点/降级原因），行为与既有名字派发一致；
    - 两者都解析不到 → skill_id=0（调用方按 #657 scope 门禁/内置包兜底）。

    agent_type 非空时做职责校验：解析到的技能 agent_type 与该节点角色不一致时，
    记 warning 并标 ``role_mismatch=True``——存量错绑（过滤上线前绑的）只告警不阻断，
    避免老场景突然停摆；新绑定由场景页下拉的职责过滤挡住。

    返回 {skill_id, skill_name, skill_path, degraded, skill, role_mismatch}。
    """
    out = {"skill_id": 0, "skill_name": skill_name or "",
           "skill_path": skill_name or "", "degraded": False, "skill": None,
           "role_mismatch": False}
    if not skill_name and not skill_id:
        return out
    if skill_id:
        row = get_skill_by_id(skill_id)
        if row:
            _name = row.get("skill_name") or skill_name or ""
            out.update(skill_id=int(skill_id), skill_name=_name,
                       skill_path=row.get("skill_path") or _name, skill=row)
            _role = (row.get("agent_type") or "").strip().lower()
            if agent_type and _role and _role != agent_type.strip().lower():
                out["role_mismatch"] = True
                log.warning(f"resolve_skill_dispatch: skill agent_type mismatch "
                            f"skill={_name} skill_role={_role} node_role={agent_type} "
                            f"(scene={scene_id or '-'} node={node_tag or '-'}, warn only)")
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
            _role = (row.get("agent_type") or "").strip().lower()
            if agent_type and _role and _role != agent_type.strip().lower():
                out["role_mismatch"] = True
                log.warning(f"resolve_skill_dispatch: skill agent_type mismatch (name path) "
                            f"skill={out['skill_name']} skill_role={_role} node_role={agent_type} "
                            f"(scene={scene_id or '-'} node={node_tag or '-'}, warn only)")
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            if repo_key:
                row = _rt.read_one_any("skill_scope_repo_owner", (skill_name, repo_key))
                if row:
                    info.update(found=True, matched=True, scope="repo",
                                skill_id=row[0], owner_id=row[1] or "")
                    return info
            row = _rt.read_one_any("skill_scope_official", (skill_name,))
            if row:
                info.update(found=True, matched=True, scope="official",
                            skill_id=row[0], owner_id="")
                return info
            row = _rt.read_one_any("skill_scope_user_any", (skill_name,))
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
                     category_id: int = 0, content_hash: str = "",
                     display_name: str | None = None) -> int | None:
    """OBS 模式技能记录 upsert（存在则更新固定 key，不存在则插入；v2.5 file_path + by 字段）。

    scope: user/repo；owner: user_id 或 repo_key。
    Issue #179: 新增 category_id 参数。
    Issue #444: 新增 content_hash 参数（UPDATE/INSERT 补写 content_hash 列，
    读时校验据此感知跨进程覆盖更新）；返回值改为 int | None（技能主键 id，
    失败返回 None——0/None 均为 falsy，既有 `if not ok` 判断语义不变）。
    Issue #3855: 新增 display_name 参数。显式非空值直接写入（新增/编辑录入的名称）；
    None（既有调用方/重新上传）表示「保留行内已有非空 display_name，为空才回填源名称」，
    避免重新上传把用户录入的名称冲回 SKILL.md 源名称。
    """
    try:
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        with db_lock:
            row = _rt.read_one_any(
                "skill_id_by_owner_scope",
                (skill_name, "repo" if scope == "repo" else "user", owner))
            if row:
                # Issue #3855：显式传入的名称优先；缺省时保留行内已有非空名称，
                # 为空则回填源名称（= skill_name），杜绝重新上传覆盖用户命名。
                _incoming_name = (display_name or "").strip() if display_name is not None else ""
                if _incoming_name:
                    _target_name = _incoming_name
                else:
                    # Issue #3855：存量非空名称原样保留（不清洗），历史 " Name " 不得被
                    # strip 静默改写；仅 None/""/纯空白视为空值回填源名称。
                    _current_name = "" if (len(row) <= 1 or row[1] is None) else str(row[1])
                    _target_name = _current_name if _current_name.strip() else skill_name
                # P3-5f：skill 在 runtime 域
                from src.kernel.domain_read import runtime_lib as _rt_lib_uso
                from src.kernel import cross_write as _xw_uso
                _uso_done = False
                _rt_uso = _rt_lib_uso(foreign_only=True)
                if _rt_uso is not None:
                    _rt_uso_ok = _rt_uso
                    _uso_done = _xw_uso.submit_if_lib(
                        _rt_uso_ok, "runtime", "skill_obs_update",
                        f"skill-obs:{row[0]}:{(content_hash or '')[:12]}",
                        {"skill_id": int(row[0]), "object_key": object_key,
                         "display_name": _target_name, "agent_type": agent_type or "",
                         "description": description or "", "category_id": category_id,
                         "updated_by": created_by or "", "content_hash": content_hash or ""},
                        lambda: _rt_uso_ok.update_skill_obs(
                            int(row[0]), object_key, _target_name, agent_type or "",
                            description or "", category_id, created_by or "",
                            content_hash or ""), log)
                if not _uso_done:
                    _rt.exec_any(
                        "skill_obs_update",
                        (object_key, _target_name, agent_type or "", description or "",
                         category_id, created_by or "", content_hash or "", row[0]))
                _mg_db.commit()
                return int(row[0])
            # P3-5f：skill 在 runtime 域（新建行）
            from src.kernel.domain_read import runtime_lib as _rt_lib_usi
            from src.kernel import cross_write as _xw_usi
            _usi_out: dict = {}
            _usi_done = False
            _rt_usi = _rt_lib_usi(foreign_only=True)
            if _rt_usi is not None:
                _rt_usi_ok = _rt_usi

                def _apply_usi() -> None:
                    _usi_out["id"] = _rt_usi_ok.insert_skill(
                        skill_name, (display_name or "").strip() or skill_name,
                        description or "", agent_type or "",
                        object_key, scope, owner, created_by or "", created_by or "",
                        category_id, content_hash or "")

                _usi_done = _xw_usi.submit_if_lib(
                    _rt_usi_ok, "runtime", "skill_obs_insert",
                    f"skill-obs-new:{skill_name}:{scope}:{owner}:{(content_hash or '')[:12]}",
                    {"skill_name": skill_name, "description": description or "",
                     "agent_type": agent_type or "", "file_path": object_key, "scope": scope,
                     "owner_id": owner, "created_by": created_by or "",
                     "category_id": category_id, "content_hash": content_hash or ""},
                    _apply_usi, log)
            if _usi_done:
                _mg_db.commit()
                return int(_usi_out.get("id") or 0)
            _rt.exec_any(
                "skill_obs_insert",
                (skill_name, (display_name or "").strip() or skill_name, description or "",
                 agent_type or "", object_key, scope, owner, created_by or "",
                 created_by or "", category_id, content_hash or ""))
            _mg_db.commit()
            _new = _rt.read_one_any(
                "skill_id_by_owner_scope",
                (skill_name, "repo" if scope == "repo" else "user", owner))
            return int(_new[0]) if _new else 0
    except Exception as e:
        log.warning(f"upsert_skill_obs failed: {e}")
        return None

def create_skill(user_id: int, skill_name: str, agent_type: str = "", description: str = "",
                 parent_agent_type: str = "", scope: str = "user", owner_id: str = "",
                 created_by: str = "") -> bool:
    try:
        with db_lock:
            cb = created_by or str(user_id)
            # P3-5f：skill 在 runtime 域（INSERT IGNORE 幂等）
            from src.kernel.domain_read import runtime_lib as _rt_lib_csk
            from src.kernel import cross_write as _xw_csk
            _csk_done = False
            _csk_owner = (owner_id or str(user_id)) if scope == "repo" else str(user_id)
            _rt_csk = _rt_lib_csk(foreign_only=True)
            if _rt_csk is not None:
                _rt_csk_ok = _rt_csk
                _csk_done = _xw_csk.submit_if_lib(
                    _rt_csk_ok, "runtime", "skill_row_create",
                    f"skill-create:{skill_name}:{scope}:{_csk_owner}",
                    {"skill_name": skill_name, "description": description,
                     "agent_type": agent_type, "scope": scope, "owner_id": _csk_owner,
                     "created_by": cb},
                    lambda: _rt_csk_ok.create_skill_row(skill_name, description, agent_type,
                                                        scope, _csk_owner, cb), log)
            if not _csk_done:
                from src.domains.runtime import public as _rt_csk_any  # noqa: PLC0415
                _rt_csk_any.exec_any(
                    "skill_row_create_ignore",
                    (skill_name, skill_name, description, agent_type, skill_name,
                     scope, _csk_owner, cb, cb))
            _mg_db.commit()
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
            c = _mg_db.cursor()
            # P3-5f：skill 在 runtime 域
            from src.kernel.domain_read import runtime_lib as _rt_lib_dsk
            from src.kernel import cross_write as _xw_dsk
            _dsk_n: dict = {}
            _dsk_done = False
            _dsk_owner = (owner_id or str(user_id)) if scope == "repo" else str(user_id)
            _rt_dsk = _rt_lib_dsk(foreign_only=True)
            if _rt_dsk is not None:
                _rt_dsk_ok = _rt_dsk

                def _apply_dsk() -> None:
                    _dsk_n["n"] = _rt_dsk_ok.delete_skill_row(skill_name, scope, _dsk_owner)

                _dsk_done = _xw_dsk.submit_if_lib(
                    _rt_dsk_ok, "runtime", "skill_row_delete",
                    f"skill-del:{skill_name}:{scope}:{_dsk_owner}",
                    {"skill_name": skill_name, "scope": scope, "owner_id": _dsk_owner},
                    _apply_dsk, log)
            if _dsk_done:
                deleted = int(_dsk_n.get("n") or 0) > 0
            else:
                # skill 归运行域：单调 DELETE 走任意拓扑具名查询（参数化 scope，不再各写一条 SQL）
                _dsk_scope = "repo" if scope == "repo" else "user"
                _dsk_target = (owner_id or str(user_id)) if scope == "repo" else str(user_id)
                deleted = _rt.exec_any("skill_delete_by_owner_scope",
                                       (skill_name, _dsk_scope, _dsk_target)) > 0
            _mg_db.commit()
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
    # skill 归运行域：具名查询（任意拓扑），列序与原 SQL 一致
    rows = _rt.read_all_any("skills_user_by_owner", (str(user_id),))
    return [{"id": r[0], "skill_name": r[1], "agent_type": r[2] or "", "description": r[3] or "", "scope": r[4], "owner_id": r[5], "file_path": r[6] or "", "category_id": r[7] or 0, "created_at": ""} for r in rows]

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
                     org_id: str = "", host_user_id: str = "", webhook_secret: str = "",
                     **extra_cols) -> bool:
    """写入仓库级托管配置（repo_key 带平台前缀，一仓库一行 upsert）。

    org_id > 0 时绑定到指定组织；org_id=0 时为全局（兼容旧逻辑）。
    host_user_id: Issue #372 路径 B 托管时写入当前用户 ID（无全局 bot token 场景）。
    webhook_secret: Issue #372 路径 B webhook 验证密钥（随机生成，与 token 解耦）。
    """
    from src.kernel.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo_key)
    full_name = from_repo_key(repo_key)
    # D105 第三批：org_id 统一 str，哨兵 0/None → ''（''=全局/未归属）
    org_id = "" if org_id in (None, "", 0, "0") else str(org_id)
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
            c = _mg_db.cursor()
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
            _mg_db.commit()
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
            c = _mg_db.cursor()
            c.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=?", (scene_id or "", repo_key))
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Set repo scene failed: {e}")

def get_repo_scene(repo_key: str) -> str:
    """读取仓库开发场景（managed_repos.dev_scene），无记录或未设置返回空字符串。

    悬空防护：dev_scene 指向的场景已被删除/从未存在时返回空串（按未配置处理）。
    典型成因是删除场景不清 dev_scene，悬空 id 会让建单预校验和派发读到幽灵场景
    （或回落内置 default 被其 sre 节点拦死）。解析查询本身失败时保持原值返回，
    不改变既有行为。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            _scene = (row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"Get repo scene failed: {e}")
        return ""
    if not _scene:
        return ""
    try:
        from src.domains.mgmt.pipeline_config import get_pipeline_config
        _cfg = get_pipeline_config(_scene, repo_key=repo_key, use_cache=False)
        if not _cfg or _cfg.get("fallback"):
            log.warning(f"Repo dev_scene dangling: repo={repo_key} scene={_scene}"
                        f" -> treated as unset")
            return ""
    except Exception as e:
        log.warning(f"Repo dev_scene validity check failed ({repo_key}/{_scene}): {e}")
    return _scene

def get_scene_recommend_config(repo_key: str) -> dict:
    """读取仓库智能推荐配置（managed_repos.scene_recommend_config）。

    返回 {"fallback_scene": "", "rules": [{"scene_id": "", "condition": ""}]}。
    无记录或未设置返回默认值。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
            c.execute("UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?", (cfg, repo_key))
            if not c.rowcount:
                # #487 upsert 兜底：仓库行缺失时补行（repo_key 主键幂等）再写入
                _provider, _owner, _name = parse_repo_key(repo_key)
                c.execute("INSERT OR IGNORE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                          "VALUES (?,?,?,?)",
                          (repo_key, _owner, _name, f"{_owner}/{_name}"))
                c.execute("UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?", (cfg, repo_key))
                if not c.rowcount:
                    _mg_db.rollback()
                    log.warning(f"Set scene recommend config failed: no managed_repos row for {repo_key}")
                    return False
            _mg_db.commit()
        log.info(f"Scene recommend config saved: repo={repo_key} fallback={fallback_scene} rules={len(rules)}")
        return True
    except Exception as e:
        try:
            _mg_db.rollback()
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
            c = _mg_db.cursor()
            c.execute(
                "UPDATE managed_repos SET host_status=?, host_step=?, host_error=?, "
                "host_updated_at=datetime('now') WHERE repo_key=?",
                (_status, step or "", error or "", repo_key))
            _mg_db.commit()
    except Exception as e:
        log.warning(f"Set host status failed: {e}")



def get_managed_repo_owner_row(repo_key: str) -> "dict | None":
    """读单行托管归属（repo_key/org_id/host_user_id/host_status/webhook_id）。

    托管前置校验用（Issue #3580 G2）：判断该仓库是否已被他人/他组织托管。
    不存在返回 None；读取异常按"无归属信息"返回 None（不阻断托管主流程）。
    """
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute(
                "SELECT repo_key, repo_owner, repo_name, webhook_id, org_id, "
                "host_user_id, host_status FROM managed_repos WHERE repo_key=? LIMIT 1",
                (repo_key,))
            row = c.fetchone()
    except Exception as e:
        log.warning(f"Get managed repo owner row failed: {e}")
        return None
    if not row:
        return None
    return {"repo_key": row[0], "repo_owner": row[1], "repo_name": row[2],
            "webhook_id": row[3], "org_id": row[4] or "", "host_user_id": row[5] or "",
            "host_status": row[6] or ""}


def get_host_status(repo_key: str) -> str:
    """读取仓库托管状态（managed_repos.host_status），无记录返回空串。"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    try:
        with db_lock:
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
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

def get_managed_repos(user_id: int, org_id: str = "", personal_org: bool = False) -> list[dict]:
    """返回该用户可见（JOIN user_repos）的托管仓库；对外 full_name 无前缀、repo_key 带前缀。

    Issue #2264：个人空间（personal_org=True）不再隐藏「已关联团队组织」的仓库——
    用户是仓库 owner，关联组织后仓库不应在个人列表里凭空消失；返回 org_id/org_name
    供前端展示组织徽标。团队组织（personal_org=False）仍严格按 m.org_id=? 隔离。
    """
    from src.kernel.repo_key import from_repo_key
    with db_lock:
        c = _mg_db.cursor()
        _base = (
            "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
            "m.bot_gitcode_token, m.gitcode_webhook_token, m.bot_username, m.created_at, "
            "COALESCE(u.is_join,0), COALESCE(u.is_admin,0), COALESCE(m.host_status,''), "
            "COALESCE(m.org_id,''), COALESCE(o.name,''), COALESCE(m.is_org_repo,0) "
            "FROM managed_repos m "
            "LEFT JOIN user_repos u ON u.repo_key=m.repo_key AND u.user_id=? "
            "LEFT JOIN organization o ON o.id=m.org_id "
            "WHERE u.repo_key IS NOT NULL "
        )
        try:
            if org_id and not personal_org:
                # 团队组织：严格隔离
                c.execute(_base + "AND m.org_id=? ORDER BY m.full_name", (user_id, org_id))
            else:
                # 个人空间/未指定组织：显示该用户全部托管仓库（含已关联组织）
                c.execute(_base + "ORDER BY m.full_name", (user_id,))
        except Exception as e:
            log.warning(f"get_managed_repos join failed: {e}")
            c.execute(
                "SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, "
                "bot_gitcode_token, gitcode_webhook_token, bot_username, created_at, 0, 0, "
                "COALESCE(host_status,''), COALESCE(org_id,''), '', COALESCE(is_org_repo,0) "
                "FROM managed_repos ORDER BY full_name")
        rows = c.fetchall()
        # autocommit=False 下 SELECT 会打开事务，同线程连接后续 SELECT 看到旧快照。
        # commit 释放事务，确保下次查询看到最新数据（修复 /repo 返回老列表）。
        _mg_db.commit()
        return [{
            "repo_key": r[0], "repo_owner": r[1], "repo_name": r[2],
            "full_name": from_repo_key(r[0]), "webhook_id": r[4],
            "bot_gitcode_token": r[5] or "", "gitcode_webhook_token": r[6] or "",
            "bot_username": r[7] or "", "created_at": r[8],
            "is_join": bool(r[9]), "is_admin": bool(r[10]),
            "host_status": (r[11] or "") if len(r) > 11 else "",
            "org_id": (r[12] or "") if len(r) > 12 else "",
            "org_name": (r[13] or "") if len(r) > 13 else "",
            "is_org_repo": bool(r[14]) if len(r) > 14 else False,
        } for r in rows]


def update_managed_repo_admin(user_id: int, repo_key: str, is_admin: bool) -> bool:
    """更新该用户在 user_repos 的管理员状态（用户级）"""
    from src.kernel.repo_key import to_repo_key
    try:
        with db_lock:
            c = _mg_db.cursor()
            c.execute("UPDATE user_repos SET is_admin=? WHERE user_id=? AND repo_key=?", (int(is_admin), user_id, to_repo_key(repo_key)))
            _mg_db.commit()
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
    from src.domains.mgmt.vcs import create_provider
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
            c = _mg_db.cursor()
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
        from src.domains.mgmt.vcs import create_provider
        _bot_token_col = create_provider(provider).token_storage_field()
        import re as _re
        # F4/D106：卡列空平台（gitee/gitlab 终态）无列可查——改查 grape_t_repo_credentials
        # （rc 唯一权威；密文+明文双形态比对与列路径同口径）。
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _bot_token_col or ""):
            _bot_token_col = ""
        with db_lock:
            c = _mg_db.cursor()
            # N3：列值已加密——比对值须同形态（明文兼容期列里可能新旧混合，
            # 两种形态都查：密文等值 + 明文等值）
            _enc = _encrypt_repo_secret(bot_token)
            if _bot_token_col:
                _q = (f"SELECT repo_key FROM managed_repos "
                      f"WHERE ({_bot_token_col}=? OR {_bot_token_col}=?) AND repo_key!=?")
                c.execute(_q, (_enc, bot_token, repo_key))
                _related_repos = [r[0] for r in c.fetchall()]
            else:
                # 凭据表归 insight：共用同一凭据值的仓库排查走该域原语
                _related_repos = _insight_public.repo_keys_sharing_value(
                    "bot", (_enc, bot_token), f"{provider}:", repo_key)
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
            c = _mg_db.cursor()
            c.execute("UPDATE managed_repos SET bot_username=? WHERE repo_key=?", (bot_username, repo_key))
            _mg_db.commit()
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
                _row = _mg_db.cursor().execute(
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
        from src.domains.mgmt.vcs import create_provider
        _col = create_provider(provider).token_storage_field()
    except Exception:
        _col = ""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _col or ""):
        _col = ""
    with db_lock:
        c = _mg_db.cursor()
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
            # 凭据表归 insight：单条读取走该域原语（行存在即返回，含空值——与原语义一致）
            row = _insight_public.repo_credential_meta(repo_key, "bot")
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

    Issue #3713（A2A/tasks 鉴权面收口）：鉴权面拿的是**可用性**语义，而
    `read_stored_token` 契约明文「不检查过期」。链首新增 rc 行后，若这一面照旧，
    过期 rc 行的旧值仍会被当成可用 token 发出去（「一边短路一边放行」）。
    所以：rc 行存在但不可用（空/解不开/过期）时，把**会读这一行的两级**
    （rc_bot 与 repo_bot——后者 `_fetch` 也优先读同一行）一并摘掉，回到「无行」语义，
    由后续级别（归属者用户凭据）兜底；绝不把旧值发出去。
    """
    from src.kernel.token_resolver import ExplicitRepoBotToken, TokenResolver
    from src.domains.mgmt.vcs import create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    resolver = TokenResolver(provider)
    # R25.1: 跳过第 3 级 GlobalBotToken（保持"no_global"语义）
    resolver.strategies = [s for s in resolver.strategies if s.level != "global_bot"]
    _head = ExplicitRepoBotToken(provider)
    # 执行面：expired 已按「跳过本级」处理（stage 返回空）；鉴权面必须自己看过期，
    # 否则 read_stored_token 会把已知过期的旧值当可用 token 发出去（C3 的配套）。
    if _head.stage(repo_key) or _head.expired(repo_key):
        resolver.strategies = [s for s in resolver.strategies
                               if s.level not in ("rc_bot", "repo_bot")]
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

def _bot_seed_value(provider: str, kind: str) -> str:
    """种子文件里某平台的值（kind = token | username）。运行期仅在库内无值时用。"""
    try:
        from src.kernel.config import read_bot_seed_file

        return (read_bot_seed_file().get(provider) or {}).get(kind, "") or ""
    except Exception:
        return ""


def _read_gbt_file_field(field: str) -> str:
    """从平台 bot 种子文件读取指定字段（库内无值时的一次性 fallback；D110 改名）。"""
    import json, os
    try:
        from src.kernel.config import bot_seed_path

        _gbt_path = bot_seed_path()
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                return json.load(f).get(field, "") or ""
    except Exception as e:
        log.warning(f"Failed to read {field} from bot seed file: {e}")
    return ""

def _read_gbt_file_usernames() -> str:
    """从平台 bot 种子文件读取 bot_usernames（JSON 数组，返回原始字符串）。"""
    import json, os
    try:
        from src.kernel.config import bot_seed_path

        _gbt_path = bot_seed_path()
        if os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as f:
                us = json.load(f).get("bot_usernames") or []
                return json.dumps(us) if us else ""
    except Exception:
        pass
    return ""

def _platform_bot_row(provider: str) -> dict:
    """D110：平台行 bot 事实（username / token 密文 / 状态），60s 缓存；读不到返回 {}。

    真源 = grape_t_repo_platforms（一平台一个 bot）；表未就绪/无行时返回 {}，
    调用方按 meta → 文件 的顺序回落（向前兼容）。
    """
    import time as _t
    key = ("platform_bot", provider)
    cached = _global_bot_cache.get(key)
    now = _t.time()
    if cached and now < cached[1]:
        return cached[0]
    data: dict = {}
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            row = RepoPlatformRepo.get(s, provider)
            if row is not None:
                data = {
                    "username": getattr(row, "bot_username", "") or "",
                    "token_enc": getattr(row, "bot_token_enc", "") or "",
                    "token_status": getattr(row, "bot_token_status", "") or "unknown",
                    "checked_at": getattr(row, "bot_checked_at", "") or "",
                }
    except Exception:
        data = {}
    if data.get("token_enc") or data.get("username"):
        _global_bot_cache[key] = (data, now + _GLOBAL_BOT_CACHE_TTL)
    else:
        _global_bot_cache.pop(key, None)
    return data


def _platform_bot_token(provider: str) -> str:
    """D110 第 2 级来源：平台行 bot token（解密后明文；空=未配置/解密失败）。"""
    enc = _platform_bot_row(provider).get("token_enc") or ""
    if not enc:
        return ""
    try:
        return _decrypt_repo_secret(enc) or ""
    except Exception:
        return ""


def get_global_gitcode_bot_token() -> str:
    """统一入口：平台行 → DB meta → 文件 fallback（60s 缓存）。

    D110: grape_t_repo_platforms.bot_token_enc 是第 2 级来源（一平台一个 bot）；
    meta / 文件只在平台行为空时兜底（向前兼容，迁移前后行为一致）。
    """
    _plat = _platform_bot_token("gitcode")
    if _plat:
        return _plat
    return _cached_get_meta("global_bot_gitcode_token",
                            file_fallback_fn=lambda: _bot_seed_value("gitcode", "token"))

def get_global_github_bot_token() -> str:
    """统一入口：平台行 → DB meta → 文件 fallback（60s 缓存，GitHub 版）。"""
    _plat = _platform_bot_token("github")
    if _plat:
        return _plat
    return _cached_get_meta("global_bot_github_token",
                            file_fallback_fn=lambda: _bot_seed_value("github", "token"))

def get_global_github_bot_username() -> str:
    """统一入口：github 卡 bot_username 优先 → meta 兜底（D15，2026-11-12）。

    B 层收敛：GitHub bot 身份归仓库平台卡字段（get_platform_card("github").bot_username），
    存量 meta global_bot_github_username（+文件 fallback）保留为卡空时的兜底
    （兼容窗口）。消费方（event_pipeline self-comment / repos 解绑协作者）零改动。

    Issue #345: DB 是权威源。文件 fallback 确保迁移失败或集群竞态时系统仍可用。
    """
    _plat = _platform_bot_row("github").get("username") or ""
    if _plat:
        return _plat
    try:
        from src.domains.runtime.public import get_platform_card
        _card = get_platform_card("github")
        if _card is not None and _card.bot_username:
            return _card.bot_username
    except Exception:
        pass
    return _cached_get_meta("global_bot_github_username",
                            file_fallback_fn=lambda: _bot_seed_value("github", "username"))

def get_global_bot_usernames() -> list[str]:
    """统一入口：各卡 bot_username 纯卡派生并集（D16→D22，2026-11-12 拍板）。

    self-comment 检测的 bot 名单 = list_vcs_platforms() 各卡 bot_username
    非空并集（新平台卡配 bot 自动进名单）。meta global_bot_usernames 兜底
    读链按用户拍板退役；存量 meta 键成死配置，清理挂账下批。
    """
    out: list[str] = []
    # D110：平台行 bot_username 是事实来源（一平台一个 bot），卡是卡位缺失时的兜底
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            for _row in RepoPlatformRepo.list_all(s):
                _u = getattr(_row, "bot_username", "") or ""
                if _u and _u not in out:
                    out.append(_u)
    except Exception:
        pass
    try:
        from src.domains.runtime.public import list_vcs_platforms, get_platform_card
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
    from src.kernel.token_resolver import TokenResolver
    from src.domains.mgmt.vcs import create_provider_from_repo_key
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

    顺序（Issue #21 R1）：
      ① task.repo_gitcode_token（历史行值）
      ② 显式配置的仓库 bot 行（链首 rc_bot；不可用只告警，不在此硬失败）
      ③ 派发身份个人令牌（默认 on）
      ④ 原链：github: → get_github_token / 其他 → get_repo_token_with_refresh

    """
    tok = task.get("repo_gitcode_token") or ""
    if tok:
        return tok
    repo_key = task.get("repo_key") or ""
    # ① 显式配置的仓库 bot 行优先（D101 后任务行不再存 bot token，
    #    "任务自带的仓库 bot 令牌"的现行形态就是 rc 行）。用链首这一级精确读一次：
    #    可用 → 直接用；不可用 → 记一条 warning 并继续往下（不在这里硬失败，
    #    真因由建树面的 resolve_exec_token 归因，避免抢走失败分类）。
    if repo_key:
        try:
            from src.kernel.token_resolver import ExplicitRepoBotToken
            from src.domains.mgmt.vcs import create_provider_from_repo_key

            _head = ExplicitRepoBotToken(create_provider_from_repo_key(repo_key))
            _explicit_token, _explicit_stage = _head.resolve(repo_key)
            if _explicit_token:
                return _explicit_token
            if _explicit_stage:
                log.warning(f"get_task_token: explicit repo bot row unusable "
                            f"({_explicit_stage}) repo={repo_key} —— 继续看派发身份/原链")
        except Exception as e:  # noqa: BLE001 - 读 rc 失败不阻断取令牌
            log.debug(f"get_task_token: explicit repo bot probe skipped: {e}")

    # ② Issue #21 第 2 步：按派发身份取个人令牌（shadow/off 返回空 → 旧行为）
    try:
        from src.domains.mgmt.user.task_token_identity import user_identity_token

        _identity_token, _ = user_identity_token(task, repo_key)
        if _identity_token:
            return _identity_token
    except Exception as e:  # noqa: BLE001 - 认人失败不得影响取令牌
        log.debug(f"get_task_token: dispatch identity step skipped: {e}")
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
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
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
            c = _mg_db.cursor()
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
            c2 = _mg_db.cursor()
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
            c = _mg_db.cursor()
            c.execute("UPDATE managed_repos SET host_vcs_username=? WHERE repo_key=? AND (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0')",
                      (login, repo_key))
            _mg_db.commit()
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
    from src.kernel.token_resolver import TokenResolver
    from src.domains.mgmt.vcs import create_provider_from_repo_key
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
    from src.domains.mgmt.vcs import create_provider_from_repo_key
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
                c = _mg_db.cursor()
                # devops_agent_session 归运行域：具名查询（任意拓扑）
                if _rt.read_one_any("devops_agent_session_repo_token", (repo_key,)):
                    return False
        except Exception:
            pass
    # 路径 B: managed_repos.bot_gitcode_token
    # R14.13: 路径 B 仓库（host_user_id > 0）跳过此层——与 get_repo_token_with_refresh 守卫对齐，
    # 防止脏写入的 bot_gitcode_token 导致判断不一致
    if _hid == 0:
        try:
            with db_lock:
                c = _mg_db.cursor()
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
                c = _mg_db.cursor()
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
    from src.kernel.token_resolver import TokenResolver
    from src.domains.mgmt.vcs import create_provider
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
            c = _mg_db.cursor()
            # D43/S63：列名与前缀子句卡/目录派生——token 列走适配器；
            # 默认平台（内置卡序首）含裸键（无前缀存量数据），其余平台按前缀
            from src.domains.mgmt.vcs import create_provider as _cp_pb
            _tok_col = _cp_pb(provider).token_storage_field()
            _prefix_clause = _path_b_prefix_clause(provider)
            # F4/D106: card column empty (gitee/gitlab final state) — path B determination changed to
            # "no non-empty bot row in rc" (NOT EXISTS; decryption comparison is not usable in SQL,
            # empty-value rows are treated as absent, consistent with the read-side rc semantics).
            if not _tok_col:
                # 凭据表归 insight：先取「已有非空 bot 凭据」的仓库集合，再在本域行集上过滤
                # （等价于原 NOT EXISTS 关联子查询：空值行视同无凭据）
                _has_bot = _insight_public.repo_keys_with_nonempty_credential("bot")
                c.execute(
                    "SELECT repo_key, repo_owner, repo_name, host_user_id FROM managed_repos "
                    f"WHERE (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0') "
                    f"AND {_prefix_clause} "
                )
                rows = [r for r in (c.fetchall() or []) if r[0] not in _has_bot]
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
        from src.domains.mgmt.vcs import create_provider
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
            c = _mg_db.cursor()
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
            _mg_db.commit()
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
            from src.domains.mgmt.vcs import create_provider
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
        c = _mg_db.cursor()
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
                from src.domains.runtime.public import get_platform_card
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
            c = _mg_db.cursor()
            c.execute("SELECT full_name, webhook_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if not row:
                return None
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("UPDATE user_repos SET is_join=0 WHERE repo_key=?", (repo_key,))
            # D101 方案三（T5）：解绑清理段删除——repo_gitcode_token/repo_github_token
            # 列已退役（无写入方），无物可清；token_status 语义随 session 级消亡。
            _mg_db.commit()
            return {"full_name": from_repo_key(repo_key), "webhook_id": row[1]}
    except Exception as e:
        log.warning(f"Remove managed repo failed: {e}")
        return None

def get_all_managed_repos() -> list[dict]:
    """Admin 用：获取所有托管记录（repo_key 带前缀，full_name 无前缀）"""
    from src.kernel.repo_key import from_repo_key
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, created_at, host_status"
                  " FROM managed_repos ORDER BY full_name")
        return [{"repo_key": r[0], "repo_owner": r[1], "repo_name": r[2],
                 "full_name": from_repo_key(r[0]), "webhook_id": r[4], "created_at": r[5],
                 "host_status": r[6] or ""} for r in c.fetchall()]

def get_all_bot_usernames() -> list[str]:
    """获取所有托管仓库配置的 bot 用户名列表（用于 self-comment 检测）。

    Issue #345: 全局 bot 用户名改用统一入口（DB 优先 → 文件 fallback）。
    """
    result = []
    with db_lock:
        c = _mg_db.cursor()
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
    # W2：meta 基表在运行域；读原语自带「自有 DSN 优先 + 单库回落进程主库」，
    # 因此本文件不再留一份跨域 SQL 兜底。
    from src.domains.runtime import public as _rt  # noqa: PLC0415
    try:
        return _rt.get_meta_value_any(key)
    except Exception as e:  # noqa: BLE001
        from src.kernel.db_errors import missing_schema
        if missing_schema(e):
            # worker 本地库无 meta 表：vcs 平台卡等配置读回空值即可
            log.debug(f"get_meta_value skipped (no local table): {key}: {e}")
        else:
            log.warning(f"get_meta_value failed: {key}: {e}")
        return ""

def set_meta_value(key: str, value: str):
    """写入配置值到 meta 表"""
    # W2：meta 基表在运行域；写原语（幂等 upsert）同样自带单库回落
    from src.domains.runtime import public as _rt  # noqa: PLC0415
    try:
        _rt.set_meta_value_any(key, value)
    except Exception as e:  # noqa: BLE001
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
    """返回部署级**公网基址**（历史键名 webhook_url；不是某平台的回调全路径）。

    语义（2026-11-15 用户确认：只补注释，不改行为）：
    - 这里填的是 base（如 `https://grape.example.com`）。**平台路径一律由平台卡
      `webhook_path` 追加**（`/webhook/gitcode`、`/webhook/gitlab` …），所以值里带的
      `/webhook/xxx` 会被 `_webhook_url_for_provider` 丢弃——填 `https://x/webhook/gitcode`
      与填 `https://x` 等价。
    - 读取优先级：configs/server-config.json → meta.webhook_url → 空串。
      不设 localhost 兜底，让 repos.py「未配置即拒（422）」的守卫生效。
    - 与 `get_server_base_url()` 同一事实的两个键：后者优先 server_base_url、并会把本键
      剥掉 `/webhook/*` 后缀使用；两个键目前并存（合并成单一键会影响 6 个读取点与自动探测
      落库路径，2026-11-15 裁定暂不合并，只在本注释与配置示例里写明口径）。
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
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        for src_key, meta_key in _oauth_keys:
            val = data.get(src_key, "") or ""
            if not val:
                continue
            # meta 基表在运行域：幂等「不存在才写」，写原语自带单库回落（无需本地 SQL）
            if _rt.set_meta_if_absent(meta_key, val):
                loaded.append(meta_key)
        if loaded:
            log.info(f"Loaded OAuth credentials from global-bot-token.json (fallback): {loaded}")
    except Exception as e:
        log.warning(f"migrate_oauth_providers: fallback sync failed: {e}")

def _sync_providers_from_config(data: dict) -> None:
    """把 oauth-providers.json 的**渠道定义**同步到 DB 表（不再写 meta）。

    为什么要改（2026-11-21）：`grape_t_oauth_channels` 已经承载全套字段 ——
    provider / name / type / auth_url / token_url / user_url / emails_endpoint /
    scope / pkce_enabled / id_field / redirect_uri / brand_color / accept_header /
    client_id_enc / client_secret_enc / enabled —— 而且 `providers.py::_load_providers`
    的读取顺序是「文件卡 → meta 逐键覆盖 → **表整卡覆盖**」，表是最后一层，即运行时真源。
    既然表能承载全部信息，meta 里的 `oauth_providers_config` 与 `oauth_<p>_client_id/secret`
    只是历史导入源：每次启动再写一遍既是空转，又会因同值重写触发
    `meta.PRIMARY` 1062（set_meta_value_any 的 rowcount 判据已单独修好，但这条写入
    本就不该存在）。

    现在的分工：
      · 文件（随仓发布）  → 首次导入 / 表未就绪时的基底
      · 表               → 运行时真源（管理台写、`platform.migration` 负责从文件灌入）
      · meta             → 仅作为老部署的读取兼容层保留（本函数不再写入）
    """
    from src.domains.platform.public import is_table_backed

    providers = [str(p or "").strip() for p in (data or {}).keys()]
    providers = [p for p in providers if p and not p.startswith("_")]
    if not providers:
        return
    if not is_table_backed():
        # 表未就绪（老库/单库）：文件本身就是基底，无需落 meta；此处只留一条调试
        log.debug("oauth providers: table not ready, keep file cards as source: %s", providers)
        return
    log.info("OAuth providers source of truth is grape_t_oauth_channels "
             "(file cards are the import source); meta sync skipped: %s", providers)


def migrate_accounts_from_json():
    """首次运行加载配置：迁移旧数据到 meta 表。

    OAuth 配置迁移由 migrate_oauth_providers() 统一处理。
    （default 账户迁移已随 user_accounts 清退删除：现网该表 0 行，迁移恒零命中。）
    """
    # OAuth 配置由 migrate_oauth_providers 统一处理
    migrate_oauth_providers()

def link_user_provider(user_id: int, provider: str, external_id: str,
                        access_token: str, refresh_token: str = "") -> bool:
    """通用 OAuth 绑定——统一写入 user_credentials。

    D32-补丁：不再写 user_accounts 老表专属列（gitcode_id/github_id）。
    此函数已无外部调用方（_sync_legacy_binding 已删除），保留仅作历史兼容。
    """
    provider_key = f"{provider}_oauth"
    try:
        with db_lock:
            c = _mg_db.cursor()
            # A2：显式写 kind 列 —— DDL 的 DEFAULT '' 在 MySQL 严格模式下会被
            # _strip_text_default 剥掉（TEXT 不能带默认），不写列会报 1364。
            c.execute("INSERT OR REPLACE INTO user_credentials (user_id, provider, kind, config_json, updated_at) VALUES (?,?,?,?,datetime('now'))",
                      (user_id, provider_key, "", json.dumps({
                          f"{provider}_id": external_id,
                          "access_token": access_token,
                          "refresh_token": refresh_token,
                          "fetched_at": int(time.time())
                      }, ensure_ascii=False)))
            _mg_db.commit()
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
        from src.kernel.session_manager import db_lock as _sm_dl
        from src.domains.mgmt.auth.credential_crypto import decrypt
        with _sm_dl:
            c = _mg_db.cursor()
            try:
                c.execute(
                    "SELECT refresh_token FROM external_identities "
                    "WHERE grape_user_id=? AND provider=? AND status=1",
                    (user_id, provider),
                )
                row = c.fetchone()
            finally:
                try:
                    c.close()
                except Exception:
                    pass
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

# Issue #3054 后续（2026-11-22 评审补强④）：刷新成功但上游仍 401 的退避。
# GitHub 每次 refresh 都会轮换 refresh_token：上游持续 401 时逐请求刷新 =
# 用掉一枚、写回一枚，风暴式轮换且每一轮都以用户重新授权收场。
_REFRESH_MIN_INTERVAL_S = 60.0
_REFRESH_LAST: "dict[tuple, float]" = {}


def _read_access_token_from_identity(user_id: int, provider: str) -> str:
    """读 external_identities 里当前 access_token（解密）；无行/失败返回空。"""
    try:
        from src.kernel.session_manager import db_lock as _sm_dl
        from src.domains.mgmt.auth.credential_crypto import decrypt
        with _sm_dl:
            c = _mg_db.cursor()
            try:
                c.execute(
                    "SELECT access_token FROM external_identities "
                    "WHERE grape_user_id=? AND provider=? AND status=1",
                    (user_id, provider))
                row = c.fetchone()
            finally:
                try:
                    c.close()
                except Exception:
                    pass
        if not row or not row[0]:
            return ""
        return decrypt(row[0])
    except Exception as e:
        log.warning(f"_read_access_token_from_identity failed: {e}")
        return ""


def mark_identity_reauth_required(user_id: int, provider: str, reason: str = "") -> None:
    """把身份行打成 status=4（绑定仍在、token 失效 → 引导重新授权）。

    调用点：invalid_grant/400（refresh_token 彻底失效），以及
    `with_oauth_token` 里“刷新成功但重试仍 401”（refresh 有效、上游同样拒绝）。
    """
    try:
        from src.kernel.session_manager import db_lock as _sm_dl
        with _sm_dl:
            db.execute(
                "UPDATE external_identities SET status=4, updated_at=datetime('now') "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (user_id, provider))
            _mg_db.commit()
        log.warning(f"identity marked reauth-required: user={user_id} provider={provider} {reason}")
    except Exception as e:
        log.warning(f"mark_identity_reauth_required failed: {user_id}/{provider}: {e}")


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

    # 退避：刚刷新过又要求刷新 → 不碰 OAuth（否则每轮都轮换 refresh_token）。
    # 返回当前 access_token 让调用方重试；若上游仍 401，由 with_oauth_token 打 status=4。
    _last = _REFRESH_LAST.get((user_id, provider))
    if _last and (time.monotonic() - _last) < _REFRESH_MIN_INTERVAL_S:
        log.warning(f"refresh_oauth: backoff active ({_REFRESH_MIN_INTERVAL_S:.0f}s) "
                    f"user={user_id}, provider={provider} —— 跳过本轮刷新")
        return _read_access_token_from_identity(user_id, provider) or None

    # 凭据（Issue #3054 P1-2）：走与换 code 完全相同的取凭据入口，client_id+secret
    # 同源成对取——原实现直接读 meta，凭据迁表后就是"登录用新凭据、刷新用旧/空凭据"，
    # 刷新必然失败 → 身份行被打 status=4。
    from src.domains.mgmt.auth.providers import (
        CredentialDecryptError,
        get_client_credentials,
        get_provider,
    )

    try:
        client_id, client_secret = get_client_credentials(provider)
    except CredentialDecryptError as e:
        # 密文不可解是密钥事故，不是"凭据过期"：不刷新、不打 status=4、绝不回落 meta。
        log.error(f"refresh_oauth: credential_decrypt_failed for user={user_id}, "
                  f"provider={provider}: {e}")
        return None
    if not client_id or not client_secret:
        log.warning(f"refresh_oauth: OAuth client_id/secret not configured for {provider}")
        return None

    # 2. 获取 token_url（从 provider 配置）
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
            mark_identity_reauth_required(user_id, provider, "invalid_grant")
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
    _REFRESH_LAST[(user_id, provider)] = time.monotonic()

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
    # user_repos 归 mgmt：非 owner 进程（runtime/insight）不建空壳副本，读路径走 owner 域原语。
    from src.kernel import table_ownership as _own_tbl
    if _own_tbl.should_skip("user_repos"):
        log.info("[DDL_SKIP] `user_repos`: owner=mgmt，非本域进程不建表")
        return
    with db_lock:
        db.execute(_USER_REPOS_DDL)
        _mg_db.commit()

def save_user_repos(user_id: int, repos: list[dict], providers: list[str] | None = None):
    """批量写入用户仓库缓存（repos 元素含 full_name/owner/name/...，full_name 无前缀）

    is_join 从用户自己的主动托管记录（mark_user_repo_join 写入的 is_join=1 AND is_admin=1）继承，
    不再查询全局 managed_repos，防止 A 托管的仓库污染 B 的下拉框。

    providers：本次**真正刷新成功**的平台集合。给定时只替换这些平台的行，其余平台的旧缓存
    原样保留——否则某平台拉取失败（401/网络）时，先删后写会把它的整段缓存抹掉
    （2026-11-14 实锤：gitee 7 行刷新后 gitcode 58 + github 16 行一起消失）。None = 全量覆盖。
    """
    from src.kernel.repo_key import to_repo_key
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT repo_key FROM user_repos WHERE user_id=? AND is_join=1 AND is_admin=1", (user_id,))
        joined_keys = {row[0] for row in c.fetchall()}
        if providers is None:
            c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
        else:
            for _p in providers:
                # P2-6：不能用 LIKE 拼前缀——`_` 是 LIKE 的单字符通配符，provider 名允许
                # 下划线（如 gitee_enterprise），会误删 giteeXenterprise: 同形行。
                # substr 前缀相等在 SQLite/MySQL 语义一致，且天然无通配符。
                _prefix = f"{_p}:"
                c.execute(
                    "DELETE FROM user_repos WHERE user_id=? AND substr(repo_key, 1, ?) = ?",
                    (user_id, len(_prefix), _prefix))
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
        _mg_db.commit()

def get_user_repos(user_id: int, org_id: str = "") -> list[dict]:
    """读取用户仓库缓存（对外返回 full_name 无前缀）。

    org_id 参数保留兼容，但实际过滤在 _filter_repos 中完成
    （需要同时返回未托管的仓库供用户托管到当前组织）。
    """
    from src.kernel.repo_key import from_repo_key
    with db_lock:
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
        c.execute("UPDATE user_repos SET is_join=?, is_admin=? WHERE user_id=? AND repo_key=?",
                  (int(is_join), int(is_admin), user_id, repo_key))
        if c.rowcount == 0:
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                (user_id, repo_key, owner, repo, int(is_admin), int(is_join)))
        _mg_db.commit()

def get_user_repos_synced_at(user_id: int) -> str:
    """返回用户仓库缓存的最近同步时间（synced_at），无缓存返回空串"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT MAX(synced_at) FROM user_repos WHERE user_id=?", (user_id,))
        row = c.fetchone()
        return row[0] or "" if row else ""


def invalidate_user_repos(user_id: "str | int"):
    """把用户仓库缓存标记为过期（保留缓存行与 is_join 状态）。

    写操作（托管 / 模板建仓 / 解除托管）与关系变更钩子（D109）调用，令下一次
    /repos/member 直接回源 VCS 刷新，避免新增或移除的仓库被 30 分钟 TTL 挡在缓存之外。
    user_id 接受 str（UUIDv7 主键）与存量 int 字符串化值——不要在这里做 int() 强转。
    synced_at 置 NULL 后 get_user_repos_synced_at 返回空串，命中 _is_expired。
    """
    with db_lock:
        c = _mg_db.cursor()
        c.execute("UPDATE user_repos SET synced_at=NULL WHERE user_id=?", (user_id,))
        _mg_db.commit()


def clear_user_repos(user_id: int):
    """清除用户仓库缓存"""
    with db_lock:
        db.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
        _mg_db.commit()

def has_user_repos(user_id: int) -> bool:
    """检查是否有缓存"""
    with db_lock:
        c = _mg_db.cursor()
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
        try:
            # agent_runtimes 归运行域：整表读 + 逐行归一都走任意拓扑具名查询/写查询
            for rid, last_seen, updated in _rt.read_all_any("runtime_utc_caliber_rows"):
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
                    _rt.exec_any("runtime_set_timestamps", (new_last, new_updated, rid))
                    changed += 1
        except Exception as e:
            log.warning(f"migrate_agent_runtimes_utc_caliber failed: {e}")
    return changed


def _columns_exist(c, table: str, cols: tuple) -> bool:
    """Issue #646：表是否已具备指定列（未迁移旧库降级判定）。

    Issue #3097：实现迁至 runtime 域（`runtime.engine.refs_reconcile.columns_exist`）
    ——被探测的表（agent_runtimes / agent_type_runtime_pool / 会话表）归 runtime 库，
    mgmt 域不再持有这些表的 SQL；本别名只为既有调用方（engine_select 等）过渡。
    """
    from src.domains.runtime.public import columns_exist
    return columns_exist(c, table, cols)


# ── Issue #646 双读 helper（三库独立拓扑：agent_runtimes 归**运行域**） ──
# 历史实现在这里直查进程主库 `db`：单库/sqlite 拓扑下就是同一个库，能跑；
# 三库拆分（GRAPE_DOMAIN_LIB=1）后 mgmt 进程主库没有 agent_runtimes →
# 1146（Table '<mgmt 库>.agent_runtimes' doesn't exist）被 except 吞掉 → 一律
# 返回 None。外部表现：场景保存/引擎绑定按 uid 提交必报「引擎 uid 无效」，
# scene_precheck / 任务快照的 uid 轨道全线静默丢失。
# 修法：只走运行域公开读原语（`_any_cursor` 自带自有 DSN / 回落进程库两种拓扑），
# 不在 mgmt 模块里拼跨域 SQL；取数失败按「调用点+错误」告警一次，避免把后端
# 不可达继续伪装成「用户抄错 uid」（同文件其余取数路径均 log.warning）。

_UID_LOOKUP_WARNED: set = set()


def _warn_uid_lookup_once(where: str, exc: BaseException) -> None:
    """uid 双读取数失败去重告警：同一 (调用点, 错误摘要) 只记一次 WARNING。

    这些 helper 的 except 是刻意降级（不阻断保存/建任务），但完全静默会把
    「运行域库不可达/缺表」与「uid 真不存在」抹成同一个 None，故障只能从
    400「引擎 uid 无效」反推。去重后既不刷屏也保留可诊断线索。
    """
    key = (where, f"{type(exc).__name__}: {str(exc)[:120]}")
    if key in _UID_LOOKUP_WARNED:
        return
    if len(_UID_LOOKUP_WARNED) > 64:
        _UID_LOOKUP_WARNED.clear()
    _UID_LOOKUP_WARNED.add(key)
    log.warning(f"engine uid lookup failed ({where}): {exc}")


def get_engine_uid_by_runtime_id(runtime_id: int) -> str | None:
    """Issue #646 双读：自增 id → engine_uid（无/未迁移返回 None）。"""
    if not runtime_id:
        return None
    try:
        row = _rt.read_one_any("agent_runtime_engine_uid_by_id", (int(runtime_id),))
    except Exception as e:
        _warn_uid_lookup_once("get_engine_uid_by_runtime_id", e)
        return None
    uid = row[0] if row else None
    return str(uid) if uid else None


def get_runtime_id_by_engine_uid(engine_uid: str) -> int | None:
    """Issue #646 双读：engine_uid → 自增 id（uid 无效/未迁移返回 None）。"""
    if not engine_uid:
        return None
    _u = str(engine_uid).strip().lower()
    if not _u:
        return None
    try:
        _ids = _rt.runtime_ids_by_engine_uids([_u])
    except Exception as e:
        _warn_uid_lookup_once("get_runtime_id_by_engine_uid", e)
        return None
    rid = int(_ids[0]) if _ids else 0
    return rid or None


def resolve_engine_refs(refs) -> tuple[list, list]:
    """Issue #646 双读解析：引擎引用列表（uid 字符串或数字 id 混合）→ (ids, uids)。

    - 数字 id：按 id 查 uid（旧路径兜底，未迁移库 uid=None）
    - uid 字符串：按 uid 查 id（服务端校验；无效 uid 对应项为 None）
    返回与输入等长的两个列表（失败项 None，调用方决定 fail-fast 或跳过）。
    """
    from src.kernel.engine_uid import is_uid_str
    _refs = list(refs or [])
    # uid 形态引用批量解析（运行域公开读原语：三库独立拓扑下不跨域直查）
    _uid_refs = [str(r).strip().lower() for r in _refs
                 if isinstance(r, str) and is_uid_str(str(r).strip())]
    _uid_to_id: dict = {}
    if _uid_refs:
        try:
            for _u, _rid in zip(_uid_refs, _rt.runtime_ids_by_engine_uids(_uid_refs)):
                if _rid:
                    _uid_to_id[_u] = int(_rid)
        except Exception as e:
            _warn_uid_lookup_once("resolve_engine_refs", e)
            _uid_to_id = {}
    ids: list = []
    uids: list = []
    for ref in _refs:
        if isinstance(ref, str) and is_uid_str(ref.strip()):
            _u = ref.strip().lower()
            ids.append(_uid_to_id.get(_u))
            uids.append(_u)
        elif isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            rid = int(ref)
            ids.append(rid)
            uids.append(get_engine_uid_by_runtime_id(rid))
        else:
            ids.append(None)
            uids.append(None)
    return ids, uids


def engine_ref_snapshot(runtime_id: int, pool_engine_ids: str = "") -> tuple:
    """Issue #646：任务落库时的引擎引用快照（写路径双写）。

    返回 (engine_uid, engine_name_snapshot, pool_engine_uids_json)：
    - 引擎已删除/未迁移 → (None, '', '')（不阻断任务创建，快照语义）；
    - pool_engine_ids JSON 数字数组 → uid 数组镜像（孤儿项剔除）。
    """
    import json as _json
    engine_uid = None
    snapshot = ""
    try:
        row = _rt.read_one_any("agent_runtime_snapshot_by_id", (int(runtime_id),))
        if row:
            engine_uid = row[0] or None
            snapshot = row[1] or row[2] or ""
    except Exception as e:
        _warn_uid_lookup_once("engine_ref_snapshot", e)
        engine_uid, snapshot = None, ""
    pool_uid_json = ""
    if pool_engine_ids:
        try:
            ids = _json.loads(pool_engine_ids)
            _ids, _uids = resolve_engine_refs(ids)
            pool_uid_json = _json.dumps([u for u in _uids if u])
        except Exception:
            pool_uid_json = ""
    return engine_uid, snapshot, pool_uid_json


def ensure_credential_uid(credential_id: int, c=None) -> str:
    """Issue #646：确保凭据行有 credential_uid（UUIDv4，一次性终身不变）。

    注册/心跳链路调用；未迁移库（无列）返回 ''。可在已有 cursor/锁内复用 c。
    实现与 SQL 全在 owner 域（runtime_credentials 归 runtime）：
    `src.domains.runtime.public.ensure_credential_uid`。本壳只为既有 mgmt 调用面
    与测试连接注入缝保留，不再持有判定逻辑。
    """
    from src.domains.runtime import public as _rt_pub
    return _rt_pub.ensure_credential_uid(credential_id, c)


def _session_uid_mirror_sql(tbl: str) -> str:
    """Issue #868：会话表 engine_uid 镜像对账 UPDATE（跨方言 NULL-safe）。

    Issue #3097：SQL 生成与实现随对账例程一并迁至 runtime 域
    （`runtime.engine.refs_reconcile.session_uid_mirror_sql`）。
    """
    from src.domains.runtime.public import session_uid_mirror_sql
    return session_uid_mirror_sql(tbl)


def _rebind_runtime_references(c, dup_ids: list, keep_id: int, reason: str = "") -> dict:
    """Issue #646 / #3097：引擎行删除前的引用改绑/显式清理（薄接线，无判定逻辑）。

    判定逻辑与 SQL 全部在 runtime 域
    `src/domains/runtime/engine/refs_reconcile.py`；本例程碰的 6 张表
    （agent_runtimes / agent_tasks / agent_type_runtime_pool / engine_capabilities /
    devops_agent_session / chat_agent_session）按 `configs/table_ownership.json`
    **全归 runtime 库**。`reconcile_for_caller` 按进程域选通道：mgmt 进程（删用户/
    合并账号）经 runtime public 原语落到 runtime 域连接；runtime 进程内复用调用方
    游标（同域事务）。
    """
    from src.domains.runtime.public import reconcile_for_caller
    return reconcile_for_caller(c, list(dup_ids or []), keep_id, reason=reason)


def rebind_runtime_references(c, dup_ids: list, keep_id: int, reason: str = "") -> dict:
    """Issue #646：去重路径公开入口——待删行引用改绑到保留行 + 对账报告。"""
    return _rebind_runtime_references(c, list(dup_ids or []), keep_id, reason=reason)


def explicit_cleanup_runtime_references(c, runtime_ids: list, reason: str = "") -> dict:
    """Issue #646：删除路径统一公开入口（吊销 / devstage 删环境 / 刷新过期行）。

    显式清理待删引擎行的池/能力/会话/绑定/场景引用并输出对账报告，caller 随后再
    DELETE agent_runtimes（MySQL RESTRICT FK 兜底防漏，不静默级联蒸发）。
    """
    return _rebind_runtime_references(c, list(runtime_ids or []), 0, reason=reason)


# ── Issue #868 交叉预警修正：engine_uid 派生源锚定后的存量回填/改绑 ──
# 表清单与 SQL 随实现一并归 runtime 域（`runtime.public._ENGINE_UID_REF_TABLES`），
# mgmt 侧不再保留第二份真源。


def rebase_engine_uid_references(c, old_uid: str, new_uid: str) -> dict:
    """Issue #868：单个 engine_uid 变更的引用轨道整体改绑（old_uid → new_uid）。

    实现（引擎行 + 池/能力/任务/消息/会话 uid 轨道，碰的表全归 runtime）已在
    owner 域：`src.domains.runtime.public.rebase_engine_uid_references`。本壳保留
    mgmt 侧既有调用面（db_setup 启动期回填 / 测试连接注入缝），不再持有 SQL。
    """
    from src.domains.runtime import public as _rt_public
    return _rt_public.rebase_engine_uid_references(c, old_uid, new_uid)


def rebase_engine_uids_by_anchor(c, anchor_rows: list) -> dict:
    """Issue #868（交叉预警处置项③ + 种子分量 id→uid 升格）：按不可变身份锚
    批量重算存量 engine_uid 并改绑引用。

    i5（feat/devbox-sandbox-integration5，四段名改名跟随）合流迁移回填入口：
    对每个 ``{"runtime_id": int, "engine_type": str, "provider_id": str,
    "template_id": str, "provider_uid": str, "template_uid": str}``——runtime 行
    按新锚重派生 uid（uid 分量成对在场 → uid 公式
    ``credential_uid|engine_type|scope|owner_id|host_name|provider_uid|template_uid``；
    仅 id 锚 → id 公式，T13 行为不变），**旧行 uid 从行内读取（旧 uid→新 uid
    映射式回填，禁止按 runtime_id 重派 JOIN——id/uid 两公式产物不可区分，重派
    会静默漏回填）**，不同则连同全部引用轨道改绑（rebase_engine_uid_references），
    保证改名/升格场景 uid 变更一次性收敛、引用不留孤儿。
    幂等可重跑：uid 已是目标值时为 no-op。普通引擎行不在清单内则零影响。
    """
    from src.kernel.engine_uid import derive_engine_uid
    report = {"engines_rebased": 0, "skipped": 0, "pool_rows": 0, "capability_rows": 0,
              "task_rows": 0, "task_message_rows": 0,
              "devops_sessions": 0, "chat_sessions": 0}
    cred_uid_by_id = {}
    try:
        # G-02/F1：同上——public 可调用原语 + 调用方游标（同事务读，fixture 注入缝保留）
        from src.domains.runtime import public as _rt_pub
        cred_uid_by_id = {int(r[0]): (r[1] or "") for r in
                          _rt_pub.credential_id_uid_pairs(c)}
    except Exception:
        cred_uid_by_id = {}
    for row in anchor_rows or []:
        try:
            rid = int(row.get("runtime_id") or 0)
            if not rid:
                report["skipped"] += 1
                continue
            from src.domains.runtime import public as _rt_public2
            erow = _rt_public2.runtime_rebase_row(rid)
            if not erow:
                report["skipped"] += 1
                continue
            cred_id, name, scope, owner_id, host_name, old_uid = erow
            cred_uid = cred_uid_by_id.get(cred_id, "") if cred_id else ""
            new_uid = derive_engine_uid(
                cred_uid, name or "", scope or "", owner_id or "", host_name or "",
                engine_type=str(row.get("engine_type") or ""),
                provider_id=str(row.get("provider_id") or ""),
                template_id=str(row.get("template_id") or ""),
                provider_uid=str(row.get("provider_uid") or ""),
                template_uid=str(row.get("template_uid") or ""))
            sub = rebase_engine_uid_references(c, old_uid, new_uid)
            for k in report:
                if k in sub:
                    report[k] += sub[k]
        except Exception as _e:
            report["skipped"] += 1
            log.warning(f"rebase_engine_uids_by_anchor runtime_id={row.get('runtime_id')}: {_e}")
    log.info("Issue #868 i5合流锚定回填报告: " + json.dumps(report, ensure_ascii=False))
    return report


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
    # P3-5f-2：agent_runtimes 在 runtime 域。整段「探测即快照」（逐引擎 upsert + prune + dedupe）
    # 必须一次事务完成，拆成多条意图会让旧 payload 重放误删新行 → 收成一个组合原语。
    from src.kernel.domain_read import runtime_lib as _rt_lib_sar
    from src.kernel import cross_write as _xw_sar
    _rt_sar = _rt_lib_sar(foreign_only=True)
    if _rt_sar is not None:
        _rt_sar_ok = _rt_sar
        _sar_out: dict = {}

        def _apply_sar() -> None:
            _sar_out.update(_rt_sar_ok.upsert_local_runtimes(
                agents, scope, owner_id, host_name, _now_iso))

        if _xw_sar.submit_if_lib(
                _rt_sar_ok, "runtime", "agent_runtimes_upsert_local",
                f"agent-runtimes-local:{scope}:{owner_id}:{host_name}:{_now_iso}",
                {"agents": agents, "scope": scope, "owner_id": owner_id,
                 "host_name": host_name, "now_iso": _now_iso}, _apply_sar, log):
            log.info(f"save_agent_runtimes via lib: {_sar_out}")
            return
    # 任意拓扑：整段「探测即快照」（逐引擎 upsert + prune + dedupe）是运行域的组合原语，
    # 必须一次事务完成（拆成多条意图会让旧 payload 重放误删新行）；单库拓扑回落进程库。
    _sar_res = _rt.upsert_local_runtimes(agents, scope, owner_id, host_name, _now_iso)
    log.info(f"save_agent_runtimes: {_sar_res}")
    return _sar_res


def get_backend_agents(scope: str = "global", owner_id: str = "") -> list[dict]:
    """[兼容别名] 见 get_agent_runtimes"""
    return get_agent_runtimes(scope, owner_id)

def get_agent_runtimes(scope: str | None = "global", owner_id: str = "") -> list[dict]:
    """读取 agent runtimes 缓存（含 status/last_seen_at，供健康态派生）。

    scope=None 时返回全量（含 user/repo 个人行，供视图层自行按可见性过滤，
    如 settings.get_agent_runtimes 的 Engines 列表）。
    Issue #646：附带 engine_uid/credential_uid（API 出口双带 uid+id，uid 供绑定/复制）。
    """
    import os as _os
    from src.kernel.domain_read import switch_on as _dr_switch  # noqa: PLC0415
    if _dr_switch("GRAPE_RUNTIMES_VIA_LIB", "runtime"):
        try:
            if _rt.available():
                return _rows_to_runtimes(_rt.list_runtimes_rows(scope, owner_id))
        except Exception as _exc:  # noqa: BLE001
            import logging as _log
            _log.getLogger(__name__).warning(f"runtimes via lib failed, fallback: {_exc}")
    # agent_runtimes 归运行域：两条具名查询（列序与原 SQL 完全一致），任意拓扑
    if scope is None:
        return _rows_to_runtimes(_rt.read_all_any("runtimes_all"))
    return _rows_to_runtimes(_rt.read_all_any("runtimes_by_scope", (scope, owner_id)))

def _rows_to_runtimes(rows) -> list[dict]:
    """将 DB 行列表转为 runtime dict 列表（供 get_agent_runtimes / paged 复用）。"""
    # AIShell 显示名（2026-10-13）：出口统一转换 name/display_name（内部键不变）
    from src.domains.runtime.public import to_display as _td
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
         "probe_spawn_fail_count": int(r[25] or 0) if len(r) > 25 else 0,
         "engine_uid": r[26] if len(r) > 26 else "" or "", "credential_uid": r[27] if len(r) > 27 else "" or ""}
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
        c = _mg_db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_b84e88_f
        _ru_lib_b84e88 = _ru_lib_b84e88_f(foreign_only=True)
        if _ru_lib_b84e88 is not None:
            row = _ru_lib_b84e88.read_one("agent_runtimes_b84e88", (name.strip().lower(),))
        else:
            # agent_runtimes 归运行域：具名查询（任意拓扑）
            row = _rt.read_one_any("runtime_id_local_direct", (name.strip().lower(),))
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
        c = _mg_db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_145cfe_f
        _ru_lib_145cfe = _ru_lib_145cfe_f(foreign_only=True)
        if _ru_lib_145cfe is not None:
            row = _ru_lib_145cfe.read_one("agent_runtimes_145cfe", (name, scope, owner_id))
        else:
            # agent_runtimes 归运行域：具名查询（含凭据吊销过滤与 online 优先排序，任意拓扑）
            row = _rt.read_one_any("runtime_id_by_name_scope_owner", (name, scope, owner_id))
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
        from src.domains.runtime.public import discover_backends
        if any((b.get("name") or "").strip().lower() == name for b in discover_backends()):
            return True
    except Exception:
        pass
    try:
        from src.domains.runtime.public import backend_registered
        if backend_registered(name):
            return True
    except Exception:
        pass
    return False

def _local_engine_info(name: str) -> tuple[bool, str]:
    """按引擎类型名返回本机是否已安装 + cli_path（discover_backends 真实探测，见 engine-selection §7.3）。"""
    try:
        from src.domains.runtime.public import discover_backends
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
        c = _mg_db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_808985_f
        _ru_lib_808985 = _ru_lib_808985_f(foreign_only=True)
        if _ru_lib_808985 is not None:
            row = _ru_lib_808985.read_one("agent_runtimes_808985", (name, scope))
        else:
            # agent_runtimes 归运行域：共享（owner_id=''）具名查询，任意拓扑
            row = _rt.read_one_any("runtime_id_by_name_scope_shared", (name, scope))
        if row:
            return row[0]
        if not is_known_engine_name(name):
            log.warning(f"ensure_default_runtime: 拒绝为未知引擎 '{name}' 创建占位行")
            return None
        installed, cli_path = _local_engine_info(name)
        healthy, version = 0, ""
        if installed and cli_path:
            try:
                from src.domains.runtime.public import probe_local_engines
                probed = probe_local_engines([{"name": name, "cli_path": cli_path, "installed": True}])
                if probed:
                    healthy = 1 if probed[0].get("healthy") else 0
                    version = probed[0].get("version", "") or ""
            except Exception:
                pass
        # P3-5f-2：agent_runtimes 在 runtime 域（组合原语自带 online 优先 + credential JOIN）
        from src.kernel.domain_read import runtime_lib as _rt_lib_edr
        from src.kernel import cross_write as _xw_edr
        _rt_edr = _rt_lib_edr(foreign_only=True)
        if _rt_edr is not None:
            _rt_edr_ok = _rt_edr
            _edr_out: dict = {}

            def _apply_edr() -> None:
                _edr_out.update(_rt_edr_ok.ensure_default_runtime(
                    name, name.capitalize(), 1 if installed else 0, cli_path, version,
                    healthy, name, scope))

            if _xw_edr.submit_if_lib(
                    _rt_edr_ok, "runtime", "agent_runtime_ensure_default",
                    f"agent-runtime-default:{name}:{scope}",
                    {"name": name, "display_name": name.capitalize(),
                     "installed": 1 if installed else 0, "cli_path": cli_path,
                     "version": version, "healthy": healthy, "pip_package": name,
                     "scope": scope}, _apply_edr, log):
                return int(_edr_out.get("id") or 0) or None
        # 任意拓扑：组合原语（SELECT online 优先 + 无行则建行），单库回落进程库
        _edr_res = _rt.ensure_default_runtime(name, name.capitalize(), 1 if installed else 0,
                                              cli_path, version, healthy, name, scope)
        return int(_edr_res.get("id") or 0) or None

def get_backend_agents_age() -> float | None:
    """[兼容别名] 见 get_agent_runtimes_age"""
    return get_agent_runtimes_age()

def get_agent_runtimes_age() -> float | None:
    """返回缓存数据的 age（秒），无缓存返回 None"""
    with db_lock:
        c = _mg_db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_e6e235_f
        _ru_lib_e6e235 = _ru_lib_e6e235_f(foreign_only=True)
        if _ru_lib_e6e235 is not None:
            row = _ru_lib_e6e235.read_one("agent_runtimes_e6e235", ())
        else:
            # agent_runtimes 归运行域：具名聚合查询（任意拓扑）
            row = _rt.read_one_any("runtime_age")
        if not row or not row[0]:
            return None
    import datetime
    updated = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return (datetime.datetime.now() - updated).total_seconds()

def list_scene_categories() -> list[dict]:
    """返回启用的场景分类列表，按 order_num 排序"""
    # P3.5：scene_category 归运行域；lib 可用时走 owner 域读
    from src.kernel.domain_read import owner_lib as _owner_lib  # noqa: PLC0415
    _rt_cats = _owner_lib("runtime", foreign_only=True)
    if _rt_cats is not None:
        try:
            return [{k: v for k, v in c.items() if k != "is_active"}
                    for c in _rt_cats.list_scene_categories(True)]
        except Exception as _exc:  # noqa: BLE001
            log.warning(f"runtime lib list_scene_categories failed, fallback: {_exc}")
    # 任意拓扑：具名查询在 owner 域一份（列序与原 SQL 一致）
    return [{"id": r[0], "name": r[1], "slug": r[2] or "", "icon": r[3] or "", "order_num": r[4]}
            for r in _rt.read_all_any("scene_categories_active")]

def get_scene_category(category_id: int) -> dict | None:
    """按 id 查场景分类"""
    # P3.5：scene_category 归运行域；lib 可用时走 owner 域读
    from src.kernel import domain_read
    _rt_cat = domain_read.owner_lib("runtime", foreign_only=True)
    if _rt_cat is not None:
        try:
            return _rt_cat.get_scene_category(int(category_id))
        except Exception as _exc:  # noqa: BLE001
            log.warning(f"runtime lib get_scene_category failed, fallback: {_exc}")
    # scene_category 归运行域：具名查询（任意拓扑，无需本域游标）
    row = _rt.read_one_any("scene_category_row", (category_id,))
    if not row:
        return None
    return {"id": row[0], "name": row[1], "slug": row[2] or "", "icon": row[3] or ""}

def list_skill_categories() -> list[dict]:
    """返回启用的技能分类列表，按 order_num 排序"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            "SELECT id, name, slug, icon, order_num FROM skill_category "
            "WHERE is_active=1 ORDER BY order_num"
        )
        return [{"id": r[0], "name": r[1], "slug": r[2] or "", "icon": r[3] or "", "order_num": r[4]}
                for r in c.fetchall()]

def get_skill_category(category_id: int) -> dict | None:
    """按 id 查技能分类"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT id, name, slug, icon FROM skill_category WHERE id=?", (category_id,))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "slug": row[2] or "", "icon": row[3] or ""}

def list_category_dict(scope: str = "") -> list[dict]:
    """返回启用的统一分类字典（Issue #2761）。

    scope: scene / skill / 空（全部）。applies_to='both' 的行对两种 scope 都可见。
    """
    scope = (scope or "").strip().lower()
    with db_lock:
        c = _mg_db.cursor()
        sql = ("SELECT id, parent_id, level, name, slug, icon, order_num, applies_to "
               "FROM grape_t_category_dict WHERE is_active=1")
        params: list = []
        if scope in ("scene", "skill"):
            sql += " AND applies_to IN ('both', ?)"
            params.append(scope)
        sql += " ORDER BY level, parent_id, order_num, id"
        c.execute(sql, params)
        rows = c.fetchall()
    return [{"id": r[0], "parent_id": r[1] or 0, "level": r[2] or 1,
             "name": r[3], "slug": r[4] or "", "icon": r[5] or "",
             "order_num": r[6] or 0, "applies_to": r[7] or "both"} for r in rows]


def list_category_tree(scope: str = "") -> list[dict]:
    """两级分类树：[{...一级, children:[{...二级}]}]（Issue #2761）。"""
    rows = list_category_dict(scope)
    l1 = [dict(r, children=[]) for r in rows if int(r.get("level") or 1) == 1]
    by_id = {r["id"]: r for r in l1}
    for r in rows:
        if int(r.get("level") or 1) == 2:
            parent = by_id.get(int(r.get("parent_id") or 0))
            if parent is not None:
                parent["children"].append(dict(r))
    return l1


def category_path_from_map(cat_map: dict, l1_id: int = 0, l2_id: int = 0) -> dict:
    """用预取字典构造分类路径 {l1, l2}（批量列表用，避免逐行查库）。"""
    l1 = cat_map.get(int(l1_id or 0))
    l2 = cat_map.get(int(l2_id or 0))
    if l2 and not l1:
        l1 = cat_map.get(int(l2.get("parent_id") or 0))
    return {
        "l1": ({k: l1[k] for k in ("id", "name", "slug")} if l1 else None),
        "l2": ({k: l2[k] for k in ("id", "name", "slug")} if l2 else None),
    }


def get_category_dict_entry(category_id: int) -> dict | None:
    """按 id 取分类字典单行；不存在/停用返回 None。"""
    if not category_id or int(category_id) <= 0:
        return None
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            "SELECT id, parent_id, level, name, slug, icon, order_num, applies_to "
            "FROM grape_t_category_dict WHERE id=? AND is_active=1", (int(category_id),))
        row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "parent_id": row[1] or 0, "level": row[2] or 1,
            "name": row[3], "slug": row[4] or "", "icon": row[5] or "",
            "order_num": row[6] or 0, "applies_to": row[7] or "both"}


def category_path(l1_id: int = 0, l2_id: int = 0) -> dict:
    """构造分类路径 {l1, l2}（缺失侧为 None），用于场景/技能读接口。"""
    l1 = get_category_dict_entry(l1_id)
    l2 = get_category_dict_entry(l2_id)
    if l2 and not l1:
        l1 = get_category_dict_entry(l2.get("parent_id") or 0)
    return {
        "l1": ({k: l1[k] for k in ("id", "name", "slug")} if l1 else None),
        "l2": ({k: l2[k] for k in ("id", "name", "slug")} if l2 else None),
    }


#: (parent_slug, level, name, slug, icon, order_num)；parent_slug 为空表示一级域
#: 种子数据与显式 id 复算规则的单一真源已上提 kernel（Issue #3168 QA Standards#2：
#: runtime 域迁移离线复算不再直接 import mgmt 模块；先例 KEEP_L1_IDS）。
#: 此处保留同名再导出（db_setup 重映射 / 既有测试引用兼容），语义不变。
from src.kernel.category import CATEGORY_DICT_SEED as _CATEGORY_DICT_SEED  # noqa: F401
from src.kernel.category import category_dict_seed_id  # noqa: F401


def seed_category_dict() -> None:
    """幂等写入分类字典种子（Issue #2761；id 显式分配，一级先落库再写二级）。"""
    with db_lock:
        c = _mg_db.cursor()
        id_by_slug: dict[str, int] = {}
        for idx, (parent_slug, level, name, slug, icon, order_num) in enumerate(_CATEGORY_DICT_SEED):
            parent_id = int(id_by_slug.get(parent_slug, 0)) if parent_slug else 0
            row_id = category_dict_seed_id(_CATEGORY_DICT_SEED, idx)
            id_by_slug[slug] = row_id
            try:
                c.execute(
                    "INSERT OR IGNORE INTO grape_t_category_dict "
                    "(id, parent_id, level, name, slug, icon, order_num, applies_to, is_active) "
                    "VALUES (?,?,?,?,?,?,?,'both',1)",
                    (row_id, parent_id, level, name, slug, icon, order_num))
            except Exception:  # noqa: BLE001 - MySQL 方言兜底（重复行忽略）
                c.execute(
                    "INSERT IGNORE INTO grape_t_category_dict "
                    "(id, parent_id, level, name, slug, icon, order_num, applies_to, is_active) "
                    "VALUES (?,?,?,?,?,?,?,'both',1)",
                    (row_id, parent_id, level, name, slug, icon, order_num))
        _mg_db.commit()





def backfill_scene_market_legacy_tags() -> int:
    """把 scene_market 旧逗号串标签就地回填为 JSON [{name, source:"manual"}]。

    Issue #2761 迁移专用（幂等：已是规范 JSON 的行跳过）。返回迁移行数。
    """
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT id, tags FROM scene_market WHERE tags IS NOT NULL AND tags != ''")
        rows = c.fetchall()
        migrated = 0
        for row in rows:
            raw = row[1]
            if isinstance(raw, str) and raw.strip().startswith("["):
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, list) and all(
                        isinstance(x, dict) and str(x.get("name") or "").strip()
                        for x in data):
                    continue
            parsed = parse_tags(raw)
            if not parsed:
                continue
            c.execute("UPDATE scene_market SET tags=? WHERE id=?",
                      (encode_tags(parsed), int(row[0])))
            migrated += 1
        _mg_db.commit()
        return migrated


def list_scene_market(q: str = "", category_id: int = 0, sort: str = "latest",
                      featured_only: int = 0, limit: int = 0,
                      page: int = 1, page_size: int = 20,
                      scope: str = "", repo_key: str = "",
                      user_id: int = 0, cat_l1_id: int = 0,
                      cat_l2_id: int = 0) -> tuple[list[dict], int]:
    """查询场景市场列表（is_active=1），支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页 + scope 过滤。

    sort: latest（按 updated_at 降序）/ hot（按 like_count 降序）。
    featured_only: 1=只查精选场景，0=全部。
    limit: >0 限制返回条数（精选场景用，不分页）。
    page/page_size: 分页参数（limit=0 时生效），page 从 1 开始。
    scope: official / repo / org；空=普通市场（不返回 org 系统模板）。
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
    if cat_l1_id and cat_l1_id > 0:
        where.append("cat_l1_id=?")
        params.append(cat_l1_id)
    if cat_l2_id and cat_l2_id > 0:
        where.append("cat_l2_id=?")
        params.append(cat_l2_id)
    if featured_only:
        where.append("is_featured=1")
    if scope:
        where.append("scope=?")
        params.append(scope)
        if scope == "repo" and repo_key:
            where.append("repo_key=?")
            params.append(repo_key)
    else:
        # Organization templates are provisioned server-side and are not
        # ordinary market items. They remain queryable with scope='org'.
        where.append("scope!='org'")
    where_sql = " AND ".join(where)
    order_by = "updated_at DESC" if sort != "hot" else "like_count DESC"

    # 精选模式：优先按 admin 管理台设置的排序返回
    featured_ordered_ids = []
    if limit and limit > 0 and featured_only:
        from src.kernel.featured_order import get_featured_order
        featured_ordered_ids = get_featured_order("scene")

    with db_lock:
        c = _mg_db.cursor()
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
            f"nodes_json, onboarding, created_at, scope, cat_l1_id, cat_l2_id, tags "
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
            "cat_l1_id": r[17] or 0, "cat_l2_id": r[18] or 0,
            "tags": parse_tags(r[19] if len(r) > 19 else "", limit=MAX_TAGS),
        })
    return result, total

def get_scene_market_by_id(market_id: int) -> dict | None:
    """按主键 id 查场景市场详情"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            "SELECT id, name, display_name, version, description, market_description, "
            "icon, obs_icon_key, category_id, is_featured, like_count, source, source_user_id, "
            "recommended_template, nodes_json, onboarding, is_active, created_at, updated_at, "
            "scope, repo_key, preset_custom_fields, cat_l1_id, cat_l2_id, tags "
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
        "cat_l1_id": row[22] or 0, "cat_l2_id": row[23] or 0,
        "tags": parse_tags(row[24] if len(row) > 24 else "", limit=MAX_TAGS),
    }

def create_scene_market(name: str, display_name: str = "", description: str = "",
                        scope: str = "repo", repo_key: str = "", source_user_id: int = 0,
                        nodes_json: str = "[]", icon: str = "", obs_icon_key: str = "",
                        category_id: int = 1, recommended_template: str = "",
                        onboarding: str = "repo-onboarding", created_by: str = "",
                        market_description: str = "", cat_l1_id: int = 0,
                        cat_l2_id: int = 0, tags: str = "") -> int:
    """创建用户自定义场景（写入 scene_market 表）。返回新记录 id。

    UNIQUE (name, scope, source_user_id, repo_key) 约束冲突时抛 IntegrityError。
    Issue #2761：新增 cat_l1_id / cat_l2_id / tags（tags 传 JSON 串，空串=无标签）。
    """
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            "INSERT INTO scene_market "
            "(name, display_name, version, description, market_description, icon, obs_icon_key, "
            "category_id, cat_l1_id, cat_l2_id, tags, is_featured, like_count, source, source_user_id, "
            "recommended_template, nodes_json, onboarding, author, content_hash, is_active, scope, repo_key, "
            "created_by, updated_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,'user',?,?,?,?, '','', 1, ?,?, ?,?, datetime('now'),datetime('now'))",
            (name, display_name or name, "1.0.0", description, market_description or description,
             icon, obs_icon_key, category_id, int(cat_l1_id or 0), int(cat_l2_id or 0),
             tags or "", source_user_id,
             recommended_template, nodes_json, onboarding,
             scope, repo_key, created_by, created_by),
        )
        _mg_db.commit()
        return c.lastrowid

def update_scene_market(market_id: int, name: str = "", display_name: str = "",
                        description: str = "", nodes_json: str = "",
                        icon: str = "", obs_icon_key: str = "",
                        category_id: int = 0, recommended_template: str = "",
                        updated_by: str = "", cat_l1_id: int = 0,
                        cat_l2_id: int = 0, tags: str | None = None,
                        clear_category: bool = False) -> bool:
    """更新用户自定义场景（source != 'official'）。返回是否更新成功。

    版本号自动管理：只有内容真正变更才 patch+1，无变更不涨。
    版本号仅代表编辑轮次，不是标准语义化版本。
    """
    with db_lock:
        c = _mg_db.cursor()
        # 读取旧数据做内容比对
        c.execute(
            "SELECT name, display_name, description, market_description, nodes_json, "
            "icon, obs_icon_key, category_id, recommended_template, version, "
            "cat_l1_id, cat_l2_id, tags "
            "FROM scene_market WHERE id=? AND source!='official'",
            [market_id],
        )
        row = c.fetchone()
        if not row:
            return False

        (old_name, old_display, old_desc, old_mkt_desc, old_nodes, old_icon,
         old_obs_key, old_cat, old_tpl, old_version, old_l1, old_l2,
         old_tags) = row

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
        if clear_category:
            # Issue #2761：显式清空分类（0 是「不修改」的哨兵值，故需要独立开关）
            if int(old_l1 or 0) != 0:
                sets.append("cat_l1_id=?"); params.append(0); changed = True
            if int(old_l2 or 0) != 0:
                sets.append("cat_l2_id=?"); params.append(0); changed = True
        elif cat_l1_id:
            new_l1 = int(cat_l1_id)
            if new_l1 != int(old_l1 or 0):
                sets.append("cat_l1_id=?"); params.append(new_l1); changed = True
            # 一级变更而二级未提供：重置旧二级，避免「新一级 + 旧二级」跨父级脏数据
            new_l2 = int(cat_l2_id or 0)
            if new_l2 != int(old_l2 or 0):
                sets.append("cat_l2_id=?"); params.append(new_l2); changed = True
        elif cat_l2_id and int(cat_l2_id) != int(old_l2 or 0):
            sets.append("cat_l2_id=?"); params.append(int(cat_l2_id)); changed = True
        if tags is not None and (tags or "") != (old_tags or ""):
            sets.append("tags=?"); params.append(tags or ""); changed = True
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
                f"UPDATE scene_market SET {', '.join(sets)} WHERE id=? AND source!='official'",
                params,
            )
            _mg_db.commit()
            return c.rowcount > 0
        except Exception as e:
            if "UNIQUE" in str(e).upper() or "DUPLICATE" in str(e).upper():
                raise ValueError(f"场景标识已存在")
            raise

def delete_scene_market(market_id: int) -> bool:
    """硬删除用户自定义场景（source != 'official'）。
    场景市场无回收站需求，删除即销毁，name 立即释放。
    级联清理 scene_skill_ref 和 scene_likes 关联记录。
    """
    with db_lock:
        c = _mg_db.cursor()
        c.execute("DELETE FROM scene_skill_ref WHERE scene_id=?", (market_id,))
        c.execute("DELETE FROM scene_likes WHERE scene_id=?", (market_id,))
        c.execute(
            "DELETE FROM scene_market WHERE id=? AND source!='official'",
            (market_id,),
        )
        _mg_db.commit()
        return c.rowcount > 0

def check_scene_name_exists(name: str, scope: str, repo_key: str = "",
                            source_user_id: int = 0, exclude_id: int = 0) -> bool:
    """检查场景标识是否重复。
    - 不能和 official 场景重名（所有 scope 都检查 official）
    - repo 场景：同仓库下 name 唯一，不同仓库可以同名
    """
    with db_lock:
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
        c.execute(
            "INSERT INTO uploaded_files "
            "(file_key, original_name, file_size, mime_type, file_type, scope, "
            "ref_table, ref_id, is_active, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?, 'temp', '', 0, 1, ?, datetime('now'),datetime('now'))",
            (file_key, original_name, file_size, mime_type, file_type, created_by),
        )
        _mg_db.commit()
        return c.lastrowid

def commit_uploaded_file(file_id: int, ref_table: str, ref_id: int) -> bool:
    """将临时文件标记为已提交（scope='committed'）。返回是否成功。"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            "UPDATE uploaded_files SET scope='committed', ref_table=?, ref_id=?, "
            "updated_at=datetime('now') WHERE id=? AND scope='temp'",
            (ref_table, ref_id, file_id),
        )
        _mg_db.commit()
        return c.rowcount > 0

def get_uploaded_file(file_id: int) -> dict | None:
    """按 id 查上传文件记录。"""
    with db_lock:
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        _mg_db.commit()
        return row[0] or ""

def list_temp_uploaded_files_before(hours: int = 24) -> list[dict]:
    """查超过指定小时数的临时文件（供定时清理用）。"""
    from src.kernel.mysql_compat import get_db_backend
    is_mysql = get_db_backend() == "mysql"
    time_expr = f"DATE_SUB(NOW(), INTERVAL {int(hours)} HOUR)" if is_mysql else f"datetime('now', '-{int(hours)} hours')"
    with db_lock:
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
        c.execute("SELECT file_key FROM uploaded_files WHERE id=?", (file_id,))
        row = c.fetchone()
        if not row:
            return ""
        c.execute("DELETE FROM uploaded_files WHERE id=?", (file_id,))
        _mg_db.commit()
        return row[0] or ""

def is_scene_liked(user_id: int, scene_id: int) -> bool:
    """检查用户是否已点赞某场景"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT 1 FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
        return c.fetchone() is not None

def toggle_scene_like(user_id: int, scene_id: int) -> tuple[bool, int]:
    """场景点赞 toggle：已赞→取消，未赞→点赞。
    返回 (liked, like_count)。在 db_lock 事务内原子执行。
    """
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT 1 FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
        if c.fetchone():
            c.execute("DELETE FROM scene_likes WHERE user_id=? AND scene_id=?", (user_id, scene_id))
            c.execute("UPDATE scene_market SET like_count=like_count-1 WHERE id=?", (scene_id,))
            _mg_db.commit()
            c.execute("SELECT like_count FROM scene_market WHERE id=?", (scene_id,))
            row = c.fetchone()
            return (False, row[0] if row else 0)
        else:
            c.execute("INSERT INTO scene_likes (user_id, scene_id) VALUES (?, ?)", (user_id, scene_id))
            c.execute("UPDATE scene_market SET like_count=like_count+1 WHERE id=?", (scene_id,))
            _mg_db.commit()
            c.execute("SELECT like_count FROM scene_market WHERE id=?", (scene_id,))
            row = c.fetchone()
            return (True, row[0] if row else 0)

def batch_scene_liked(user_id: int, scene_ids: list[int]) -> dict[int, bool]:
    """批量查询用户对多个场景的点赞状态，返回 {scene_id: bool}"""
    if not scene_ids or not user_id:
        return {}
    placeholders = ",".join(["?"] * len(scene_ids))
    with db_lock:
        c = _mg_db.cursor()
        c.execute(
            f"SELECT scene_id FROM scene_likes WHERE user_id=? AND scene_id IN ({placeholders})",
            [user_id] + scene_ids,
        )
        liked_ids = {r[0] for r in c.fetchall()}
    return {sid: (sid in liked_ids) for sid in scene_ids}

def is_skill_liked(user_id: int, skill_id: int) -> bool:
    """检查用户是否已点赞某技能"""
    with db_lock:
        c = _mg_db.cursor()
        c.execute("SELECT 1 FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        return c.fetchone() is not None

def toggle_skill_like(user_id: int, skill_id: int) -> tuple[bool, int]:
    """技能点赞 toggle：已赞→取消，未赞→点赞。
    返回 (liked, like_count)。在 db_lock 事务内原子执行。
    """
    with db_lock:
        c = _mg_db.cursor()

        def _bump_like(delta: int) -> None:
            """P3-5f：like_count 在 runtime 域（skill_likes 仍是本域，保持本地）。"""
            from src.kernel.domain_read import runtime_lib as _rt_lib_lk
            from src.kernel import cross_write as _xw_lk
            _rt_lk = _rt_lib_lk(foreign_only=True)
            if _rt_lk is not None:
                _rt_lk_ok = _rt_lk
                if _xw_lk.submit_if_lib(
                        _rt_lk_ok, "runtime", "skill_like_bump",
                        f"skill-like:{skill_id}:{delta:+d}",
                        {"skill_id": int(skill_id), "delta": int(delta)},
                        lambda: _rt_lk_ok.adjust_skill_like_count(int(skill_id), int(delta)), log):
                    return
            # skill 归运行域：自增/自减走具名写查询（任意拓扑，仍是原子 UPDATE）
            _rt.exec_any("skill_like_inc" if delta > 0 else "skill_like_dec", (skill_id,))

        c.execute("SELECT 1 FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        if c.fetchone():
            c.execute("DELETE FROM skill_likes WHERE user_id=? AND skill_id=?", (user_id, skill_id))
            _bump_like(-1)
            _mg_db.commit()
            row = _rt.read_one_any("skill_like_count", (skill_id,))
            return (False, row[0] if row else 0)
        else:
            c.execute("INSERT INTO skill_likes (user_id, skill_id) VALUES (?, ?)", (user_id, skill_id))
            _bump_like(+1)
            _mg_db.commit()
            row = _rt.read_one_any("skill_like_count", (skill_id,))
            return (True, row[0] if row else 0)

def batch_skill_liked(user_id: int, skill_ids: list[int]) -> dict[int, bool]:
    """批量查询用户对多个技能的点赞状态，返回 {skill_id: bool}"""
    if not skill_ids or not user_id:
        return {}
    placeholders = ",".join(["?"] * len(skill_ids))
    with db_lock:
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
        c = _mg_db.cursor()
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
    # P3.5：skill 归运行域；lib 可用时走 owner 域读（排序仍在 mgmt 侧决定）
    from src.kernel.domain_read import owner_lib as _owner_lib  # noqa: PLC0415
    _rt_fs = _owner_lib("runtime", foreign_only=True)
    if _rt_fs is not None:
        try:
            return _rt_fs.list_featured_skills(list(ordered_ids or []), limit)
        except Exception as _exc:  # noqa: BLE001
            log.warning(f"runtime lib list_featured_skills failed, fallback: {_exc}")

    # 任意拓扑：精选技能取数（含「admin 排序 + like_count 降序追加」）在 owner 域一份 SQL
    return _rt.list_featured_skills_any(list(ordered_ids or []), limit)

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
        from src.kernel.domain_read import runtime_lib as _ru_lib_43d037_f
        _ru_lib_43d037 = _ru_lib_43d037_f(foreign_only=True)
        if _ru_lib_43d037 is not None:
            rows = _ru_lib_43d037.read_all("skill_43d037", (agent_type,))
        else:
            # skill 归运行域：具名查询（列序与原 SQL 一致，任意拓扑）
            rows = _rt.read_all_any("skills_official_by_agent", (agent_type,))
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

def _upsert_repo_credential(c, repo_key: str, cred_type: str, value: "str | None") -> None:
    """D98/D99/N4：grape_t_repo_credentials 幂等 upsert（UPDATE → rowcount==0 → INSERT）。

    供 add_managed_repo / _update_repo_bot_token 等写入口共用；
    id 首写 uuid7（主键，更新不碰），value 走 _encrypt_repo_secret。
    F4/D106 修订①：原裸 INSERT 撞 UNIQUE(repo_key, cred_type)（S82 基线
    3 红根因），同仓二次轮换 500——两步法即本助手函数。

    归属更正：该表归 **insight**（不是 runtime）。三步（UPDATE→INSERT→冲突再 UPDATE）
    收进 `insight.public.upsert_repo_credential_value`，由 owner 域在同一事务内执行；
    调用方传入的 `c` 不再参与（跨库拓扑下本就不可能与本域写同事务）。
    """
    _now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 空值 = 没有这行：写空会留下「配了但不可用」的空行，git-credential 的显式 bot 闸
    # 会把这种行判成 424 stage=empty 并停止下降（2026-11-21 实测：6 个 gitee 仓库的
    # 空占位行就是这样来的）。所以清空/未配置一律走删除，让解析继续往下一档。
    if not str(value or "").strip():
        _insight_public.delete_repo_credential_value(repo_key, cred_type)
        return
    _enc = _encrypt_repo_secret(value or "")
    _insight_public.upsert_repo_credential_value(repo_key, cred_type, _enc, _now)

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
            c = _mg_db.cursor()
            c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            return bool(c.fetchone())
    except Exception as e:
        log.warning(f"Managed repo exists check failed: {e}")
        return False

def _webhook_url_for_provider(base_url: str, provider: str) -> str:
    """webhook 回调 URL 的平台段由平台卡 webhook_path 决定（数据驱动，无平台字面量）。

    注意：`base_url` 里原有的 path（哪怕写的是 `/webhook/gitlab`）**会被整体替换掉**——
    同一份基址喂给不同平台得到不同回调地址，这是"配置只写公网基址"的实现落点。
    """
    if not base_url:
        return base_url
    try:
        from src.domains.runtime.public import get_platform_card
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
        from src.domains.mgmt.vcs import create_provider
        _col = create_provider(provider).token_storage_field()
    except Exception:
        _col = ""
    _has_col = bool(_re_crbt.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _col or ""))
    _now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _sm_dl:
            # 晚绑定 _sm_db（测试按 patch("src.kernel.session_manager.db") 打桩），
            # 不能换成域代理 —— 换掉就打桩失效、单测连真库。
            c = _sm_db.cursor()
            try:
                if _has_col:
                    c.execute(
                        f"UPDATE managed_repos SET {_col}='', bot_token_status='' "
                        f"WHERE repo_key=?", (repo_key,))
                    found = c.rowcount > 0
                else:
                    # 注：本函数当前**零调用方**（审计实测）。凭据表归 insight，走该域原语
                    # （不再给死代码留跨域 SQL；P4 前要么删除、要么随 purge 原语一起收敛）。
                    _insight_public.clear_repo_credential_value(repo_key, _now)
                    c.execute(
                        "UPDATE managed_repos SET bot_token_status='' WHERE repo_key=?",
                        (repo_key,))
                    found = c.rowcount > 0
                _sm_db.commit()
            finally:
                try:
                    c.close()
                except Exception:
                    pass
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

def _load_credential_json(raw: str, *, errors: "dict | None" = None) -> dict:
    """读侧统一解密+反序列化（N1 通道加密配套）。

    优先 decrypt()（密文格式 {kid}:{nonce}:{ct}），解密产物/明文行统一
    json.loads。decrypt 的明文兼容（无冒号分隔符原样返回）天然覆盖历史
    明文行——读路径零迁移。

    边界约束（2026-11-18 架构观察 5）：``errors`` 里的 ``credential_error_detail``
    只给服务端日志/审计用，**不得进入任何 HTTP 响应**——响应面只允许出现
    ``credential_error`` 代码值（decrypt_failed / parse_failed），避免解密实现细节外泄。

    解析失败仍返回 {}（调用方零改动），但会把失败原因写进 ``errors``
    （2026-11-18：凭据密文解不开时此前**完全静默**，上层只能看到"没有 token"，
    于是把"配置在但坏了"误判成"没配置"，项目页整块回落引导页）。
    """
    from src.domains.mgmt.auth.credential_crypto import decrypt
    if not raw:
        return {}
    try:
        plain = decrypt(raw)
    except Exception as e:
        if errors is not None:
            errors["credential_error"] = "decrypt_failed"
            errors["credential_error_detail"] = str(e)[:120] or type(e).__name__
        return {}
    try:
        return json.loads(plain)
    except Exception as e:
        if errors is not None:
            errors["credential_error"] = "parse_failed"
            errors["credential_error_detail"] = str(e)[:120]
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
        from src.domains.runtime.public import get_platform_card
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
    _plat = _platform_bot_token(provider)
    if _plat:
        return _plat
    key = f"global_bot_{provider}_token"
    # D110：种子文件按 provider 取值（不再限于 gitcode/github 两平台的历史字段名）
    return _cached_get_meta(key, file_fallback_fn=lambda: _bot_seed_value(provider, "token"))

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
    _plat = _platform_bot_row(provider).get("username") or ""
    if _plat:
        return [_plat]
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
        from src.domains.runtime.public import default_vcs_provider
        _default = default_vcs_provider()
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
        # 凭据表归 insight：批量判存走该域原语（任意拓扑）
        return _insight_public.any_repo_credential(keys)
    except Exception as e:
        log.warning(f"any_repo_credential_for_repos failed: {e}")
        return False

def get_repo_credential_meta(repo_key: str, cred_type: str) -> "dict | None":
    """D90-N4：读 rc 凭据 + 审计锚点（id）一次查询。

    返回 {"value": 解密后明文, "id": 行 UUID}；无行返回 None。
    供 RepoBotToken 命中审计用（value 与 id 同事务一致）。
    """
    try:
        # 凭据表归 insight：value + 审计锚点一次查询（同事务一致）
        row = _insight_public.repo_credential_meta(repo_key, cred_type)
        if not row:
            return None
    except Exception as e:
        log.warning(f"get_repo_credential_meta failed: {e}")
        return None
    # 行存在 ≠ 值可用：解密失败必须与「无行」区分开（2026-11-21）。
    # 混在一起会让上层把「配了但解不开」当成「没配」→ 静默降级到用户身份，
    # 正是 D90 禁止的身份混用。
    raw = row[0] or ""
    reason = ""
    try:
        plain, undecryptable = _decrypt_repo_secret(raw), False
    except Exception as e:
        from src.domains.mgmt.auth.credential_crypto import UnknownKeyError

        reason = "unknown_key" if isinstance(e, UnknownKeyError) else "decrypt_failed"
        log.warning(f"repo credential unusable: repo_key={repo_key} cred_type={cred_type} "
                    f"reason={reason} err={type(e).__name__}")
        plain, undecryptable = "", True
    return {"value": plain, "id": row[1] or "", "undecryptable": undecryptable,
            "reason": reason}

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
        # 凭据表归 insight：单条读取走该域原语（未解密，由本函数统一解密）
        return _decrypt_repo_secret(_insight_public.repo_credential_value(repo_key, cred_type))
    except Exception as e:
        log.warning(f"get_repo_credential_value failed: {e}")
        return ""


"""用户系统数据库：users/credentials/vms/skills 表管理

D32 迁移完成后 user_accounts 表为死表，不再被业务代码读取。
Issue #1588 B 批：老表包装函数（create_user/get_user/get_user_by_username/list_users/
delete_user/update_password/verify_user/get_token_version/increment_token_version/
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
            c = _mg_db.cursor()
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

