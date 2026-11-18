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

# 凭据真源与解析序全部收在 credential_source（Issue #3054 P1）：本模块只做适配，
# 不再自写「表优先 / meta 兜底」——登录换 code、token 刷新、configured 三处共用同一实现。
from src.domains.mgmt.auth.credential_source import (  # noqa: E402
    CredentialDecryptError,
    provider_credentials,
)
from src.domains.mgmt.auth.retry_policy import DEFAULT_TOKEN_RETRY, RetryPolicy  # noqa: E402

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
        from src.domains.runtime.public import get_platform_card

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
                # configured 走与取用同一解析链（credential_source）：表内加密列优先，
                # 两列皆空才整对回落 meta（迁移窗口）。两边都没有才是真未配置。
                "configured": channel_configured(provider),
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
    from src.domains.runtime.public import get_platform_card

    for key in keys:
        cfg = get_provider(key) or {}
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
            "configured": channel_configured(key),
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

    # Issue #3054 QA：渠道凭证缺失时**不能**用空 client_id 拼 URL ——那会把用户送到
    # 第三方授权页看一个与其无关的报错。这里直接返回 None，由调用方给可见错误。
    _cid = _get_client_id(provider)
    if not _cid:
        log.warning(f"build_auth_url: {provider} client_id 未配置（渠道凭证缺失），拒绝拼 URL")
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
        "client_id": _cid,
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
                   code_verifier: str = "",
                   policy: "RetryPolicy | None" = None) -> "tuple[dict | None, str]":
    """用 code 换 access_token。

    P2-3: 统一用 requests 替代 urllib.request，与 get_user_info 保持一致。
    返回 (token_dict, error_type) tuple：
    - 成功：(token_dict, "")
    - 失败：(None, "network_timeout") / (None, "token_exchange_failed") /
      (None, "credential_decrypt_failed") / (None, "not_configured")

    Issue #3054 P1-4：重试语义由 :class:`RetryPolicy` 决定——**只对瞬时错误重试**
    （超时 / 连接类异常 / 5xx），4xx 立即返回并落 provider 的 error/error_description。
    原实现除超时外一律 `except Exception` 重试 3 次且不读响应体，把 invalid_client /
    redirect_uri_mismatch / invalid_grant 伪装成 token_exchange_failed；而按 RFC 6749
    重复出示已用 code 会撤销先前 token —— 一次确定性失败被放大成「用户被登出」。
    """
    cfg = get_provider(provider)
    if not cfg:
        return None, "not_configured"

    # 唯一取凭据入口（P1-2）：client_id + client_secret 同源成对。
    try:
        client_id, client_secret = get_client_credentials(provider)
    except CredentialDecryptError as e:
        log.error(f"Exchange token for {provider} blocked by credential failure: {e}")
        return None, CredentialDecryptError.code
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

    retry = policy or DEFAULT_TOKEN_RETRY
    try:
        import requests as _req
        _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        _proxies = {"https": _proxy, "http": _proxy} if _proxy else None
        last_err_type = "token_exchange_failed"
        for _attempt in range(retry.attempts):
            try:
                resp = _req.post(
                    cfg["token_url"],
                    data=data,
                    headers={"Accept": "application/json"},
                    timeout=(5, 10),  # (connect_timeout, read_timeout)
                    proxies=_proxies,
                )
            except Exception as e:
                if not retry.should_retry_exception(e):
                    log.error(f"Exchange token for {provider} non-retryable error: "
                              f"{type(e).__name__}: {e}")
                    return None, "token_exchange_failed"
                last_err_type = "network_timeout" if "Timeout" in {
                    c.__name__ for c in type(e).__mro__} else "token_exchange_failed"
                log.warning(f"Exchange token for {provider} transient failure "
                            f"(attempt {_attempt + 1}/{retry.attempts}): "
                            f"{type(e).__name__}: {e}")
                if not retry.is_last_attempt(_attempt):
                    import time as _t
                    _t.sleep(retry.delay_for(_attempt))
                continue

            status = int(getattr(resp, "status_code", 0) or 0)
            if retry.should_retry_status(status):
                last_err_type = "token_exchange_failed"
                log.warning(f"Exchange token for {provider} provider 5xx (attempt "
                            f"{_attempt + 1}/{retry.attempts}): status={status} "
                            f"body={_resp_body_snippet(resp)}")
                if retry.should_retry_server_error(_attempt):
                    import time as _t
                    _t.sleep(retry.delay_for(_attempt))
                    continue
                # Issue #3054 QA：5xx 可能发生在 code 已被消费之后，再出示会撤销已发
                # token（RFC 6749）→ 到此为止，返回原始失败，不再二次出示。
                log.error(f"Exchange token for {provider} 5xx: code may already be "
                          f"consumed, not re-presenting it (status={status})")
                return None, "token_exchange_failed"
            if status >= 400:
                # 4xx = 确定性失败（invalid_client / invalid_grant / redirect_uri_mismatch…）
                # 立即返回，并把 provider 的错误码与描述写进日志（P1-4 可观测性）
                oauth_err, desc = _parse_oauth_error(resp)
                log.error(f"Exchange token for {provider} rejected by provider: "
                          f"status={status} error={oauth_err or '-'} "
                          f"error_description={desc or '-'} "
                          f"body={_resp_body_snippet(resp)}")
                return None, "token_exchange_failed"

            try:
                return resp.json(), ""
            except Exception as e:
                log.error(f"Exchange token for {provider} invalid JSON response: "
                          f"status={status} {type(e).__name__}: {e} "
                          f"body={_resp_body_snippet(resp)}")
                return None, "token_exchange_failed"
        log.warning(f"Exchange token for {provider} failed after {retry.attempts} attempts: "
                    f"{last_err_type}")
        return None, last_err_type
    except Exception as e:
        log.warning(f"Exchange token for {provider} failed: {e}")
        return None, "token_exchange_failed"


