"""登录渠道协议引擎（D26/S38-S43）：三步契约 + 协议注册表，实例全配置化。

抽象落位（见 docs/weekly-09/multi-platform-decouple-design.md D26）——与
vcs 方言模板同一条设计定律：**协议引擎是模板（写一次），实例全部进卡**：
- 内置三个引擎：oauth2 / oidc（OAuth2 家族标准变体，纯配置实例化）/ saml
  （通用 SAML 2.0 引擎，IdP 差异全部由卡字段承载：idp_sso_url / attribute_map /
  IdP 元数据）。新增渠道/换 IdP = 一张卡，零平台代码。
- 协议引擎实现统一三步契约：
  ① initiate_auth(state, redirect_uri) → 重定向 URL 或登录表单参数
  ② handle_callback(callback_input) → 归一化身份声明 IdentityClaims
  ③ refresh(tokens)（可选；SAML 无此步）
- 关键不变量：任何协议的产出都是同一身份声明结构——下游
  external_identities / 白名单 / grape_accounts / JWT 全部协议无关。
- 注册表（S40）：register_channel_strategy(type, engine) 仅作非标准协议的
  逃生口（与 register_vcs_backend 同构）；卡 type 未注册时构建校验拒绝
  （不静默回退 oauth2——协议错配是安全事故）。

批次边界：本模块是契约层与引擎骨架；SAML 断言验签待真实 IdP 元数据联调
（S38 实弹件，元数据字段已在卡契约中预留）；OAuth2 存量路由
（grape_auth.py）向引擎层的迁移是渐进工作，存量行为零改动。
"""
from __future__ import annotations
from src.kernel.config import log

from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityClaims:
    """归一化身份声明（S38/S39 契约）：所有协议策略的统一产出。

    external_id 是绑定主键（缺失即拒绝，S42）；login 是白名单判定键；
    name/email 是展示字段（缺失降级空值）。
    """

    provider: str
    external_id: str
    login: str = ""
    name: str = ""
    email: str = ""
    tokens: dict = None  # type: ignore[assignment]  # OAuth2 类协议附带（SAML 空）


class ChannelAuthError(Exception):
    """渠道认证失败（可向用户展示的明确错误）。"""


class LoginChannelStrategy:
    """登录渠道协议基座（三步契约 + 卡字段契约校验）。

    能力声明（D28：路由按能力分派，不比 type 字符串）：
    - needs_oauth_client：协议是否需要 OAuth 应用（client_id）；
    - post_callback：回调的 HTTP 形态是否为 POST binding（SAML ACS 表单）。
    """

    channel_type: str = ""
    needs_oauth_client: bool = True
    post_callback: bool = False

    def validate_card(self, card: dict) -> None:
        """校验渠道卡必需字段（S45）：构建时拒绝，报缺失字段名。"""
        return

    def initiate_auth(self, state: str, redirect_uri: str, card: dict,
                      extra_state: "dict | None" = None) -> dict:
        """发起认证：返回 {"redirect_url": ...} 或 {"form": {...}}。

        extra_state 由路由传入（binding_user_id / login_flow / nonce 等），
        state 持久化责任归策略（委托存量机制或自行落库）。
        """
        raise NotImplementedError

    def handle_callback(self, callback_input: dict, card: dict) -> IdentityClaims:
        """处理回调：callback_input 协议相关（code/SAMLResponse/…），产出身份声明。"""
        raise NotImplementedError

    def refresh(self, tokens: dict, card: dict) -> dict:
        """刷新 token（可选）。默认不支持（SAML 等无刷新语义）。"""
        raise ChannelAuthError(f"channel type {self.channel_type} has no refresh")


# ---------------------------------------------------------------- 注册表（S40）

_CHANNEL_STRATEGIES: dict[str, LoginChannelStrategy] = {}
_BUILTIN_TYPES = ("oauth2", "oidc", "saml")


def register_channel_strategy(channel_type: str, strategy: LoginChannelStrategy) -> None:
    """注册协议策略（插件口，与 register_vcs_backend 同构）。

    内置类型（oauth2）不可覆盖——防止第三方把标准协议换成私改实现。
    """
    if channel_type in _BUILTIN_TYPES and _CHANNEL_STRATEGIES.get(channel_type) is not None:
        raise ChannelAuthError(f"builtin channel type {channel_type!r} cannot be overridden")
    if not channel_type or not channel_type.replace("_", "").replace("-", "").isalnum():
        raise ChannelAuthError(f"invalid channel type: {channel_type!r}")
    strategy.channel_type = channel_type
    _CHANNEL_STRATEGIES[channel_type] = strategy


