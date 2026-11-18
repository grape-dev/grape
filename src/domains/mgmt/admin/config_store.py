"""Bot config DB meta + oauth-providers.json 读写。

Issue #345: Bot 配置从文件迁移到 DB meta 表（DB 是唯一权威源）。
global-bot-token.json 保留只读，仅供启动迁移读取（迁移后不再被运行时访问）。

- Bot 运行时配置（api_token / webhook_token / bot_usernames）：DB meta 表，唯一权威源
- oauth-providers.json：账号体系 OAuth 配置（协议参数 + 凭据），meta 表同步后运行时从 DB 读取

admin 配置中心 save_config 分流：bot 字段写 DB meta，OAuth 字段写 oauth-providers.json + 同步 meta 表。
前端 API 不变（ConfigBody 扁平结构不变），后端做扁平→嵌套映射。
"""

import json

import os

from src.kernel.config import log, CONFIG_DIR

# D110：种子文件 configs/global-bot-token.json（兼容名 platform-bots.json 也认），路径读取时解析

_OAUTH_PATH = os.path.join(CONFIG_DIR, "oauth-providers.json")

# 历史明文 secret 清理：每进程一次（幂等函数 purge_legacy_secret_copies）
_PURGE_DONE = False

# Bot 字段——写入 DB meta 表（Issue #345: DB 是唯一权威源）
_BOT_FIELDS = [
    "gitcode_api_token",
    "github_bot_token",
    "github_bot_username",
    "gitcode_webhook_token",
    "webhook_url",
    "bot_usernames",
]

# 运行参数字段（Issue #470：写死配置抽取到管理台）——写 DB meta，运行时经
# src/kernel/config.py 统一 getter 读取（env > meta > runtime-config.json > 默认）。
# 非敏感，明文回显。空值/0 = 未设置（回退下一层），沿用「只保存非空字段」约定。
_RUNTIME_FIELDS = [
    "runtime_task_timeout",
    "devstage_template_id",
    "devstage_flavor_id",
    "devstage_cores",
    "devstage_memory",
    "devstage_arch",
]

# OAuth 扁平字段 → oauth-providers.json provider 条目映射
# flat_field: (provider, cred_field)
_OAUTH_FIELD_MAP = {
    "gitcode_oauth_client_id": ("gitcode", "client_id"),
    "gitcode_oauth_client_secret": ("gitcode", "client_secret"),
    "github_oauth_client_id": ("github", "client_id"),
    "github_oauth_client_secret": ("github", "client_secret"),
    # D90-N5 补齐：gitee 也是 OAuth 平台之一，管理台需能保存其凭据
    "gitee_oauth_client_id": ("gitee", "client_id"),
    "gitee_oauth_client_secret": ("gitee", "client_secret"),
    "huawei_oauth_client_id": ("huawei", "client_id"),
    "huawei_oauth_client_secret": ("huawei", "client_secret"),
}

# 全字段清单（前端展示用）
ALL_FIELDS = _BOT_FIELDS + list(_OAUTH_FIELD_MAP.keys())

def get_config_path() -> str:
    from src.kernel.config import bot_seed_path

    return bot_seed_path()

def load_config() -> dict:
    """Read global-bot-token.json; return empty dict if missing or corrupt.

    Note: Used only by startup migration (_migrate_global_bot_token_to_db).
    Runtime code reads from DB meta table via unified entry functions.
    """
    try:
        from src.kernel.config import bot_seed_path  # 本函数的局部 import（get_config_path 里那处不覆盖这里）

        _seed_path = bot_seed_path()
        if os.path.exists(_seed_path):
            with open(_seed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"admin config_store: load failed: {e}")
    return {}

