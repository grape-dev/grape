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
        from src.user.db import set_meta_value
        import json as _json
        _BOT_META_MAP = {
            "gitcode_api_token": "global_bot_gitcode_token",
            "github_bot_token": "global_bot_github_token",
            "github_bot_username": "global_bot_github_username",
            "gitcode_webhook_token": "gitcode_webhook_token",
            "webhook_url": "webhook_url",
            "bot_usernames": "global_bot_usernames",
        }
        for field, meta_key in _BOT_META_MAP.items():
            if field in bot_payload:
                val = bot_payload[field]
                if isinstance(val, list):
                    set_meta_value(meta_key, _json.dumps(val))
                else:
                    set_meta_value(meta_key, str(val) if val else "")
        # Issue #345: 清除本实例缓存（当前实例立即生效，其他实例最多 60s TTL 后生效）
        from src.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
        log.info("admin config_store: bot config updated (DB)")

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
    login = user_data.get("login", "")
    return {"provider": "gitcode", "login": login, "name": user_data.get("name") or login}