def get_channel_strategy(channel_type: str) -> "LoginChannelStrategy | None":
    if channel_type == "oauth2":
        return _CHANNEL_STRATEGIES.get("oauth2")  # 内置（init 时注册）
    return _CHANNEL_STRATEGIES.get(channel_type)


def build_channel_strategy(provider: str) -> "LoginChannelStrategy":
    """按渠道卡构建策略（S40/S45）：type 未注册 → 拒绝；字段缺失 → 构建时拒绝并报字段名。"""
    from src.domains.mgmt.auth.providers import get_provider
    card = get_provider(provider) or {}
    ctype = str(card.get("type") or "oauth2")  # 存量卡无 type = oauth2（向后兼容）
    strategy = get_channel_strategy(ctype)
    if strategy is None:
        raise ChannelAuthError(
            f"channel type {ctype!r} not registered (provider={provider!r}); "
            f"register via register_channel_strategy before configuring the card")
    _card_with_key = {**card, "provider_key": provider}
    strategy.validate_card(_card_with_key)
    return strategy


# ---------------------------------------------------------------- OAuth2 策略（内置）

class OAuth2ChannelStrategy(LoginChannelStrategy):
    """OAuth2 协议策略：委托 providers.py 既有机制（行为零改动）。

    initiate 委托 build_auth_url；handle_callback 委托
    exchange_token + get_user_info 并归一为 IdentityClaims。
    存量路由（grape_auth.py）向本策略的迁移是渐进工作。
    """

    channel_type = "oauth2"

    _REQUIRED_FIELDS = ("auth_url", "token_url", "user_url")

    def validate_card(self, card: dict) -> None:
        missing = [f for f in self._REQUIRED_FIELDS if not str(card.get(f) or "")]
        if missing:
            raise ChannelAuthError(
                f"oauth2 channel card missing fields: {', '.join(missing)} "
                f"(provider={card.get('provider_key', '?')})")

    def initiate_auth(self, state: str, redirect_uri: str, card: dict,
                      extra_state: "dict | None" = None) -> dict:
        from src.domains.mgmt.auth.providers import build_auth_url
        provider = card.get("provider_key") or ""
        out = build_auth_url(provider, state, redirect_uri,
                             extra_state_data=extra_state) or {}
        return {"redirect_url": out.get("redirect_url", "")}

    def handle_callback(self, callback_input: dict, card: dict) -> IdentityClaims:
        from src.domains.mgmt.auth.providers import exchange_token, get_user_info
        provider = card.get("provider_key") or ""
        code = str(callback_input.get("code") or "")
        redirect_uri = str(callback_input.get("redirect_uri") or "")
        if not code:
            raise ChannelAuthError("oauth2 callback missing code")
        tokens, _err = exchange_token(provider, code, redirect_uri) or (None, "")
        tokens = tokens or {}
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise ChannelAuthError("oauth2 token exchange returned no access_token")
        user = get_user_info(provider, access_token) or {}
        return normalize_identity_claims(provider, user, {
            "id_field": card.get("id_field", "id"),
            "login_field": card.get("login_field", "login"),
            "name_field": card.get("name_field", "nickname"),
            "email_field": card.get("email_field", "email"),
        }, tokens=tokens)


# ---------------------------------------------------------------- SAML 策略骨架（华为云 SSO，S38）

