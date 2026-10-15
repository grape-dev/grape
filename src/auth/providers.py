"""第三方平台 Provider 注册表 + 统一 OAuth 流程。

标准 OAuth 2.0 平台：通过配置文件（global-bot-token.json oauth_providers 字段）
接入，无需改代码。配置文件 → meta 表 → 内存缓存。

非标准平台：通过 OAuthProvider 子类扩展。

平台配置包含：平台名称、OAuth 端点、授权范围、字段映射、pkce_enabled、
whitelist_column、accept_header。
"""
import hashlib
import os
import secrets
import time
import urllib.parse
import urllib.request
import json
from typing import Protocol

from src.config import log
from src.session_manager import db, db_lock
from src.user import db as user_db


# ---------------------------------------------------------------------------
# Provider 注册表
# ---------------------------------------------------------------------------

# 代码内置默认——和生产环境当前行为一致，确保旧配置文件向前兼容。
# 当 meta 表无 oauth_providers_config 时（旧文件未更新），使用此默认。
_DEFAULT_PROVIDERS: dict[str, dict] = {
    "gitcode": {
        "name": "GitCode",
        "auth_url": "https://gitcode.com/oauth/authorize",
        "token_url": "https://gitcode.com/oauth/token",
        "user_url": "https://gitcode.com/api/v5/user",
        "scope": "all",
        "id_field": "id",
        "login_field": "login",
        "name_field": "nickname",
        "pkce_enabled": True,
        "brand_color": "#e7330f",
        "whitelist_column": "gitcode_username",
        "accept_header": "application/json",
    },
    "github": {
        "name": "GitHub",
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "user_url": "https://api.github.com/user",
        # D46 P1-12: 缩减 scope——仅需读取用户信息和邮箱，不需要 repo 写权限
        "scope": "read:user user:email",
        "id_field": "id",
        "login_field": "login",
        "name_field": "name",
        "pkce_enabled": True,
        "brand_color": "#24292f",
        "whitelist_column": "github_username",
        "accept_header": "application/vnd.github+json",
    },
}

# 运行时内存缓存——从 meta 表加载，无则用代码内置默认
_PROVIDERS_CACHE: dict[str, dict] = {}
_loaded: bool = False
_cache_ts: float = 0.0          # P2-2: 上次加载时间戳
_CACHE_TTL: float = 300.0       # P2-2: 缓存 TTL 5 分钟（兜底，admin 改配置时主动失效）
_providers_lock = __import__("threading").Lock()


def _load_providers():
    """从 meta 表加载 provider 配置，无则用代码内置默认。

    P2-10: 加 threading.Lock 保护初始化，防止多线程并发加载。
    - meta 表有 oauth_providers_config（配置文件已同步）→ 从 DB 读取
    - meta 表无配置（旧文件未更新）→ 用代码内置默认，向前兼容
    P2-2: _loaded 改为带 TTL 的缓存时间戳，admin 改配置后调 invalidate_provider_cache() 热生效。
    BUG-15 修复：将加载逻辑移入锁内，防止两线程同时加载和修改 _PROVIDERS_CACHE。
    """
    global _loaded, _cache_ts
    if _loaded and (time.time() - _cache_ts < _CACHE_TTL):
        return
    with _providers_lock:
        # 双重检查：进入锁后再检查一次
        if _loaded and (time.time() - _cache_ts < _CACHE_TTL):
            return
        raw = user_db.get_meta_value("oauth_providers_config") or ""
        if raw:
            try:
                data = json.loads(raw)
                _PROVIDERS_CACHE.clear()
                _PROVIDERS_CACHE.update(data)
                log.info(f"Loaded {len(_PROVIDERS_CACHE)} OAuth providers from DB: {list(_PROVIDERS_CACHE.keys())}")
                _loaded = True
                _cache_ts = time.time()
                return
            except Exception as e:
                log.warning(f"Failed to parse oauth_providers_config from DB: {e}, using defaults")

        # meta 表无配置 → 用代码内置默认
        _PROVIDERS_CACHE.clear()
        _PROVIDERS_CACHE.update(_DEFAULT_PROVIDERS)
        log.info(f"Using built-in default providers: {list(_PROVIDERS_CACHE.keys())}")
        _loaded = True
        _cache_ts = time.time()


def invalidate_provider_cache():
    """P2-2: 主动失效 Provider 缓存——admin 改 OAuth 配置后调用，无需重启服务。"""
    global _loaded
    with _providers_lock:
        _loaded = False
    log.info("OAuth provider cache invalidated")


def get_provider(provider: str) -> "dict | None":
    """获取 Provider 配置。"""
    if not _loaded:
        _load_providers()
    return _PROVIDERS_CACHE.get(provider)


def list_providers() -> list[str]:
    """列出所有已配置的 Provider。"""
    if not _loaded:
        _load_providers()
    return list(_PROVIDERS_CACHE.keys())


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def generate_pkce() -> tuple[str, str]:
    """生成 PKCE code_verifier 和 code_challenge（S256）。

    返回 (code_verifier, code_challenge)。
    """
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = base64url_encode(verifier_bytes)
    challenge_bytes = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64url_encode(challenge_bytes)
    return code_verifier, code_challenge