def _resp_body_snippet(resp, limit: int = 300) -> str:
    """响应体摘要（日志用；避免整页 HTML/超长 body 灌日志，也不落我方 secret）。"""
    try:
        return str(resp.text or "")[:limit].replace("\n", " ")
    except Exception:
        return ""


def _parse_oauth_error(resp) -> "tuple[str, str]":
    """从 token 端点 4xx 响应解析 (error, error_description)（RFC 6749 §5.2）。

    兼容 JSON 与 form-encoded 两种响应体；解析不出就返回 ("", "")。
    """
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("error") or ""), str(body.get("error_description") or "")
    except Exception:
        pass
    try:
        import urllib.parse as _up

        parsed = _up.parse_qs(str(resp.text or ""))
        return (parsed.get("error", [""])[0], parsed.get("error_description", [""])[0])
    except Exception:
        return "", ""

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

# 凭据入口见模块顶部导入的 provider_credentials（成对/单列两种读法都在这里适配）。


def get_client_credentials(provider: str) -> "tuple[str, str]":
    """**唯一取凭据入口**（登录换 code / token 刷新 / configured 共用）。

    返回 ``(client_id, client_secret)``——同源成对；密文不可解抛
    :class:`CredentialDecryptError`，调用方必须显式失败（禁止回落 meta）。
    """
    cid, sec = provider_credentials.pair(provider)
    if cid.error or sec.error:
        err = cid.error or sec.error
        raise CredentialDecryptError(provider, (cid.field if cid.error else sec.field), err)
    return cid.value, sec.value


def _get_client_id(provider: str) -> str:
    """client_id（宽容版：解密失败=空串，绝不回落 meta）。"""
    try:
        return get_client_credentials(provider)[0]
    except CredentialDecryptError as e:
        log.error("get_client_id blocked: %s", e)
        return ""


def _get_client_secret(provider: str) -> str:
    """client_secret（宽容版：解密失败=空串，绝不回落 meta）。"""
    try:
        return get_client_credentials(provider)[1]
    except CredentialDecryptError as e:
        log.error("get_client_secret blocked: %s", e)
        return ""


def channel_configured(provider: str) -> bool:
    """``configured`` 判定：与运行时同一取凭据入口（Issue #3054 P1-1）。"""
    return provider_credentials.configured(provider)

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
        from src.domains.runtime.public import get_platform_card
        card = get_platform_card(provider)
        if card is not None and card.git_tree_family == "github_api":
            return f"https://{card.host.rstrip('/')}/user/emails" if card.host != "github.com" else "https://api.github.com/user/emails"
    except Exception:
        pass
    return ""

# 说明：登录渠道卡真源=configs/oauth-providers.json（一份聚合，全部渠道）。
# 代码里不再有内置渠道卡字面量——加渠道/换端点 = 加文件，零代码发版。
