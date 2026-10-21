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

from src.config import log, CONFIG_DIR

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

# OAuth 扁平字段 → oauth-providers.json provider 条目映射
# flat_field: (provider, cred_field)
_OAUTH_FIELD_MAP = {
    "gitcode_oauth_client_id": ("gitcode", "client_id"),
    "gitcode_oauth_client_secret": ("gitcode", "client_secret"),
    "github_oauth_client_id": ("github", "client_id"),
    "github_oauth_client_secret": ("github", "client_secret"),
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
    from src.user.db import get_meta_value
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
    from src.auth.providers import list_providers
    out["oauth_status"] = {
        p: bool(get_meta_value(f"oauth_{p}_client_id"))
        for p in list_providers()
    }
    # allow_signup — 归一化为 "1"/"0"（兼容 meta 中可能存在的 "true"/"yes" 旧值）
    from src.auth.signup_policy import _is_allow_signup
    out["allow_signup"] = "1" if _is_allow_signup() else "0"
    return out


def save_config(payload: dict) -> dict:
    """保存配置——分流到 DB meta + oauth-providers.json。

    Issue #345: Bot 字段写 DB meta 表（唯一权威源），不写文件。
    OAuth 字段写 oauth-providers.json + 同步 meta 表。

    只更新 payload 中给出的字段；未给出的字段保留原值。
    返回落盘后的完整配置（脱敏）。
    """
    # 1. Bot 字段 → DB meta（唯一权威源）
    # Issue #345: 集群部署下 DB 是唯一权威源，不再写文件。
    # global-bot-token.json 保留只读，仅供启动迁移读取（迁移后不再被运行时访问）。
    bot_payload = {k: v for k, v in payload.items() if k in _BOT_FIELDS}
    if bot_payload:
        from src.user.db import set_meta_value, get_meta_value
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
        from src.user.db import db_lock
        _upgrade_triggers = []  # [(provider, new_token, bot_username)] — 从无到有
        _swap_triggers = []     # [(provider, old_token, new_token)] — 有→有换账号
        with db_lock:
            for field, meta_key in _BOT_META_MAP.items():
                if field in bot_payload:
                    val = bot_payload[field]
                    # 检测从无到有 / 有→有换账号
                    if field in ("gitcode_api_token", "github_bot_token") and val:
                        _old = get_meta_value(meta_key) or ""
                        provider = "github" if field == "github_bot_token" else "gitcode"
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
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
        # R16.3.2: 清除全局 bot token 过期标记（per-provider, R21.2）
        # 管理员更新 bot token 时清除对应 provider 的过期状态，短路自动解除
        from src.user.db import set_meta_value
        if "gitcode_api_token" in bot_payload:
            set_meta_value("global_gitcode_bot_token_status", "")
        if "github_bot_token" in bot_payload:
            set_meta_value("global_github_bot_token_status", "")
        log.info("admin config_store: bot config updated (DB)")

        # Issue #372 §13.2: bot token 从无到有时异步升级路径 B 仓库
        if _upgrade_triggers:
            from src.user.db import get_global_bot_usernames, get_global_github_bot_username
            import threading
            for _provider, _token in _upgrade_triggers:
                _bot_username = ""
                if _provider == "github":
                    _bot_username = get_global_github_bot_username()
                else:
                    _us = get_global_bot_usernames()
                    _bot_username = _us[0] if _us else ""
                if _bot_username:
                    from src.user.db import auto_upgrade_path_b_repos
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
        from src.user.db import set_meta_value
        for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
            if flat_field in oauth_payload:
                val = oauth_payload[flat_field]
                if val:
                    set_meta_value(f"oauth_{provider}_{cred_field}", val)
        log.info("admin config_store: oauth-providers.json + meta updated")
        # P2-2: 失效 Provider 内存缓存，下次请求时从 DB 重新加载
        from src.auth.providers import invalidate_provider_cache
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

    - provider=gitcode:  GET https://gitcode.com/api/v5/user
    - provider=github:   GET https://api.github.com/user
    """
    import os as _os
    _proxies = None
    _proxy = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy") or ""
    if _proxy:
        _proxies = {"https": _proxy, "http": _proxy}
    if provider == "github":
        from src.user.repo_manager import _github_req
        user_data = _github_req("GET", "https://api.github.com/user", token)
        login = user_data.get("login", "")
        return {"provider": "github", "login": login, "name": user_data.get("name") or login}
    from src.user.repo_manager import _req
    user_data = _req("GET", "https://gitcode.com/api/v5/user", token)
    login = user_data.get("login", "") or ""
    return {"provider": "gitcode", "login": login, "name": user_data.get("name") or login}


# ---------------------------------------------------------------------------
# R21.1: 全局 bot token 更换（有→有换账号）
# ---------------------------------------------------------------------------

def _global_bot_token_swap_wrapper(provider: str, old_token: str, new_token: str):
    """R21.1.3/R21.1.7: 后台线程 wrapper——加分布式锁 + 异常隔离。"""
    try:
        from src.lock import acquire_lock, release_lock
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
    3. 遍历路径 A 仓库（host_user_id==0 且 bot_gitcode_token==旧 token / bot_github_token==旧 token）
    4. 邀新 bot 为协作者（如果新 bot 与旧 bot_username 不同）
    5. 重建 webhook（用新 token 删旧 + 建新，password=新 token）
    6. 写 DB（更新 webhook_token + bot_username）
    7. best-effort 移除旧 bot 协作者身份

    R21.1.4: 失败时仓库保持旧状态，记录 warning。
    R21.1.5: 过期标记已在 save_config 中清除（R16.3.2）。
    """
    from src.token_resolver import create_provider
    from src.session_manager import db, db_lock
    from src.user.db import get_webhook_url, _get_managed_webhook_id
    from src.repo_key import split_owner_repo

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

    # 步骤 3: 遍历路径 A 仓库
    token_field = "bot_github_token" if provider == "github" else "bot_gitcode_token"
    webhook_token_field = "github_webhook_token" if provider == "github" else "gitcode_webhook_token"
    repos = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT repo_key, bot_username FROM managed_repos "
                f"WHERE host_user_id=0 AND {token_field}=?",
                (old_token,),
            )
            for row in c.fetchall():
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
    if provider == "github":
        _wh_url = _wh_url.replace("/webhook/gitcode", "/webhook/github")

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
                _permission = "push" if provider == "github" else "maintainer"
                _vcs.add_collaborator(new_token, owner, repo_name, new_bot_username, _permission)
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
                    f"UPDATE managed_repos SET {token_field}=?, {webhook_token_field}=?, "
                    f"webhook_id=?, bot_username=? WHERE repo_key=?",
                    (new_token, new_token, str(_new_hook_id), new_bot_username, repo_key),
                )
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