def read_public_config() -> dict:
    """返回脱敏配置（前端展示）。

    Issue #345: Bot 字段从 DB meta 表读取（DB 是唯一权威源）。
    OAuth 配置从 meta 表读取。
    """
    from src.domains.mgmt.user.db import get_meta_value
    import json as _json
    out = {}
    # Bot 字段——从 DB meta 表读取（Issue #345）
    _BOT_META_MAP = {
        "gitcode_api_token": "global_bot_gitcode_token",
        "github_bot_token": "global_bot_github_token",
        "github_bot_username": "global_bot_github_username",
        "gitcode_webhook_token": "gitcode_webhook_token",
        "webhook_url": "webhook_url",
        "bot_usernames": "global_bot_usernames",
    }
    for field, meta_key in _BOT_META_MAP.items():
        val = get_meta_value(meta_key) or ""
        if field == "bot_usernames" and val:
            try:
                val = _json.loads(val)
            except Exception:
                val = []
        if isinstance(val, list):
            out[field] = val
        elif isinstance(val, str) and val:
            tail = val[-4:] if len(val) >= 4 else val
            out[f"{field}_present"] = True
            out[f"{field}_tail"] = tail
            # 非敏感字段回显明文：webhook_url / github_bot_username
            if field in ("webhook_url", "github_bot_username"):
                out[field] = val
    # OAuth 字段：与运行时同一取凭据入口（Issue #3054 P1-1：configured / 回显同源，
    # 不再从 meta 判断——凭据迁表后 meta 可能为空或过期）
    from src.domains.mgmt.auth.providers import channel_configured, list_providers
    from src.domains.mgmt.auth.credential_source import provider_credentials

    _cred_by_provider: dict[str, tuple[str, str]] = {}

    def _creds(p: str) -> tuple[str, str]:
        if p not in _cred_by_provider:
            cid, sec = provider_credentials.pair(p)
            # 密文不可解：配置页只显示"没有可用凭据"，原因已落
            # credential_decrypt_failed 审计事件
            _cred_by_provider[p] = (cid.value, sec.value)
        return _cred_by_provider[p]

    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        val = _creds(provider)[0 if cred_field == "client_id" else 1]
        if val:
            out[f"{flat_field}_present"] = True
            out[f"{flat_field}_tail"] = val[-4:] if len(val) >= 4 else val
            # client_id 非敏感，回显明文便于前端回填
            if cred_field == "client_id":
                out[flat_field] = val
    # oauth_status（动态从 provider 列表生成，不硬编码平台名）
    out["oauth_status"] = {p: channel_configured(p) for p in list_providers()}
    # allow_signup — 归一化为 "1"/"0"（兼容 meta 中可能存在的 "true"/"yes" 旧值）
    from src.domains.mgmt.auth.signup_policy import _is_allow_signup
    out["allow_signup"] = "1" if _is_allow_signup() else "0"
    # 运行参数（Issue #470）：明文回显管理台设置值（未设置回退展示默认）+
    # 当前生效值（含 env/文件层，便于管理员核对覆盖关系）
    from src.kernel.config import (get_runtime_task_timeout, get_devstage_template_id,
                            get_devstage_flavor, RUNTIME_CONFIG_PATH)
    _rt_meta = {f: (get_meta_value(f) or "") for f in _RUNTIME_FIELDS}
    _fl_eff = get_devstage_flavor()

    def _rt_int(meta_val, fallback):
        """meta 数值字段防御性转换（QA Minor2）：非数字/非正数脏数据
        （绕过 API 写库）回退默认，避免 /api/admin/config GET 500。"""
        try:
            v = int(meta_val)
            return v if v > 0 else fallback
        except Exception:
            return fallback

    # 未设置（空）时回显默认值——管理员看到的「当前值」语义；设置过则回显设置值
    out["runtime_task_timeout"] = _rt_int(_rt_meta["runtime_task_timeout"], get_runtime_task_timeout())
    out["devstage_template_id"] = _rt_meta["devstage_template_id"] or get_devstage_template_id()
    out["devstage_flavor_id"] = _rt_meta["devstage_flavor_id"] or _fl_eff["flavor_id"]
    out["devstage_cores"] = _rt_int(_rt_meta["devstage_cores"], _fl_eff["cores"])
    out["devstage_memory"] = _rt_int(_rt_meta["devstage_memory"], _fl_eff["memory"])
    out["devstage_arch"] = _rt_meta["devstage_arch"] or _fl_eff["arch"]
    out["runtime_task_timeout_effective"] = get_runtime_task_timeout()
    out["devstage_template_id_effective"] = get_devstage_template_id()
    out["devstage_flavor_effective"] = _fl_eff
    out["runtime_config_file"] = os.path.exists(RUNTIME_CONFIG_PATH)
    # Issue #3054 P1-1：历史明文 secret 副本一次性清理（幂等、每进程一次）
    _purge_once()
    return out