def base64url_encode(data: bytes) -> str:
    """Base64 URL-safe 编码（无 padding）。"""
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# OAuth State 管理（复用现有 oauth_states 表）
# ---------------------------------------------------------------------------

def save_oauth_state(state: str, provider: str, data: "dict | None" = None,
                     pkce_verifier: str = "") -> None:
    """保存 OAuth state（含 PKCE code_verifier）。

    复用现有 oauth_states 表，data_json 中存储 PKCE verifier。
    """
    state_data = data or {}
    if pkce_verifier:
        state_data["pkce_verifier"] = pkce_verifier
    user_db.save_oauth_state(state, provider, state_data)


def validate_oauth_state(state: str, provider: str) -> "dict | None":
    """验证并消费 OAuth state，返回 data（含 PKCE verifier）或 None。"""
    return user_db.validate_oauth_state(state, provider)


# ---------------------------------------------------------------------------
# 统一授权 URL 构造
# ---------------------------------------------------------------------------

def build_auth_url(provider: str, state: str, redirect_uri: str,
                   extra_state_data: "dict | None" = None,
                   prompt: str = "") -> "dict | None":
    """构造 OAuth 授权 URL。

    返回 dict:
    - ``{"redirect_url": "...", "code_verifier": "..."}`` — 成功
    - ``None`` — Provider 不存在

    extra_state_data: 调用方额外存入 state 的数据（如 binding_user_id / login_flow），
    与 pkce_verifier 一起保存，避免双重 save_oauth_state 覆盖。
    prompt: OAuth prompt 参数（如 "login" 强制重新认证 / "consent" 强制确认授权）。
    用于已绑定场景切换账号时避免复用浏览器已有会话。
    """
    cfg = get_provider(provider)
    if not cfg:
        return None

    # PKCE
    code_verifier = ""
    code_challenge = ""
    if cfg.get("pkce_enabled"):
        code_verifier, code_challenge = generate_pkce()

    # 保存 state（合并 pkce_verifier、redirect_uri 和 extra_state_data，一次写入）
    # 加固项 §10.6：redirect_uri 存入 state，回调时从 state 恢复而非重新构造，
    # 消除发起与回调之间配置被改的竞态（RFC 6749 要求两次 redirect_uri 一致）
    state_data = dict(extra_state_data or {})
    if code_verifier:
        state_data["pkce_verifier"] = code_verifier
    state_data["redirect_uri"] = redirect_uri
    user_db.save_oauth_state(state, provider, state_data)

    params = {
        "client_id": _get_client_id(provider),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    scope = cfg.get("scope", "")
    if scope:
        params["scope"] = scope
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if prompt:
        params["prompt"] = prompt

    auth_url = f"{cfg['auth_url']}?{urllib.parse.urlencode(params)}"
    return {"redirect_url": auth_url, "code_verifier": code_verifier}


# ---------------------------------------------------------------------------
# Token 交换 + 用户信息获取
# ---------------------------------------------------------------------------

def exchange_token(provider: str, code: str, redirect_uri: str,
                   code_verifier: str = "") -> "tuple[dict | None, str]":
    """用 code 换 access_token。

    P2-3: 统一用 requests 替代 urllib.request，与 get_user_info 保持一致。
    返回 (token_dict, error_type) tuple：
    - 成功：(token_dict, "")
    - 失败：(None, "network_timeout") 或 (None, "token_exchange_failed")
    - Provider 不存在/无 client_id：(None, "not_configured")

    超时策略：连接超时 5s，读超时 10s，重试 2 次，指数退避（1s, 2s）。
    最坏情况约 5+10+1+5+10+2 = 33s（比原来 63s 快一倍）。
    """
    cfg = get_provider(provider)
    if not cfg:
        return None, "not_configured"

    client_id = _get_client_id(provider)
    client_secret = _get_client_secret(provider)
    if not client_id:
        return None, "not_configured"

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    try:
        import requests as _req
        _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        _proxies = {"https": _proxy, "http": _proxy} if _proxy else None
        # 境外平台网络不稳定，重试 2 次（共 3 次尝试），指数退避
        last_err_type = "token_exchange_failed"
        for _attempt in range(3):
            try:
                resp = _req.post(
                    cfg["token_url"],
                    data=data,
                    headers={"Accept": "application/json"},
                    timeout=(5, 10),  # (connect_timeout, read_timeout)
                    proxies=_proxies,
                )
                resp.raise_for_status()
                return resp.json(), ""
            except _req.exceptions.ConnectTimeout:
                last_err_type = "network_timeout"
                if _attempt < 2:
                    import time as _t
                    _t.sleep(1 * (2 ** _attempt))  # 1s, 2s
            except _req.exceptions.ReadTimeout:
                last_err_type = "network_timeout"
                if _attempt < 2:
                    import time as _t
                    _t.sleep(1 * (2 ** _attempt))
            except Exception as e:
                last_err_type = "token_exchange_failed"
                if _attempt < 2:
                    import time as _t
                    _t.sleep(1 * (2 ** _attempt))
        log.warning(f"Exchange token for {provider} failed after 3 retries: {last_err_type}")
        return None, last_err_type
    except Exception as e:
        log.warning(f"Exchange token for {provider} failed: {e}")
        return None, "token_exchange_failed"


def get_user_info(provider: str, access_token: str) -> "dict | None":
    """获取第三方平台用户信息。

    返回标准化 dict：
    - external_id: 平台用户 ID
    - external_login: 平台用户名
    - external_name: 平台昵称
    - external_avatar: 平台头像
    - access_token: OAuth access_token
    - refresh_token: OAuth refresh_token
    - email: 平台返回的邮箱（可能为空）
    - email_verified: 平台邮箱是否已验证
    """
    cfg = get_provider(provider)
    if not cfg:
        return None

    try:
        import requests as _req
        # 统一用配置里的 accept_header，消除 provider if-else 分叉
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": cfg.get("accept_header", "application/json"),
        }

        _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        _proxies = {"https": _proxy, "http": _proxy} if _proxy else None

        resp = _req.get(cfg["user_url"], headers=headers, timeout=15, proxies=_proxies)
        resp.raise_for_status()
        user_info = resp.json()
    except Exception as e:
        log.warning(f"Get user info for {provider} failed: {e}")
        return None

    result = {
        "external_id": str(user_info.get(cfg["id_field"], "")),
        "external_login": user_info.get(cfg["login_field"], ""),
        "external_name": user_info.get(cfg["name_field"], ""),
        "external_avatar": user_info.get("avatar_url", ""),
        "access_token": access_token,
        "refresh_token": "",
        "email": user_info.get("email", "") or "",
        "email_verified": bool(user_info.get("email_verified", False)),
    }

    # P2-2: GitHub /user 接口默认不返回 email（用户设了私密），
    # 需额外调 /user/emails 接口获取主邮箱。
    if provider == "github" and not result["email"]:
        try:
            emails_resp = _req.get(
                "https://api.github.com/user/emails",
                headers=headers, timeout=15, proxies=_proxies,
            )
            emails_resp.raise_for_status()
            emails = emails_resp.json()
            # 取 primary + verified 的邮箱
            for e in emails:
                if e.get("primary") and e.get("verified"):
                    result["email"] = e.get("email", "")
                    result["email_verified"] = True
                    break
        except Exception as e:
            log.warning(f"Get GitHub emails for {result.get('external_login')} failed: {e}")

    return result