class OidcChannelStrategy(OAuth2ChannelStrategy):
    """OIDC 引擎（S44）：OAuth2 家族标准变体——claims 映射进卡，实例零平台代码。

    标准 claims 默认映射：sub→external_id、preferred_username→login、
    name/email→展示字段；卡 claims_map 可覆盖。端点直配（与 oauth2 同构，
    discovery 自动发现属实弹批次增强）。
    """

    channel_type = "oidc"

    _DEFAULT_CLAIMS = {
        "id_field": "sub",
        "login_field": "preferred_username",
        "name_field": "name",
        "email_field": "email",
    }

    def handle_callback(self, callback_input: dict, card: dict) -> IdentityClaims:
        from src.domains.mgmt.auth.providers import exchange_token, get_user_info
        provider = card.get("provider_key") or ""
        code = str(callback_input.get("code") or "")
        redirect_uri = str(callback_input.get("redirect_uri") or "")
        # D102-E（实弹热修语义重建）：_tokens 复用——华为等 OIDC IdP 的 code
        # 一次性（二次 exchange 报 1101 "code used twice"）。回调路由在
        # userinfo 失败转入引擎兜底时必须传回已换的 token 集合，不得重换。
        tokens = dict(callback_input.get("_tokens") or {})
        if not tokens:
            if not code:
                raise ChannelAuthError("oidc callback missing code")
            tokens, _err = exchange_token(provider, code, redirect_uri) or (None, "")
            tokens = tokens or {}
        access_token = str(tokens.get("access_token") or tokens.get("id_token") or "")
        if not access_token:
            raise ChannelAuthError("oidc token exchange returned no access_token")
        claims_map = dict(self._DEFAULT_CLAIMS)
        claims_map.update(card.get("claims_map") or {})
        # D102-E: userinfo 失败 → id_token 解码兜底（不验签，仅取 claims——
        # token 本身已由 IdP 的 token 端点签发，TLS 通道保证来源；华为 /user
        # 端点 404 时此兜底是唯一身份来源）
        user = {}
        try:
            user = get_user_info(provider, access_token) or {}
        except Exception as e:
            user = {}
        if not user:
            id_token = str(tokens.get("id_token") or "")
            if id_token and id_token.count(".") == 2:
                try:
                    import base64 as _b64
                    import json as _json
                    _payload = id_token.split(".")[1]
                    _payload += "=" * (-len(_payload) % 4)
                    claims = _json.loads(_b64.urlsafe_b64decode(_payload))
                    user = {
                        "sub": str(claims.get("sub") or ""),
                        "preferred_username": str(claims.get("preferred_username")
                                                  or claims.get("sub") or ""),
                        "name": str(claims.get("name") or ""),
                        "email": str(claims.get("email") or ""),
                    }
                    log.info(f"oidc userinfo fallback: id_token claims decoded "
                             f"(provider={provider}, sub={user['sub'][:8]}...)")
                except Exception as e2:
                    log.warning(f"oidc id_token decode fallback failed: {e2}")
        return normalize_identity_claims(provider, user, claims_map, tokens=tokens)



