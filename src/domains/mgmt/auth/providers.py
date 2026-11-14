"""第三方平台 Provider 注册表 + 统一 OAuth 流程。

标准 OAuth 2.0 平台：通过配置文件（global-bot-token.json oauth_providers 字段）
接入，无需改代码。配置文件 → meta 表 → 内存缓存。

非标准平台：通过 OAuthProvider 子类扩展。

平台配置包含：平台名称、OAuth 端点、授权范围、字段映射、pkce_enabled、
accept_header。

白名单列名声明字段（whitelist_column）已随 2026-11-12 行式化改造退役：
login_whitelist 同表加 provider/username 行式两列，登录校验直接按渠道
key 查行（is_user_allowed(provider, login)），卡不再声明白名单元数据。
"""

import hashlib

import os

import secrets

import time

import urllib.parse

import urllib.request

import json

from typing import Protocol

from src.kernel.config import log

from src.kernel.session_manager import db, db_lock

from src.domains.mgmt.user import db as user_db

# 渠道卡真源 = configs/oauth-providers.json（一份聚合文件，与仓库侧的
# configs/platforms/<provider>.json 同一纪律）。代码里不再持有任何平台卡字面量——
# 加渠道/换端点 = 加一个文件，零代码、零发版（多平台解耦在登录渠道侧的收口）。
# 优先级：meta.oauth_providers_config（运行时覆盖，逐键合并）> 文件卡 > 无（fail-closed）。
# 登录渠道配置真源 = configs/oauth-providers.json（一份聚合文件，全部渠道）。
# 与仓库托管配置（configs/platforms/*.json，每平台一文件）物理分离、字段互不渗透：
# 这份文件只放 OAuth 渠道卡，不放托管能力位；那份只放托管卡，不放 auth/scope。
_AUTH_CHANNELS_FILE_NAME = "oauth-providers.json"

# 渠道卡合法键集（与消费面一一对应：build_auth_url / exchange_token /
# get_user_info / get_redirect_uri / 渠道策略构建校验 / OIDC claims_map）。
# 未知键整卡拒绝——静默丢键 = 能力静默丢失（S69 反模式）。
_CHANNEL_CARD_FIELDS = {
    "name", "type", "channel_type",
    "auth_url", "token_url", "user_url",
    "scope", "id_field", "login_field", "name_field", "email_field",
    "avatar_field", "pkce_enabled", "brand_color", "accept_header",
    "emails_endpoint", "claims_map",
    # 环境值但可选：留空则由 server_base_url 派生 /api/auth/callback/<provider>；
    # 显式写必须与平台后台登记的回调地址逐字节一致（get_redirect_uri 优先读它）。
    "redirect_uri",
    # legacy：白名单列名声明字段已于 2026-11-12 行式化改造退役（login_whitelist 改为
    # provider/username 行式两列，卡不再声明列名）。仍允许出现：旧卡/旧文件里带着它，
    # 整卡拒绝会让存量配置直接失效；加载后代码不再读取该键。
    "whitelist_column",
    # 渠道自足字段：登录页/绑定弹窗用（PAT 创建页直达链接）。缺失时回落平台卡的
    # pat_create_url（过渡兼容），渠道卡显式声明后登录域不再依赖托管配置。
    "pat_create_url",
    "idp_sso_url", "attribute_map",     # SAML 类协议预留（由渠道策略消费）
}
_REQUIRED_CHANNEL_CARD_FIELDS = ("auth_url", "token_url", "user_url")