def save_config(payload: dict) -> dict:
    """保存配置——分流到 DB meta + oauth-providers.json。

    Issue #345: Bot 字段写 DB meta 表（唯一权威源），不写文件。
    OAuth 字段写 oauth-providers.json + 同步 meta 表。

    只更新 payload 中给出的字段；未给出的字段保留原值。
    返回落盘后的完整配置（脱敏）。
    """
    # 0. 运行参数字段（Issue #470）→ DB meta + 失效统一配置缓存
    runtime_payload = {k: v for k, v in payload.items() if k in _RUNTIME_FIELDS}
    if runtime_payload:
        from src.domains.mgmt.user.db import set_meta_value
        for field, val in runtime_payload.items():
            # 数值字段类型校验：超时/cores/memory 必须为正整数（值本身已在路由层
            # 通过 ConfigBody 类型 + gt 约束校验，这里防御性兜底）
            if field in ("runtime_task_timeout", "devstage_cores", "devstage_memory"):
                try:
                    if int(val) <= 0:
                        continue
                except Exception:
                    continue
            set_meta_value(field, str(val))
        # 当前实例立即生效（其他实例 ≤60s TTL）
        from src.kernel.config import invalidate_runtime_config_cache
        invalidate_runtime_config_cache()
        log.info("admin config_store: runtime config updated (DB meta)")

    # 1. Bot 字段 → DB meta（唯一权威源）
    # Issue #345: 集群部署下 DB 是唯一权威源，不再写文件。
    # global-bot-token.json 保留只读，仅供启动迁移读取（迁移后不再被运行时访问）。
    bot_payload = {k: v for k, v in payload.items() if k in _BOT_FIELDS}
    if bot_payload:
        from src.domains.mgmt.user.db import set_meta_value, get_meta_value
        import json as _json
        _BOT_META_MAP = {
            "gitcode_api_token": "global_bot_gitcode_token",
            "github_bot_token": "global_bot_github_token",
            "github_bot_username": "global_bot_github_username",
            "gitcode_webhook_token": "gitcode_webhook_token",
            "webhook_url": "webhook_url",
            "bot_usernames": "global_bot_usernames",
        }
        # Issue #372 §13.2: 检测 bot token 从无到有的转换，用于触发自动升级
        # R21.1: 检测 bot token 有→有换账号，触发 global_bot_token_swap
        # R11.5: 加 db_lock 防止 TOCTOU 竞态（多管理员并发保存重复触发 upgrade）
        from src.domains.mgmt.user.db import db_lock
        _upgrade_triggers = []  # [(provider, new_token, bot_username)] — 从无到有
        _swap_triggers = []     # [(provider, old_token, new_token)] — 有→有换账号
        with db_lock:
            for field, meta_key in _BOT_META_MAP.items():
                if field in bot_payload:
                    val = bot_payload[field]
                    # 检测从无到有 / 有→有换账号
                    if field in ("gitcode_api_token", "github_bot_token") and val:
                        _old = get_meta_value(meta_key) or ""
                        # D43/S63：provider 从字段名派生（gitcode_api_token→gitcode、github_bot_token→github）
                        provider = field.split("_")[0]
                        if not _old:
                            # 从无到有 → auto_upgrade
                            _upgrade_triggers.append((provider, str(val)))
                        elif _old != str(val):
                            # 有→有换账号 → global_bot_token_swap
                            _swap_triggers.append((provider, _old, str(val)))
                    if isinstance(val, list):
                        set_meta_value(meta_key, _json.dumps(val))
                    else:
                        set_meta_value(meta_key, str(val) if val else "")
        # D110：同一份 payload 同步到平台行（新真源；meta 保留为兼容期第 3 级来源）。
        # 放在 db_lock 之外：平台行走 SQLAlchemy 会话，避免与 sqlite 线程锁叠锁。
        _sync_platform_bot_rows(bot_payload)
        # Issue #345: 清除本实例缓存（当前实例立即生效，其他实例最多 60s TTL 后生效）
        from src.domains.mgmt.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
        # R16.3.2: 清除全局 bot token 过期标记（per-provider, R21.2）
        # 管理员更新 bot token 时清除对应 provider 的过期状态，短路自动解除
        from src.domains.mgmt.user.db import set_meta_value
        if "gitcode_api_token" in bot_payload:
            set_meta_value("global_gitcode_bot_token_status", "")
        if "github_bot_token" in bot_payload:
            set_meta_value("global_github_bot_token_status", "")
        log.info("admin config_store: bot config updated (DB)")

        # Issue #372 §13.2: bot token 从无到有时异步升级路径 B 仓库
        if _upgrade_triggers:
            from src.domains.mgmt.user.repo_manager import resolve_bot_username
            import threading
            for _provider, _token in _upgrade_triggers:
                # D30/S50：bot 身份统一来源链（存量平台全局 meta，config-only 走卡）
                _bot_username = resolve_bot_username(_provider)
                if _bot_username:
                    from src.domains.mgmt.user.db import auto_upgrade_path_b_repos
                    threading.Thread(
                        target=auto_upgrade_path_b_repos,
                        args=(_provider, _token, _bot_username),
                        daemon=True,
                    ).start()
                    log.info(f"config_store: triggered auto_upgrade_path_b_repos for {_provider}")

        # R21.1: bot token 有→有换账号时异步执行 global_bot_token_swap
        if _swap_triggers:
            import threading
            for _provider, _old_token, _new_token in _swap_triggers:
                threading.Thread(
                    target=_global_bot_token_swap_wrapper,
                    args=(_provider, _old_token, _new_token),
                    daemon=True,
                ).start()
                log.info(f"config_store: triggered global_bot_token_swap for {_provider}")

    # 2. OAuth 字段 → 渠道表加密列（唯一真源，Issue #3054 P1-1）
    #    明文 secret 不再写 meta、不再写 configs/oauth-providers.json：
    #    - 表 = 唯一存放点（client_id_enc / client_secret_enc，AES-GCM 密文）
    #    - meta 只保留 client_id（非敏感，兼容回显/迁移源）
    #    - json 只保留协议参数（client_id），secret 一律剥离
    oauth_payload = {k: v for k, v in payload.items() if k in _OAUTH_FIELD_MAP}
    if oauth_payload:
        _table_written = save_oauth_credentials(oauth_payload)
        # 同步 meta 表：只写非敏感字段；表不可写（迁移窗口）才把 secret 留 meta 兜底
        from src.domains.mgmt.user.db import set_meta_value
        for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
            if flat_field not in oauth_payload:
                continue
            val = oauth_payload[flat_field]
            if not val:
                continue
            if cred_field == "client_secret":
                if not _table_written.get(provider):
                    # 老库渠道行不存在 / 表不可写：保留旧行为，否则本次 secret 无处可存
                    set_meta_value(f"oauth_{provider}_{cred_field}", val)
                    log.warning("admin config_store: oauth channel table unavailable, "
                                "client_secret kept in meta (migration window, provider=%s)",
                                provider)
                continue
            set_meta_value(f"oauth_{provider}_{cred_field}", val)
        _save_oauth_providers(oauth_payload)
        log.info("admin config_store: oauth credentials → channel table (encrypted); "
                 "meta/configs carry no plaintext secret")
        # P2-2: 失效 Provider 内存缓存，下次请求时从 DB 重新加载
        from src.domains.mgmt.auth.providers import invalidate_provider_cache
        invalidate_provider_cache()
        # 历史明文副本一次性清理（幂等）
        purge_legacy_secret_copies()

    # 3. D109：同步到平台对象表（表=运行时真源；fail-safe，不阻断旧保存路径）
    from src.domains.platform.public import sync_from_admin_payload
    sync_from_admin_payload(payload)

    return read_public_config()