class SamlChannelStrategy(LoginChannelStrategy):
    """SAML 2.0 协议策略骨架（华为云 IAM 联邦 / 通用 IdP）。

    initiate：重定向到渠道卡 idp_sso_url（SP-initiated，RelayState 带 state）。
    handle_callback：SAMLResponse 属性断言按卡 attribute_map 归一为身份声明。
    验签（S41 元数据轮换）通过 verify_assertion 钩子注入——默认未配置时
    拒绝（安全默认：不验签的断言不可用于登录）。
    """

    channel_type = "saml"
    needs_oauth_client = False   # D28：SAML 无 OAuth 应用（client_id 不适用）
    post_callback = True         # D28：SAML ACS 回调为 POST binding（表单）

    def validate_card(self, card: dict) -> None:
        missing = []
        if not str(card.get("idp_sso_url") or ""):
            missing.append("idp_sso_url")
        amap = card.get("attribute_map") or {}
        # SAML 属性名是 IdP 专属，无安全默认——必须卡明示 external_id 映射
        ext_id_attr = str(amap.get("external_id") or "")
        if not ext_id_attr:
            missing.append("attribute_map.external_id")
        if missing:
            raise ChannelAuthError(
                f"saml channel card missing fields: {', '.join(missing)} "
                f"(provider={card.get('provider_key', '?')})")
        # S46 构建时稳定性校验：email/transient 类属性不可作绑定主键
        # （emailAddress 会随邮箱变更漂移；transient NameID 每次登录都变）。
        # 启发式只能判属性名形态——最终语义由 S46 回调值校验兜底。
        _n = ext_id_attr.lower()
        if any(k in _n for k in ("email", "mail", "transient")):
            raise ChannelAuthError(
                f"saml attribute_map.external_id={ext_id_attr!r} looks like an "
                f"email/transient attribute — binding key must be a persistent "
                f"identifier (S46); map a stable numeric/persistent attribute "
                f"instead (provider={card.get('provider_key', '?')})")

    def __init__(self, verify_assertion=None):
        """verify_assertion(saml_response_b64, card) → attributes dict；
        未注入时 handle_callback 拒绝（需要真实 IdP 元数据联调，S38 实弹件）。"""
        self._verify_assertion = verify_assertion

    def initiate_auth(self, state: str, redirect_uri: str, card: dict,
                      extra_state: "dict | None" = None) -> dict:
        idp_sso = str(card.get("idp_sso_url") or "")
        if not idp_sso:
            raise ChannelAuthError("saml card missing idp_sso_url")
        # D28：state 持久化责任归策略（与 OAuth 走 build_auth_url 内部落库对称）。
        # 存储语义同 §10.6：redirect_uri 存入 state，回调从 RelayState 取回校验。
        from src.domains.mgmt.user import db as user_db
        user_db.save_oauth_state(
            state, str(card.get("provider_key") or ""),
            {**(extra_state or {}), "redirect_uri": redirect_uri})
        sep = "&" if "?" in idp_sso else "?"
        return {"redirect_url": f"{idp_sso}{sep}RelayState={state}"}

    def handle_callback(self, callback_input: dict, card: dict) -> IdentityClaims:
        b64 = str(callback_input.get("SAMLResponse") or "")
        if not b64:
            raise ChannelAuthError("saml callback missing SAMLResponse")
        if self._verify_assertion is None:
            raise ChannelAuthError(
                "saml assertion verification not configured (IdP metadata required, S41)")
        attributes = self._verify_assertion(b64, card) or {}
        provider = str(card.get("provider_key") or "")
        amap = card.get("attribute_map") or {}
        return normalize_identity_claims(provider, attributes, {
            "id_field": amap.get("external_id", "external_id"),
            "login_field": amap.get("login", "login"),
            "name_field": amap.get("name", "name"),
            "email_field": amap.get("email", "email"),
        })


# ---------------------------------------------------------------- 声明归一（S42）

def normalize_identity_claims(provider: str, attributes: dict,
                              field_map: dict, tokens: dict | None = None) -> IdentityClaims:
    """归一化身份声明（S42 语义，协议无关）。

    external_id / login 缺失 → ChannelAuthError（绑定主键/白名单键不可缺）；
    name / email 缺失 → 空值降级（展示字段）。
    """
    def _pick(field: str) -> str:
        v = attributes.get(field)
        if isinstance(v, list):  # SAML 属性常为多值
            v = v[0] if v else None
        return str(v).strip() if v is not None else ""

    external_id = _pick(field_map.get("id_field", "id"))
    login = _pick(field_map.get("login_field", "login"))
    if not external_id:
        raise ChannelAuthError(
            f"identity claim missing external_id (provider={provider!r}) — 绑定主键不可缺")
    # S46 回调值校验：email 形态的值不可作绑定主键（会随邮箱变更漂移，
    # 同人换邮箱后归一产出新 external_id → 误建新绑定）。复合键
    # UNIQUE(provider, external_id)（db_setup DDL）以 provider 隔离不同 IdP
    # 的主体空间；provider 名 = IdP 实例身份（D27 纪律：换 IdP 必须换卡名）。
    if "@" in external_id:
        raise ChannelAuthError(
            f"external_id value {external_id!r} looks like an email address — "
            f"binding key must be a stable persistent identifier, not an email "
            f"(S46, provider={provider!r})")
    if not login:
        login = external_id  # 白名单键降级：login 缺失时以 external_id 充当（S42）
    return IdentityClaims(
        provider=provider,
        external_id=external_id,
        login=login,
        name=_pick(field_map.get("name_field", "name")),   # 展示字段：缺失降级空值
        email=_pick(field_map.get("email_field", "email")),
        tokens=dict(tokens or {}),
    )


# 内置注册（oauth2/oidc/saml 不可覆盖；saml 验签钩子由实弹装配注入，
# 未注入时 callback 安全默认拒绝）
register_channel_strategy("oauth2", OAuth2ChannelStrategy())
register_channel_strategy("oidc", OidcChannelStrategy())
register_channel_strategy("saml", SamlChannelStrategy())
