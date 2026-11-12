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

_GBT_PATH = os.path.join(CONFIG_DIR, "global-bot-token.json")

_OAUTH_PATH = os.path.join(CONFIG_DIR, "oauth-providers.json")

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
# src/config.py 统一 getter 读取（env > meta > runtime-config.json > 默认）。
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
    return _GBT_PATH

def load_config() -> dict:
    """Read global-bot-token.json; return empty dict if missing or corrupt.

    Note: Used only by startup migration (_migrate_global_bot_token_to_db).
    Runtime code reads from DB meta table via unified entry functions.
    """
    try:
        if os.path.exists(_GBT_PATH):
            with open(_GBT_PATH, "r", encoding="utf-8") as f:
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
    # OAuth 字段从 meta 表读取
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        meta_key = f"oauth_{provider}_{cred_field}"
        val = get_meta_value(meta_key) or ""
        if val:
            out[f"{flat_field}_present"] = True
            out[f"{flat_field}_tail"] = val[-4:] if len(val) >= 4 else val
            # client_id 非敏感，回显明文便于前端回填
            if cred_field == "client_id":
                out[flat_field] = val
    # oauth_status（动态从 provider 列表生成，不硬编码平台名）
    from src.domains.mgmt.auth.providers import list_providers
    out["oauth_status"] = {
        p: bool(get_meta_value(f"oauth_{p}_client_id"))
        for p in list_providers()
    }
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

    # 2. OAuth 字段 → oauth-providers.json + 同步 meta 表
    oauth_payload = {k: v for k, v in payload.items() if k in _OAUTH_FIELD_MAP}
    if oauth_payload:
        _save_oauth_providers(oauth_payload)
        # 同步 meta 表
        from src.domains.mgmt.user.db import set_meta_value
        for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
            if flat_field in oauth_payload:
                val = oauth_payload[flat_field]
                if val:
                    set_meta_value(f"oauth_{provider}_{cred_field}", val)
        log.info("admin config_store: oauth-providers.json + meta updated")
        # P2-2: 失效 Provider 内存缓存，下次请求时从 DB 重新加载
        from src.domains.mgmt.auth.providers import invalidate_provider_cache
        invalidate_provider_cache()

    return read_public_config()

def _save_oauth_providers(oauth_payload: dict):
    """把 ConfigBody 扁平 OAuth 字段写入 oauth-providers.json（嵌套结构）。

    读取现有 oauth-providers.json，合并新字段，原子写回。
    """
    existing = {}
    if os.path.exists(_OAUTH_PATH):
        try:
            with open(_OAUTH_PATH, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            pass
    # 扁平 → 嵌套
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        if flat_field in oauth_payload:
            if provider not in existing:
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
            c.execute(
                "SELECT rc.repo_key, mr.bot_username, rc.value FROM grape_t_repo_credentials rc "
                "JOIN managed_repos mr ON mr.repo_key = rc.repo_key "
                "WHERE rc.cred_type='bot' "
                "AND (mr.host_user_id IS NULL OR mr.host_user_id='' OR mr.host_user_id='0')",
            )
            for row in c.fetchall():
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
                for _ct in ("bot", "webhook"):
                    _cur2 = c.execute(
                        "UPDATE grape_t_repo_credentials SET value=?, updated_at=(datetime('now')) "
                        "WHERE repo_key=? AND cred_type=?",
                        (_enc_rs(new_token), repo_key, _ct))
                    if _cur2.rowcount == 0:
                        c.execute(
                            "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
                            "VALUES (?, ?, ?, ?)",
                            (str(__import__("uuid").uuid4()), repo_key, _ct, _enc_rs(new_token)))
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