def platform_bot_old_token(provider: str) -> str:
    """读平台行当前 bot token 明文（解密；无值/失败返回空串）。仅供轮换比较用。"""
    try:
        from sqlalchemy.orm import Session

        from src.domains.mgmt.user.db import _decrypt_repo_secret
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            enc = RepoPlatformRepo.get_bot_token_enc(s, provider)
        return _decrypt_repo_secret(enc) if enc else ""
    except Exception as e:
        log.warning(f"platform_bot_old_token({provider}) failed: {e}")
        return ""


def trigger_platform_bot_swap(provider: str, old_token: str, new_token: str) -> None:
    """D110 R1：平台 bot token 轮换必须传播到已托管仓库。

    path A 托管把全局 bot token 同时写成仓库 webhook 验签 token（routes/repos.py:976/986），
    不同步刷这批仓库 → 所有老仓库 webhook 立刻 403。异步 + 分布式锁（同 save_config 口径）。
    """
    if not (old_token and new_token) or old_token == new_token:
        return
    try:
        import threading

        threading.Thread(target=_global_bot_token_swap_wrapper,
                         args=(provider, old_token, new_token), daemon=True).start()
        log.info(f"config_store: triggered platform bot swap for {provider}")
    except Exception as e:
        log.warning(f"trigger_platform_bot_swap({provider}) failed: {e}")