# ---------------------------------------------------------------------------
# Client ID/Secret 读取
# ---------------------------------------------------------------------------

def _get_client_id(provider: str) -> str:
    """获取 OAuth client_id——统一用 provider 名拼 meta key。"""
    return user_db.get_meta_value(f"oauth_{provider}_client_id") or ""


def _get_client_secret(provider: str) -> str:
    """获取 OAuth client_secret——统一用 provider 名拼 meta key。"""
    return user_db.get_meta_value(f"oauth_{provider}_client_secret") or ""


def get_redirect_uri(request, provider: str = "") -> str:
    """获取 OAuth 回调 URI。

    需求 11：从 provider 配置的 redirect_uri 字段读取（完整地址，含路径）。
    需求 3：路径为 /api/auth/callback/{provider}。
    开发环境 fallback：provider 未配 redirect_uri 时从 Host header 构造。
    """
    # 优先从 provider 配置读取
    if provider:
        cfg = get_provider(provider)
        if cfg and cfg.get("redirect_uri"):
            return cfg["redirect_uri"]

    # fallback：从 Host header 构造（开发环境）
    # provider 为空意味着调用方未传 provider，属于编程错误——统一回调路由已删除
    if not provider:
        raise ValueError("get_redirect_uri requires a non-empty provider after callback route split")
    path = f"/api/auth/callback/{provider}"
    # 生产环境强制 https，防止 X-Forwarded-Proto 伪造
    from src.auth.security_config import is_production
    if is_production():
        scheme = "https"
    else:
        scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = _get_trusted_host(request)
    return f"{scheme}://{host}{path}"


def _get_trusted_host(request) -> str:
    """获取可信 Host（BUG-1 修复）。

    优先级：
    1. TRUSTED_HOSTS 环境变量白名单校验
    2. 回退到 Host header（开发环境兼容）
    """
    raw_host = request.headers.get("Host", "localhost:80")

    # 校验 Host header 是否在可信白名单中
    trusted_hosts = os.environ.get("TRUSTED_HOSTS", "").strip()
    if trusted_hosts:
        hosts = [h.strip().lower() for h in trusted_hosts.split(",") if h.strip()]
        if raw_host.lower() in hosts:
            return raw_host
        # 不在白名单 → 回退到第一个可信 Host（防止开放重定向）
        # P2-12: 过滤换行符防日志注入
        safe_host = raw_host.replace("\n", "").replace("\r", "")
        log.warning(f"Host header '{safe_host}' not in TRUSTED_HOSTS, using fallback '{hosts[0]}'")
        return hosts[0]

    # 未设置 TRUSTED_HOSTS → 保持原有行为（开发环境兼容）
    return raw_host