def _repo_root(start: str) -> str:
    """向上找到含 configs/ 的仓库根（与 platform_card._repo_root 同构）。"""
    cur = os.path.dirname(os.path.abspath(start))
    while cur and cur != "/":
        if os.path.isdir(os.path.join(cur, "configs")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(os.path.abspath(start))

_AUTH_CHANNELS_FILE = os.path.join(_repo_root(__file__), "configs", _AUTH_CHANNELS_FILE_NAME)

# P2-7 缓存：渠道展示序（键=文件 mtime+size）
_order_cache_key = None
_order_cache_idx: "list[str] | None" = None

def _builtin_channels_from_file(path: str) -> "dict[str, dict]":
    """configs/oauth-providers.json → 渠道卡 raw（登录渠道的配置真源）。

    守卫（与 configs/platforms 同纪律，构建即校验）：
    - 顶层必须是 {provider: card} 对象；
    - 每张卡未知键 → 整卡拒绝并 error 级响亮（拼错键=能力静默丢失）；
    - 缺 auth_url/token_url/user_url → 整卡拒绝；
    - 凭据不进文件：client_id/client_secret 若出现则剥离并告警（唯一落点=DB meta/管理面）。
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        log.error(f"oauth-providers.json 解析失败 {path}: {e}（无渠道可用）")
        return {}
    if not isinstance(data, dict):
        log.error(f"oauth-providers.json 顶层必须是 {{provider: card}} 对象（实际 {type(data).__name__}）")
        return {}
    raws: dict[str, dict] = {}
    for provider, raw in data.items():
        provider = str(provider or "").strip()
        # 约定：下划线前缀 = 文档/注释键（JSON 无注释，用 "_readme"/"_note" 承载说明），
        # 不是渠道名。这样 example 文件可以写满说明，复制成真文件也不会被当成渠道。
        if not provider or provider.startswith("_"):
            continue
        if not isinstance(raw, dict):
            log.error(f"oauth-providers.json: {provider!r} 卡不是 JSON 对象（整卡拒绝）")
            continue
        _creds = [k for k in ("client_id", "client_secret") if k in raw]
        card = {k: v for k, v in raw.items()
                if k not in ("client_id", "client_secret") and not str(k).startswith("_")}
        if _creds:
            log.warning(f"oauth-providers.json: {provider} 卡内出现 {_creds} —— 凭据不进配置文件，"
                        f"已剥离；请在管理面「登录渠道」或 meta oauth_{provider}_client_id/secret 配置")
        unknown = set(card) - _CHANNEL_CARD_FIELDS
        if unknown:
            log.error(f"oauth-providers.json: {provider} 含未知键 {sorted(unknown)}"
                      f"（合法键={sorted(_CHANNEL_CARD_FIELDS)}），整卡拒绝")
            continue
        missing = [k for k in _REQUIRED_CHANNEL_CARD_FIELDS if not str(card.get(k) or "").strip()]
        if missing:
            log.error(f"oauth-providers.json: {provider} 缺必填端点 {missing}，整卡拒绝")
            continue
        raws[provider] = card
    return raws

def builtin_channels() -> "dict[str, dict]":
    """登录渠道卡文件真源（管理面保存基线 / 测试口径的唯一入口；不含任何凭据）。"""
    return _builtin_channels_from_file(_AUTH_CHANNELS_FILE)

# 运行时内存缓存——文件卡 + meta 运行时覆盖
_PROVIDERS_CACHE: dict[str, dict] = {}

_loaded: bool = False

_cache_ts: float = 0.0          # P2-2: 上次加载时间戳

_CACHE_TTL: float = 300.0       # P2-2: 缓存 TTL 5 分钟（兜底，admin 改配置时主动失效）

_providers_lock = __import__("threading").Lock()

_CHANNEL_TABLE_FIELDS = (
    "name", "type", "auth_url", "token_url", "user_url", "emails_endpoint", "scope",
    "id_field", "login_field", "name_field", "avatar_field", "redirect_uri",
    "brand_color", "accept_header",
)


def _channels_from_table() -> dict:
    """D109：表就绪后从 grape_t_oauth_channels 读渠道卡（运行时真源）。

    未就绪 / 读失败 → 返回空 dict，调用方保持原有「文件 + meta」路径（§16 #1 回落）。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return {}
        import json as _json
        from sqlalchemy.orm import Session
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        out: dict[str, dict] = {}
        with Session(get_engine()) as s:
            for row in OAuthChannelRepo.list_all(s, enabled_only=True):
                # 防御：导入器已跳过下划线文档键，但历史库可能留有脏行。
                # 注意不能写 `not row.provider`——ORM 列在类型层是 Column[str]，
                # 布尔求值会触发 Pyright reportGeneralTypeIssues。
                _prov = str(row.provider) if row.provider is not None else ""
                _prov = _prov.strip()
                if not _prov or _prov.startswith("_"):
                    continue
                card = {f: getattr(row, f) or "" for f in _CHANNEL_TABLE_FIELDS}
                card["pkce_enabled"] = bool(row.pkce_enabled)
                try:
                    card["repo_links"] = _json.loads(row.repo_links_json or "[]")
                except Exception:
                    card["repo_links"] = []
                out[row.provider] = card
        return out
    except Exception as e:
        log.warning(f"oauth_channels table read failed, fallback to file+meta: {e}")
        return {}

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
        # 真源：configs/oauth-providers.json（一份聚合，全部渠道）；meta 逐键覆盖
        # （D105-19 同型——meta 只覆盖它声明的键，缺席键落文件卡）。
        if os.path.isfile(_AUTH_CHANNELS_FILE):
            merged = {p: dict(c) for p, c in _builtin_channels_from_file(_AUTH_CHANNELS_FILE).items()}
        else:
            merged = {}
            log.error(f"oauth-providers.json 缺失 {_AUTH_CHANNELS_FILE} —— 当前无可用登录渠道"
                      f"（该文件是运行必需配置，随仓库发布；解耦后不放回代码兜底卡）")
        raw = user_db.get_meta_value("oauth_providers_config") or ""
        if raw:
            try:
                data = json.loads(raw)
                for provider, card in (data or {}).items():
                    provider = str(provider or "").strip()
                    # 约定与文件分支同口径（providers.py:_builtin_channels_from_file）：
                    # 下划线前缀 = 文档/注释键，不是渠道名。meta 里若被历史同步写进
                    # `_fields`/`_readme` 之类文档键，不过滤就会凭空多出一个"渠道"，
                    # 登录页按目录渲染按钮、未配置提示点名它（实弹：hint 出现 _fields）。
                    if not provider or provider.startswith("_"):
                        continue
                    if not isinstance(card, dict):
                        continue
                    _base = dict(merged.get(provider) or {})
                    _base.update(card)
                    merged[provider] = _base
                log.info(f"Loaded {len(merged)} OAuth channels (file cards + meta overrides): {sorted(merged)}")
            except Exception as e:
                log.warning(f"oauth_providers_config 解析失败: {e} —— 仅用文件卡: {sorted(merged)}")
        else:
            log.info(f"Using auth-channel file cards: {sorted(merged)}")

        # D109：表就绪后 DB 表是运行时真源（文件与 meta 只是导入源）；
        # 表内有的 provider 覆盖同名文件卡，文件独有的仍保留（防表缺行致渠道消失）。
        _tbl = _channels_from_table()
        if _tbl:
            for _p, _card in _tbl.items():
                merged[_p] = dict(_card)
            log.info(f"oauth channels from table: {sorted(_tbl)}")

        _PROVIDERS_CACHE.clear()
        _PROVIDERS_CACHE.update(merged)
        _loaded = True
        _cache_ts = time.time()

def invalidate_provider_cache():
    """P2-2: 主动失效 Provider 缓存——admin 改 OAuth 配置后调用，无需重启服务。"""
    global _loaded
    with _providers_lock:
        _loaded = False
    log.info("OAuth provider cache invalidated")

def get_provider(provider: str) -> "dict | None":
    """获取 Provider 配置。

    D102-D（R1，2026-11-08）：无条件走 _load_providers（内含 TTL 双重检查
    锁，热路径成本=一次内存时间比较）——原惰性守卫形态导致 TTL
    过期后登录端点永不重载（带外写库 40 分钟不可见，实测复现）。
    对齐 platform_card.get_platform_card 现行形态（统一入口先例）。
    """
    _load_providers()
    return _PROVIDERS_CACHE.get(provider)

def is_channel_enabled(provider: str) -> bool:
    """登录渠道是否启用（P2-4 fail-closed 判据）。

    真源 = `grape_t_oauth_channels.enabled`（与登录目录 `_catalog_from_object` 的
    enabled_only 过滤同源）。表未就绪 / 读失败 → 按"启用"处理：这是历史行为，
    否则 DB 抖动会把所有登录打挂。只有明确读到 enabled=0 才拒绝。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return True
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        with Session(get_engine()) as s:
            row = OAuthChannelRepo.get(s, str(provider))
        if row is None:
            return False  # 渠道不在对象表里 → 不存在即不可登录
        return bool(getattr(row, "enabled", 1))
    except Exception as e:
        from src.kernel.config import log

        log.warning(f"is_channel_enabled 读取失败，按启用处理（fail-open）: {e}")
        return True


def list_providers() -> list[str]:
    """列出所有已配置的 Provider。"""
    _load_providers()
    return list(_PROVIDERS_CACHE.keys())


def _catalog_order_index() -> "dict[str, int]":
    """登录渠道展示顺序：`configs/oauth-providers.json` 的 `_ui_order`（可选）→ 配置键顺序。

    D102-B 曾用「平台 key → 序号」的字面表固定展示序（新渠道必须改代码），与「渠道集合由
    配置/表驱动」的口径不一致（2026-11-14 深度检查）。顺序属产品展示偏好，改为数据：
    优先 `_ui_order` 数组，其次配置文件的键顺序（当前文件即 gitcode → github → gitee →
    huawei），未列出的渠道排最后按名。
    """
    # P2-7：本函数被 HTML head 注入与 /auth/providers 每请求调用；同文件其它读配置
    # 的都走 _load_providers 的 TTL 缓存，这里不能每次 open+parse。
    # 缓存键 = (路径, mtime, size)：配置被带外改写时自动失效，无需重启。
    global _order_cache_key, _order_cache_idx
    order: list[str] = []
    try:
        _st = os.stat(_AUTH_CHANNELS_FILE)
        _key = (_AUTH_CHANNELS_FILE, int(_st.st_mtime), int(_st.st_size))
        if _order_cache_key == _key and _order_cache_idx is not None:
            order = _order_cache_idx
        else:
            with open(_AUTH_CHANNELS_FILE, encoding="utf-8") as f:
                _cfg = json.load(f)
            order = [p for p in (_cfg.get("_ui_order") or []) if isinstance(p, str) and p]
            _order_cache_key = _key
            _order_cache_idx = order
    except Exception:
        order = []
    idx = {p: i for i, p in enumerate(order)}
    for k in list_providers():
        if k not in idx:
            idx[k] = len(idx)
    return idx


def _catalog_from_object() -> "list[dict]":
    """D109 对象层读模型（表就绪时=唯一真源，不再回查 meta）。

    以 grape_t_oauth_channels 行为准：provider/name/brand_color 取行内定义字段，
    configured 取行内加密凭据列（client_id_enc）——凭据迁移后 meta 不再是真源。
    表未就绪 / 读失败 → 返回空列表，调用方回落 file+meta（§16 影响 #1 fail-open）。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return []
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        with Session(get_engine()) as s:
            rows = list(OAuthChannelRepo.list_all(s, enabled_only=True))
        if not rows:
            return []
        from src.domains.runtime.vcs.platform_card import get_platform_card

        out = []
        for row in rows:
            # 防御：历史脏行（下划线文档键）不参与目录
            provider = str(row.provider) if row.provider is not None else ""
            provider = provider.strip()
            if not provider or provider.startswith("_"):
                continue
            cfg = get_provider(provider) or {}
            pat_url = str(cfg.get("pat_create_url") or "").strip()
            try:
                card = get_platform_card(provider)
                if not pat_url:
                    pat_url = (getattr(card, "pat_create_url", "") or "") if card else ""
            except Exception:
                pat_url = ""
            out.append({
                "provider": provider,
                "name": str(row.name or cfg.get("name") or provider),
                # configured 走与取用同一解析链：表内加密列优先，空列回落 meta
                # （迁移窗口）。两边都没有才是真未配置。
                "configured": bool(_get_client_id(provider)),
                "brand_color": str(row.brand_color or cfg.get("brand_color") or ""),
                "pat_create_url": pat_url,
            })
        _idx = _catalog_order_index()
        out.sort(key=lambda c: (_idx.get(c["provider"], len(_idx)), c["provider"]))
        return out
    except Exception as e:
        log.warning(f"oauth_channels table read failed, fallback to file+meta: {e}")
        return []


def providers_catalog() -> "list[dict]":
    """登录渠道目录（D108 登录按钮零延迟渲染）。

    返回固定展示序的 [{provider, name, configured, brand_color}]——
    服务端 HTML head 注入（window.__OAUTH_PROVIDERS__）与 /auth/providers
    路由共用此单一来源：登录页按钮在首帧渲染前同步生成，不等 AJAX。

    D109 收口：查询真源 = 平台对象层（grape_t_oauth_channels），不再逐个回查
    meta（凭据已迁到表内加密列，meta 只是导入源）。表未就绪才回落 file+meta。
    """
    _from_table = _catalog_from_object()
    if _from_table:
        return _from_table

    from src.domains.mgmt.user.db import get_meta_value
    # D102-B 的展示序改由数据驱动（_catalog_order_index：_ui_order → 配置键顺序）
    _idx = _catalog_order_index()
    keys = sorted(list_providers(), key=lambda k: (_idx.get(k, len(_idx)), k))
    result = []
    # 2026-11-13：手动绑定令牌弹窗需要「前往令牌创建页面」的直达链接。
    # 单一来源=平台卡 pat_create_url（gitee/gitlab 等按卡派生）；取不到就返回空串，
    # 由前端对 github/gitcode 用官方预选参数链接兜底（见 AccountView.patCreateUrl）。
    from src.domains.runtime.vcs.platform_card import get_platform_card

    for key in keys:
        cfg = get_provider(key) or {}
        client_id = _get_client_id(key)
        pat_url = str(cfg.get("pat_create_url") or "").strip()
        try:
            card = get_platform_card(key)
            if not pat_url:
                pat_url = (getattr(card, "pat_create_url", "") or "") if card else ""
        except Exception:
            pat_url = ""
        result.append({
            "provider": key,
            "name": cfg.get("name", key),
            "configured": bool(client_id),
            "brand_color": cfg.get("brand_color", ""),
            "pat_create_url": pat_url,
        })
    return result

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
        user_url = cfg["user_url"]
        # 少数平台（如华为 GOpen.User.getInfo）只认查询参数形式的 access_token，
        # 且忽略 Authorization 头：user_url 用 {access_token} 占位，替换后不再发 Bearer 头。
        _token_in_url = "{access_token}" in user_url
        if _token_in_url:
            user_url = user_url.replace(
                "{access_token}", urllib.parse.quote(access_token, safe=""))
        headers = {
            "Accept": cfg.get("accept_header", "application/json"),
        }
        if not _token_in_url:
            headers["Authorization"] = f"Bearer {access_token}"

        _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        _proxies = {"https": _proxy, "http": _proxy} if _proxy else None

        resp = _req.get(user_url, headers=headers, timeout=15, proxies=_proxies)
        resp.raise_for_status()
        user_info = resp.json()
    except Exception as e:
        log.warning(f"Get user info for {provider} failed: {e}")
        return None

    result = {
        "external_id": str(user_info.get(cfg["id_field"], "")),
        "external_login": user_info.get(cfg["login_field"], ""),
        "external_name": user_info.get(cfg["name_field"], ""),
        # 头像字段各平台不同（华为 headPictureURL / GitHub avatar_url），配置可覆盖
        "external_avatar": user_info.get(cfg.get("avatar_field", "avatar_url"), ""),
        "access_token": access_token,
        "refresh_token": "",
        "email": user_info.get("email", "") or "",
        "email_verified": bool(user_info.get("email_verified", False)),
    }

    # P2-2: 渠道 /user 接口默认不返回 email 时，按渠道卡 emails_endpoint 补抓
    # 主邮箱（S89/D65 能力位；github 内置 /user/emails，其余渠道空=无补抓）。
    _emails_ep = _channel_emails_endpoint(provider, cfg)
    if _emails_ep and not result["email"]:
        try:
            emails_resp = _req.get(
                _emails_ep,
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

def _object_credential(provider: str, column: str) -> "str | None":
    """D109 对象层凭据读口：grape_t_oauth_channels.client_id_enc / client_secret_enc。

    表就绪时表是唯一真源（凭据已迁加密列，meta 只是导入源）；表未就绪 / 无该行 /
    解密失败 → 返回 None，调用方回落 meta（fail-open，登录不被新表拖挂）。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return None
        from sqlalchemy.orm import Session

        from src.domains.mgmt.auth.credential_crypto import decrypt
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        with Session(get_engine()) as s:
            row = OAuthChannelRepo.get(s, provider)
        if row is None:
            return None
        blob = getattr(row, column, "") or ""
        if not blob:
            # 行在但凭据列为空 = 迁移未落地（如历史库只配了 meta，或迁移因缺
            # secret 跳过该条）。此时返回 None 交给 meta 兜底，避免登录被误判
            # "未配置"（fail-open，与 §16 影响 #1 同口径）。
            return None
        return decrypt(blob) or ""
    except Exception as e:
        log.warning(f"oauth channel credential read failed ({provider}.{column}), "
                    f"fallback to meta: {e}")
        return None


def _get_client_id(provider: str) -> str:
    """获取 OAuth client_id：表就绪读对象层加密列，否则回落 meta。"""
    _from_object = _object_credential(provider, "client_id_enc")
    if _from_object is not None:
        return _from_object
    return user_db.get_meta_value(f"oauth_{provider}_client_id") or ""

def _get_client_secret(provider: str) -> str:
    """获取 OAuth client_secret：表就绪读对象层加密列，否则回落 meta。"""
    _from_object = _object_credential(provider, "client_secret_enc")
    if _from_object is not None:
        return _from_object
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
    # fallback 1：server_base_url 配置（nginx 反代场景优先用配置而非 Host header）
    try:
        from src.domains.mgmt.user.db import get_browser_callback_url
        base = get_browser_callback_url()
        if base and "localhost" not in base and "127.0.0.1" not in base:
            return f"{base.rstrip('/')}{path}"
    except Exception:
        pass

    # fallback 2：从 Host header 构造（开发环境直连）
    # 生产环境强制 https，防止 X-Forwarded-Proto 伪造
    from src.domains.mgmt.auth.security_config import is_production
    if is_production():
        scheme = "https"
    else:
        scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = _get_trusted_host(request)
    return f"{scheme}://{host}{path}"

def _get_trusted_host(request) -> str:
    """获取可信 Host（BUG-1 修复 + Issue #372 §15.3 X-Forwarded-Host 支持）。

    优先级：
    1. X-Forwarded-Host header（nginx 代理场景，含用户真实地址+端口）
    2. Host header（直连场景，开发环境兼容）
    → TRUSTED_HOSTS 环境变量白名单校验最终值

    Issue #372: 排除 localhost / 127.0.0.1（nginx 代理场景下 Host 是内网地址，
    X-Forwarded-Host 才是用户真实地址）。
    """
    # R11.1: 未设 TRUSTED_HOSTS 时不读 X-Forwarded-Host（防伪造）
    trusted_hosts = os.environ.get("TRUSTED_HOSTS", "").strip()
    fwd_host = request.headers.get("X-Forwarded-Host", "").strip() if trusted_hosts else ""
    raw_host = request.headers.get("Host", "")

    # 选择非 localhost 的那个（nginx 代理场景 Host 是 127.0.0.1:3000）
    _local = ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    candidate = ""
    if fwd_host and not any(fwd_host.lower() == lh or fwd_host.lower().startswith(lh + ":") for lh in _local):
        candidate = fwd_host
    elif raw_host and not any(raw_host.lower() == lh or raw_host.lower().startswith(lh + ":") for lh in _local):
        candidate = raw_host
    else:
        candidate = raw_host  # 都是 localhost，保持原行为（开发环境）

    # 校验 Host 是否在可信白名单中（trusted_hosts 已在上方读取）
    if trusted_hosts:
        hosts = [h.strip().lower() for h in trusted_hosts.split(",") if h.strip()]
        if candidate.lower() in hosts:
            return candidate
        # 不在白名单 → 回退到第一个可信 Host（防止开放重定向）
        safe_host = candidate.replace("\n", "").replace("\r", "")
        log.warning(f"Host '{safe_host}' not in TRUSTED_HOSTS, using fallback '{hosts[0]}'")
        return hosts[0]

    # 未设置 TRUSTED_HOSTS → 返回候选值（开发环境兼容）
    return candidate

def is_oidc_channel(cfg: "dict | None") -> bool:
    """渠道卡是否为 OIDC 类型。

    兼容两种键：卡内 `type`（oauth_providers_config 现行口径，华为卡即此）
    与 `channel_type`（D102-E 方案稿曾用的键——回调兜底曾只读它导致兜底
    永不触发，华为登录全挂；两键任一为 oidc 即判定为 OIDC 渠道）。
    """
    if not cfg:
        return False
    return str(cfg.get("type") or cfg.get("channel_type") or "").strip().lower() == "oidc"

def _channel_emails_endpoint(provider: str, cfg: "dict | None" = None) -> str:
    """S89/D65：渠道卡 emails 补抓端点（email 补抓能力位）。

    github 内置 /user/emails（其 /user 接口默认不返回邮箱）；其余渠道空=
    无补抓（用户信息接口自带邮箱或该渠道无此机制）。卡可配置覆盖。
    """
    try:
        raw = (cfg or {}).get("emails_endpoint")
        if raw is not None:
            return str(raw)
        if provider == "builtin_default_github_only_marker":
            pass
    except Exception:
        pass
    # 内置默认：仅 github 渠道有补抓端点（存量语义，经渠道内置卡派生）
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
        if card is not None and card.git_tree_family == "github_api":
            return f"https://{card.host.rstrip('/')}/user/emails" if card.host != "github.com" else "https://api.github.com/user/emails"
    except Exception:
        pass
    return ""

# 说明：登录渠道卡真源=configs/oauth-providers.json（一份聚合，全部渠道）。
# 代码里不再有内置渠道卡字面量——加渠道/换端点 = 加文件，零代码发版。