def _sync_platform_bot_rows(payload: dict) -> None:
    """D110：把 admin 配置里的 bot 字段写进 grape_t_repo_platforms（一平台一个 bot）。

    gitcode_api_token → (gitcode, token)；github_bot_token → (github, token)；
    github_bot_username → (github, username)。bot_usernames 池已退役（一平台一 bot），忽略。
    平台行不存在时跳过（行由配置导入建立），不在这里凭空建行。
    """
    _map = {
        "gitcode_api_token": ("gitcode", "token"),
        "github_bot_token": ("github", "token"),
        "github_bot_username": ("github", "username"),
    }
    # provider → 承载 bot token 的扁平配置字段（轮换传播时反查，不再写 provider 字面量比较）
    _token_field_by_provider = {p: f for f, (p, k) in _map.items() if k == "token"}
    wanted: dict[str, dict] = {}
    old_tokens: dict[str, str] = {}
    for field, (provider, kind) in _map.items():
        if field not in payload:
            continue
        val = payload.get(field)
        if kind == "token":
            if not val:
                continue
            try:
                from src.domains.mgmt.auth.credential_crypto import encrypt
                old_tokens[provider] = platform_bot_old_token(provider)
                wanted.setdefault(provider, {})["token_enc"] = encrypt(str(val))
            except Exception as e:
                log.warning(f"_sync_platform_bot_rows: encrypt failed for {provider}: {e}")
                continue
        else:
            wanted.setdefault(provider, {})["username"] = str(val or "")
    if not wanted:
        return
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            for provider, fields in wanted.items():
                RepoPlatformRepo.set_bot(s, provider, **fields)
            s.commit()
        log.info(f"admin config_store: platform bot rows synced {sorted(wanted)}")
        # D110 R1：token 轮换传播（path A 仓库的 webhook 验签 token 同步刷新）
        for provider, fields in wanted.items():
            if "token_enc" not in fields:
                continue
            _new_plain = str(payload.get(_token_field_by_provider.get(provider, "")) or "")
            trigger_platform_bot_swap(provider, old_tokens.get(provider, ""), _new_plain)
    except Exception as e:
        log.warning(f"_sync_platform_bot_rows failed: {e}")

