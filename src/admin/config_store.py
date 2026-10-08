"""global-bot-token.json 读写 + meta 表同步。

文件是**唯一权威源**（bot token / bot_usernames 运行时不走 meta，只读文件），
meta 表仅承载 OAuth 配置（auth.py 走 get_meta_value）。保存时先写文件再 sync meta。
"""
import json
import os

from src.config import log, CONFIG_DIR

_GBT_PATH = os.path.join(CONFIG_DIR, "global-bot-token.json")

# 全字段清单（文件字典的 key）
ALL_FIELDS = [
    "gitcode_api_token",
    "gitcode_oauth_client_id",
    "gitcode_oauth_client_secret",
    "gitcode_oauth_redirect_uri",
    "github_bot_token",
    "github_oauth_client_id",
    "github_oauth_client_secret",
    "github_oauth_redirect_uri",
    "bot_usernames",
]

# 同步到 meta 表的映射：文件 key -> meta key（仅 OAuth 部分走 meta）
_META_SYNC_MAP = {
    "gitcode_oauth_client_id": "oauth_gitcode_client_id",
    "gitcode_oauth_client_secret": "oauth_gitcode_client_secret",
    "gitcode_oauth_redirect_uri": "oauth_gitcode_redirect_uri",
    "github_oauth_client_id": "oauth_github_client_id",
    "github_oauth_client_secret": "oauth_github_client_secret",
    "github_oauth_redirect_uri": "oauth_github_redirect_uri",
}


def get_config_path() -> str:
    return _GBT_PATH


def load_config() -> dict:
    """读取 global-bot-token.json；不存在或损坏返回空 dict。"""
    try:
        if os.path.exists(_GBT_PATH):
            with open(_GBT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"admin config_store: load failed: {e}")
    return {}


def read_public_config() -> dict:
    """返回脱敏配置（前端展示）：token/secret 只显示是否存在 + 尾 4 位。

    OAuth 字段优先读 meta 表（运行时权威，可能是旧版只配 meta 未落盘文件），
    文件里的 bot token / bot_usernames 为兜底。
    """
    from src.user.db import get_meta_value
    data = load_config()
    # meta 表 OAuth 值覆盖文件（运行时 OAuth 走 meta）
    _merged = dict(data)
    for src_key, meta_key in _META_SYNC_MAP.items():
        _mv = get_meta_value(meta_key)
        if _mv:
            _merged[src_key] = _mv
    out = {}
    for field in ALL_FIELDS:
        val = _merged.get(field, "")
        if isinstance(val, list):
            out[field] = val
        elif isinstance(val, str) and val:
            tail = val[-4:] if len(val) >= 4 else val
            out["{}_present".format(field)] = val != ""
            out["{}_tail".format(field)] = tail
    # oauth_status 汇总（前端直接展示三态）
    out["oauth_status"] = {
        "gitcode": bool(get_meta_value("oauth_gitcode_client_id")),
        "github": bool(get_meta_value("oauth_github_client_id")),
    }
    return out


def save_config(payload: dict) -> dict:
    """保存配置到 global-bot-token.json + sync meta 表。

    只更新 payload 中给出的字段；未给出的字段保留原值（前端脱敏读、增量写）。
    返回落盘后的完整配置（脱敏）。
    """
    data = load_config()
    for field in ALL_FIELDS:
        if field in payload:
            data[field] = payload[field]
    # 落盘：原子写（先写临时文件再 rename）
    tmp = _GBT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _GBT_PATH)
    log.info("admin config_store: global-bot-token.json updated")
    # 同步 OAuth 到 meta 表（auth.py 走 meta）
    from src.user.db import set_meta_value
    for src_key, meta_key in _META_SYNC_MAP.items():
        val = data.get(src_key, "")
        if val:
            set_meta_value(meta_key, val)
    return read_public_config()


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