def save_oauth_credentials(oauth_payload: dict) -> "dict[str, bool]":
    """把扁平 OAuth 凭据写进渠道表加密列——**唯一存放点**（Issue #3054 P1-1）。

    返回 ``{provider: 是否已写表}``。明文字段在内存里加密后立刻落库，明文既不写
    meta 也不写 configs/oauth-providers.json。渠道行不存在时返回 False（调用方决定
    迁移窗口兜底），**绝不**静默把明文写去 meta 假装成功。

    表归属：``grape_t_oauth_channels`` 是 mgmt 库的 D109 对象表（mgmt 域所有），
    管理面（mgmt 进程）直连自己的表，不跨域。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt

    wanted: dict[str, dict[str, str]] = {}
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        val = str(oauth_payload.get(flat_field) or "")
        if not val:
            continue
        col = "client_id_enc" if cred_field == "client_id" else "client_secret_enc"
        wanted.setdefault(provider, {})[col] = encrypt(val)
    if not wanted:
        return {}

    from datetime import datetime, timezone

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.models_platform_object import OAuthChannelModel
    from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    written: dict[str, bool] = {}
    with Session(get_engine()) as s:
        for provider, cols in wanted.items():
            if OAuthChannelRepo.get(s, provider) is None:
                written[provider] = False
                log.warning("admin config_store: oauth 渠道 %s 不在对象表，凭据未落库", provider)
                continue
            s.execute(
                update(OAuthChannelModel)
                .where(OAuthChannelModel.provider == provider)
                .values(**dict(cols, updated_at=now))
            )
            written[provider] = True
        s.commit()
    if any(written.values()):
        from src.domains.platform.public import bump_epoch

        bump_epoch()
    return written


def _purge_once() -> None:
    """每进程只跑一次的历史明文 secret 清理（幂等；失败只告警，不影响配置读写）。"""
    global _PURGE_DONE
    if _PURGE_DONE:
        return
    _PURGE_DONE = True
    try:
        purge_legacy_secret_copies()
    except Exception as e:
        log.warning(f"admin config_store: purge_legacy_secret_copies failed: {e}")


def purge_legacy_secret_copies() -> "dict[str, list[str]]":
    """一次性清理历史明文 secret 副本（幂等，Issue #3054 P1-1）。

    只清两份**历史副本**，且前提是对应渠道在表里已有可解密的密文（否则删了就没凭据）：
    1. meta ``oauth_<p>_client_secret`` —— 明文；
    2. ``configs/oauth-providers.json`` 里各渠道的 ``client_secret`` 键（其它键一律保留）。

    另外清理 ``meta.oauth_providers_config`` 卡内遗留的 ``client_secret``。不删
    client_id / redirect_uri / name 等任何其它字段。返回 {"meta": [...], "configs": [...]}
    便于测试与日志核对。
    """
    from src.domains.mgmt.auth.credential_crypto import decrypt
    from src.domains.mgmt.user.db import get_meta_value, set_meta_value

    removed = {"meta": [], "configs": []}
    providers = {p for p, _cf in _OAUTH_FIELD_MAP.values()}

    # 表里哪几家的密文可解（只有这些才允许删历史明文）
    backed: set[str] = set()
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        with Session(get_engine()) as s:
            for provider in providers:
                row = OAuthChannelRepo.get(s, provider)
                blob = str(getattr(row, "client_secret_enc", "") or "") if row else ""
                if not blob:
                    continue
                try:
                    if decrypt(blob):
                        backed.add(provider)
                except Exception as e:
                    log.warning("purge_legacy_secret_copies: 密文不可解，保留 meta 副本 "
                                "(provider=%s): %s", provider, e)
    except Exception as e:
        log.warning("purge_legacy_secret_copies: 表探测失败，本轮不清理 meta: %s", e)
        return removed

    for provider in sorted(backed):
        key = f"oauth_{provider}_client_secret"
        if (get_meta_value(key) or "").strip():
            set_meta_value(key, "")
            removed["meta"].append(key)

    # meta 里的渠道卡 JSON：剥离 client_secret（保留其它键）
    try:
        raw = get_meta_value("oauth_providers_config") or ""
        if raw.strip():
            cards = json.loads(raw)
            dirty = False
            if isinstance(cards, dict):
                for provider, card in cards.items():
                    # Issue #3054 QA：只有「表里已有可解密文」的渠道才允许剥明文副本；
                    # 否则可能删掉唯一一份明文（表为空/不可解时）。
                    if provider not in backed:
                        continue
                    if isinstance(card, dict) and card.pop("client_secret", None):
                        dirty = True
                        removed["meta"].append(f"oauth_providers_config.{provider}.client_secret")
            if dirty:
                set_meta_value("oauth_providers_config", json.dumps(cards, ensure_ascii=False))
    except Exception as e:
        log.warning("purge_legacy_secret_copies: oauth_providers_config 清理失败: %s", e)

    # configs/oauth-providers.json：只剥 client_secret（其它键一律保留）
    try:
        if os.path.exists(_OAUTH_PATH):
            with open(_OAUTH_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
            dirty = False
            if isinstance(data, dict):
                for provider, card in data.items():
                    # 同上：只剥「表里已有可解密文」的渠道，避免删唯一明文副本。
                    if provider not in backed:
                        continue
                    if isinstance(card, dict) and card.pop("client_secret", None) is not None:
                        dirty = True
                        removed["configs"].append(f"{provider}.client_secret")
            if dirty:
                tmp = _OAUTH_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, _OAUTH_PATH)
    except Exception as e:
        log.warning("purge_legacy_secret_copies: oauth-providers.json 清理失败: %s", e)

    if removed["meta"] or removed["configs"]:
        log.info("admin config_store: 历史明文 client_secret 已清理 meta=%s configs=%s",
                 removed["meta"], removed["configs"])
    return removed


def _save_oauth_providers(oauth_payload: dict):
    """把 ConfigBody 扁平 OAuth 字段写入 oauth-providers.json（协议参数，不含凭据）。

    读取现有 json，合并新字段，原子写回。
    Issue #3054 P1-1：**client_secret 永不写文件**（明文可被 git 托管，且加载时
    本来就会被 importer 剥离——之前那条路径纯属"写一次明文垃圾"）。
    """
    existing = {}
    if os.path.exists(_OAUTH_PATH):
        try:
            with open(_OAUTH_PATH, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            pass
    # 扁平 → 嵌套（secret 跳过；顺带剥掉历史遗留的明文 secret 键）
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        card = existing.get(provider)
        if isinstance(card, dict):
            card.pop("client_secret", None)
        if flat_field in oauth_payload and cred_field != "client_secret":
            if not isinstance(existing.get(provider), dict):
                existing[provider] = {}
            existing[provider][cred_field] = oauth_payload[flat_field]
    # 原子写
    tmp = _OAUTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _OAUTH_PATH)

def verify_token(provider: str, token: str) -> dict:
    """用 token 调平台 /user 接口校验，返回登录名 + 平台。

    D30/S50：卡驱动（create_provider → GenericVcsProvider.verify_token，
    api_base/api_auth 按平台卡）——任何已配平台可用，gitcode/github
    存量行为由内置卡对齐。
    """
    from src.kernel.token_resolver import create_provider
    user_data = create_provider(provider).verify_token(token)
    if not isinstance(user_data, dict):
        raise RuntimeError(f"verify_token({provider}): empty user info")
    login = str(user_data.get("login", "") or "")
    return {"provider": provider, "login": login, "name": user_data.get("name") or login}


def _global_bot_token_swap_wrapper(provider: str, old_token: str, new_token: str):
    """R21.1.3/R21.1.7: 后台线程 wrapper——加分布式锁 + 异常隔离。"""
    try:
        from src.kernel.lock import acquire_lock, release_lock
        if acquire_lock("global_bot_swap", ttl=600):
            try:
                global_bot_token_swap(provider, old_token, new_token)
            finally:
                release_lock("global_bot_swap")
        else:
            log.info(f"global_bot_token_swap: lock not acquired for {provider}, another instance running")
    except Exception as e:
        log.warning(f"global_bot_token_swap: failed for {provider}: {e}")

def global_bot_token_swap(provider: str, old_token: str, new_token: str):
    """R21.1.2: 全局 bot token 更换流程（有→有换账号）。

    7 步流程：
    1. 验证新 token（VCS API GET /user）
    2. 获取新 bot username
    3. 遍历路径 A 仓库（str(host_user_id or "") in ("", "0") 且 bot_gitcode_token==旧 token / bot_github_token==旧 token）
    4. 邀新 bot 为协作者（如果新 bot 与旧 bot_username 不同）
    5. 重建 webhook（用新 token 删旧 + 建新，password=新 token）
    6. 写 DB（更新 webhook_token + bot_username）
    7. best-effort 移除旧 bot 协作者身份

    R21.1.4: 失败时仓库保持旧状态，记录 warning。
    R21.1.5: 过期标记已在 save_config 中清除（R16.3.2）。
    """
    from src.kernel.token_resolver import create_provider
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.db import get_webhook_url, _get_managed_webhook_id
    from src.kernel.repo_key import split_owner_repo

    _vcs = create_provider(provider)
    platform_label = provider.capitalize()

    # 步骤 1: 验证新 token
    try:
        user_data = _vcs.verify_token(new_token)
    except Exception as e:
        log.warning(f"global_bot_token_swap: new {platform_label} token invalid: {e}")
        return

    # 步骤 2: 获取新 bot username
    new_bot_username = (user_data.get("login") or "") if isinstance(user_data, dict) else ""
    if not new_bot_username:
        log.warning(f"global_bot_token_swap: new {platform_label} token verify OK but no login")
        return

    log.info(f"global_bot_token_swap: starting for {platform_label}, new bot={new_bot_username}")

    # 步骤 3: 遍历路径 A 仓库（D99：cred_type 归一='bot'，平台由 repo_key 承载）
    # D90-N2① + D99：grape_t_repo_credentials cred_type 值域=归一常量 {'bot','webhook'}
    # （用户 PAT 权威=user_credentials 用户级，*_pat 禁绝不变；新增凭据种类须走契约评审）
    repos = []
    try:
        with db_lock:
            c = db.cursor()
            # S82/D58：凭据唯一权威=grape_t_repo_credentials（managed_repos 列已冻结）
            # F4/D106 修订（2026-11-11，r3 树既有形态确认）：N3 加密为随机
            # nonce——密文等值比对恒不命中（旧实现步骤 3 空转，换发从不发生）；
            # 拉全量 bot 行 Python 侧解密比对，解密失败按明文兼容期原值比对。
            from src.domains.mgmt.user.db import _decrypt_repo_secret as _dec_rs
            # D99：cred_type 归一='bot'（值域 {'bot','webhook'}）；token 本身
            # 平台专属（同一 token 不会跨平台有效），解密等值即平台定界
            # P4：这条是**跨域 JOIN**（runtime 的 rc + mgmt 的 managed_repos），删视图后不能一条 SQL 走完 →
            # 拆成「rc 走 lib 读 + managed_repos 本地读 + Python 内连接」，语义是 INNER JOIN，按 repo_key 求交。
            # 凭据表归 insight、managed_repos 归本域：跨域 JOIN 拆成两查后在 Python 内连接
            # （INNER JOIN 语义 = 按 repo_key 求交）
            from src.domains.insight import public as _insight_cred
            _rc_rows = _insight_cred.repo_bot_credential_pairs()
            c.execute(
                "SELECT repo_key, bot_username FROM managed_repos "
                "WHERE (host_user_id IS NULL OR host_user_id='' OR host_user_id='0')")
            _mr_map = {r[0]: r[1] for r in c.fetchall()}
            _rows_join = [(rk, _mr_map[rk], val) for (rk, val) in _rc_rows if rk in _mr_map]
            for row in _rows_join:
                _raw_val = row[2] or ""
                try:
                    _val = _dec_rs(_raw_val)
                except Exception:
                    _val = _raw_val
                if _val != old_token:
                    continue
                repos.append({"repo_key": row[0], "old_bot_username": row[1] or ""})
    except Exception as e:
        log.warning(f"global_bot_token_swap: query repos failed: {e}")
        return

    if not repos:
        log.info(f"global_bot_token_swap: no path A repos using old {platform_label} token")
        return

    log.info(f"global_bot_token_swap: {len(repos)} repos to update")

    _wh_url = get_webhook_url()
    if not _wh_url:
        log.warning("global_bot_token_swap: webhook_url not configured")
        return
    # D43/S63：按目标平台卡重建回调 URL（D12 既有函数）
    from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
    _wh_url = _rebuild_webhook_url_for_provider(_wh_url, provider)

    # 遍历每个仓库执行步骤 4-6
    for repo_info in repos:
        repo_key = repo_info["repo_key"]
        old_bot_username = repo_info["old_bot_username"]
        try:
            owner, repo_name = split_owner_repo(repo_key)
        except Exception:
            log.warning(f"global_bot_token_swap: cannot split {repo_key}, skipping")
            continue

        # 步骤 4: 邀新 bot（如果与旧 bot 不同）
        if new_bot_username != old_bot_username:
            try:
                # D43/S63：权限缺省=适配器存量默认（GitHub push / Gitee 系 maintainer）
                _vcs.add_collaborator(new_token, owner, repo_name, new_bot_username)
            except Exception as e:
                log.warning(f"global_bot_token_swap: add_collaborator failed for {repo_key} (continuing): {e}")

        # 步骤 5: 重建 webhook
        _old_hook_id = _get_managed_webhook_id(repo_key)
        if _old_hook_id:
            try:
                _vcs.remove_webhook(new_token, owner, repo_name, str(_old_hook_id))
            except Exception as e:
                log.warning(f"global_bot_token_swap: remove old webhook failed for {repo_key}: {e}")
                continue  # 删旧失败 → 不建新，保留旧状态

        try:
            _new_hook_id = _vcs.setup_webhook(new_token, owner, repo_name, _wh_url, webhook_secret=new_token)
        except Exception as e:
            log.warning(f"global_bot_token_swap: setup new webhook failed for {repo_key}: {e}")
            continue  # 建新失败 → 不写 DB，保留旧状态

        # 步骤 6: 写 DB
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE managed_repos SET webhook_id=?, bot_username=? WHERE repo_key=?",
                    (str(_new_hook_id), new_bot_username, repo_key),
                )
                # S82/D58：凭据单写 rc（managed_repos 列已冻结）
                # N3：value 通道加密
                # D99：cred_type 归一 {'bot','webhook'}；两步法 UPDATE→INSERT
                # （双方言一致，无 ON CONFLICT/REPLACE 方言分支）
                from src.domains.mgmt.user.db import _encrypt_repo_secret as _enc_rs
                # 归属更正：grape_t_repo_credentials 归 **insight**（此前注释写「runtime 域」
                # 是错的，只影响注释、不影响写入路径——写原语在 insight public，runtime
                # public 只是转发）。两步法 UPDATE→INSERT 收在 owner 域的 `_any_tx()` 里。
                import time as _t_gb
                from src.kernel.domain_read import runtime_lib as _rt_lib_gb
                from src.kernel import cross_write as _xw_gb
                _rt_gb = _rt_lib_gb(foreign_only=True)
                _gb_ts = _t_gb.strftime("%Y-%m-%d %H:%M:%S")
                _pending_creds: list[tuple[str, str]] = []
                for _ct in ("bot", "webhook"):
                    _gb_done = False
                    if _rt_gb is not None:
                        _rt_gb_ok = _rt_gb
                        _gb_val = _enc_rs(new_token)
                        _gb_done = _xw_gb.submit_if_lib(
                            _rt_gb_ok, "runtime", "repo_credential_upsert",
                            f"repo-cred:{repo_key}:{_ct}",
                            {"repo_key": repo_key, "cred_type": _ct, "value": _gb_val,
                             "updated_at": _gb_ts},
                            lambda ct=_ct, v=_gb_val: _rt_gb_ok.upsert_repo_credential(
                                repo_key, ct, v, _gb_ts), log)
                    if _gb_done:
                        continue
                    # 走 owner 域原语（任意拓扑）：自有 DSN → insight 连接；单库/测试 →
                    # 进程主库。兜底 SQL 若留在本文件，三库拓扑下 mgmt 库无该表必 1146，
                    # 静默跳过整个写块（换发后凭据仍是旧 token）。
                    _pending_creds.append((_ct, _enc_rs(new_token)))
                if _pending_creds:
                    # bot/webhook 两行同一事务：避免「bot 已换、webhook 仍旧」的半写状态
                    from src.domains.insight import public as _insight_cred_w
                    _insight_cred_w.upsert_repo_credentials(repo_key, _pending_creds, _gb_ts)
                db.commit()
        except Exception as e:
            log.warning(f"global_bot_token_swap: DB update failed for {repo_key}: {e}")
            continue

        # 步骤 7: best-effort 移除旧 bot
        if old_bot_username and old_bot_username != new_bot_username:
            try:
                _vcs.remove_collaborator(new_token, owner, repo_name, old_bot_username)
            except Exception as e:
                log.warning(f"global_bot_token_swap: remove old bot failed for {repo_key} (best-effort): {e}")

    log.info(f"global_bot_token_swap: completed for {platform_label}")
