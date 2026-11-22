"""仓库所属命名空间（个人 / 组织）的领域模型与清单端口。

为什么要有这一层：
- 「个人空间 / 组织」是四平台的**共性模型**，但各家叫法不同（Gitee 个人空间、
  GitHub personal account、GitLab personal namespace/group），且取清单的端点各异
  （`/users/{login}/orgs`、`/user/memberships/orgs`、`/groups?min_access_level=30`、
  `/user/orgs`）。原先这些差异散在 `repo_manager` 的 fetcher 注册表 + 路由里的
  字面量 `"type": "user"/"org"`，新平台接入要改多处。
- 更硬的缺陷（2026-11-20 实查）：fetcher 注册表只登记了 gitcode/github，
  **GitLab / Gitee 的组织空间根本进不了清单** —— 「组织」页面对这两家永远只有个人项。
- 判据必须是**白名单**：`kind` 只有 `personal` / `org` / `unknown` 三态，未知类型不落进
  任何"可建仓"桶（fail-safe defaults：未知输入不能落到权限更宽的桶里）。
- 同一份清单是服务端校验 owner 的权威：前端下拉只是它的投影，建仓前必须比对。

本模块只做「清单 + 归一层」，HTTP 细节由各 provider 端口（`list_create_spaces`）或
平台适配器承担；写入不在这里（组织/仓库行归各自域）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "NamespaceRef",
    "compose_namespaces",
    "namespace_from_org_row",
    "personal_namespace",
    "NamespaceNotVisible",
    "NamespaceLister",
    "KIND_PERSONAL",
    "KIND_ORG",
    "KIND_UNKNOWN",
    "listable_providers",
    "list_org_namespaces",
    "list_namespaces",
    "namespace_paths",
    "assert_owner_visible",
]

KIND_PERSONAL = "personal"
KIND_ORG = "org"
KIND_UNKNOWN = "unknown"

#: 个人桶白名单（`user` 为上游历史字面量的兼容别名）
_PERSONAL_KINDS = frozenset({KIND_PERSONAL, "user"})
#: 组织桶白名单（`group` 为 GitLab 原生字面量的兼容别名）
_ORG_KINDS = frozenset({KIND_ORG, "group"})


def classify_namespace_kind(raw_kind: str | None) -> str:
    """平台/历史 type → 归一 kind（白名单；未知一律 unknown，不默认个人）。"""
    kind = str(raw_kind or "").strip().lower()
    if kind in _PERSONAL_KINDS:
        return KIND_PERSONAL
    if kind in _ORG_KINDS:
        return KIND_ORG
    return KIND_UNKNOWN


@dataclass(frozen=True)
class NamespaceRef:
    """一个可建仓的命名空间（值对象）。`path` 即建仓 owner 参数。"""

    provider: str
    path: str
    name: str
    avatar_url: str = ""
    kind: str = KIND_ORG

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", classify_namespace_kind(self.kind))

    @property
    def is_personal(self) -> bool:
        return self.kind == KIND_PERSONAL

    @property
    def is_org(self) -> bool:
        return self.kind == KIND_ORG

    def as_payload(self) -> dict[str, Any]:
        """管理面/前端投影：`type` 用归一 kind（个人/组织白名单两端都认）。"""
        return {
            "login": self.path,
            "name": self.name or self.path,
            "avatar_url": self.avatar_url,
            "type": self.kind,
        }


class NamespaceNotVisible(ValueError):
    """owner 不在该用户的服务端可见清单里（拒绝越权/猜测型建仓）。"""


@runtime_checkable
class NamespaceLister(Protocol):
    """组织空间清单端口（每个平台一个实现，注册进下表）。"""

    provider: str

    def list_orgs(self, token: str) -> list[NamespaceRef]:
        ...


class _FetcherLister:
    """复用 `repo_manager` 既有 fetcher（gitcode/github 的 /orgs 家族）。"""

    def __init__(self, provider: str, fetcher) -> None:
        self.provider = provider
        self._fetcher = fetcher

    def list_orgs(self, token: str) -> list[NamespaceRef]:
        return [
            NamespaceRef(provider=self.provider,
                         path=str((row or {}).get("login") or "").strip(),
                         name=str((row or {}).get("name") or ""),
                         avatar_url=str((row or {}).get("avatar_url") or ""),
                         kind=KIND_ORG)
            for row in (self._fetcher(token) or [])
            if str((row or {}).get("login") or "").strip()
        ]


class _CardSpacesLister:
    """走 provider 端口的 `list_create_spaces`（GitLab 组/子组；未来平台声明即得）。"""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def list_orgs(self, token: str) -> list[NamespaceRef]:
        from src.domains.mgmt.vcs import create_provider

        spaces = create_provider(self.provider).list_create_spaces(token) or []
        return [
            NamespaceRef(provider=self.provider,
                         path=str((row or {}).get("path") or "").strip(),
                         name=str((row or {}).get("name") or ""),
                         kind=KIND_ORG)
            for row in spaces
            if str((row or {}).get("path") or "").strip()
        ]


class _GiteeUserOrgsLister:
    """Gitee v5：`GET /user/orgs`（当前 token 用户所属组织）。"""

    provider = "gitee"

    def list_orgs(self, token: str) -> list[NamespaceRef]:
        from src.domains.mgmt.vcs import create_provider

        # Gitee 端口没有「组织空间」抽象方法，这里用其卡驱动的通用 HTTP 能力；
        # 取不到能力（非配置驱动 Provider）就返回空清单，不臆造。
        http = getattr(create_provider(self.provider), "_http", None)
        if http is None:
            return []
        rows = http("GET", "/user/orgs?per_page=100", token) or []
        if not isinstance(rows, list):
            return []
        return [
            NamespaceRef(provider=self.provider,
                         path=str((row or {}).get("login") or (row or {}).get("path") or "").strip(),
                         name=str((row or {}).get("name") or ""),
                         avatar_url=str((row or {}).get("avatar_url") or ""),
                         kind=KIND_ORG)
            for row in rows
            if str((row or {}).get("login") or (row or {}).get("path") or "").strip()
        ]


def _registry() -> dict[str, NamespaceLister]:
    """provider → 组织清单端口（登记顺序即「组织」页平台顺序）。

    内置四平台 + **所有 GitLab 方言的实例卡**（自建实例一实例一卡，provider id
    形如 `gitlab-corp`）：实例卡的本事全来自卡，组织清单端口按方言复用同一实现，
    不再需要为每个自建实例往这里加一行（上一版是固定四键，新卡在清单里恒缺席）。
    """
    from src.domains.mgmt.user.repo_manager import fetch_github_orgs, fetch_user_orgs

    registry: dict[str, NamespaceLister] = {
        "gitcode": _FetcherLister("gitcode", lambda tok: fetch_user_orgs(tok, admin_only=True)),
        "github": _FetcherLister("github", lambda tok: fetch_github_orgs(tok, admin_only=True)),
        "gitlab": _CardSpacesLister("gitlab"),
        "gitee": _GiteeUserOrgsLister(),
    }
    for provider in _instance_card_providers():
        if provider not in registry:
            registry[provider] = _CardSpacesLister(provider)
    return registry


def _instance_card_providers() -> list[str]:
    """平台卡里「同族但非内置名」的实例 provider（目前只认 GitLab 方言的实例）。

    只收 GitLab 方言：其 lister（list_create_spaces）本来就是卡驱动、可跨实例；
    github/gitee 的内置 orgs 抓取绑死官方域名，收进来反而是假可用。
    """
    try:
        from src.kernel.ports import vcs_cards

        providers = list(vcs_cards.list_vcs_platforms() or [])
    except Exception:  # noqa: BLE001 - 卡端口不可用（单测/未装配）按无实例处理
        return []
    out: list[str] = []
    for provider in providers:
        if provider in ("gitcode", "github", "gitlab", "gitee"):
            continue
        try:
            card = vcs_cards.get_platform_card(provider)
        except Exception:  # noqa: BLE001
            continue
        if card is not None and str(getattr(card, "dialect", "") or "") == "gitlab":
            out.append(provider)
    return out


def listable_providers() -> list[str]:
    return sorted(_registry())


def list_org_namespaces(provider: str, token: str) -> list[NamespaceRef]:
    """组织空间清单（失败上抛，由调用方决定是否降级为「组织列表不可用」）。"""
    lister = _registry().get(str(provider or "").lower())
    if lister is None or not token:
        return []
    return lister.list_orgs(token)


def personal_namespace(provider: str, account: Mapping[str, Any] | None) -> NamespaceRef | None:
    """账号身份 → 个人命名空间 ref（无 login 时返回 None，不伪造）。"""
    login = str((account or {}).get("login") or "").strip()
    if not login:
        return None
    return NamespaceRef(
        provider=provider, path=login,
        name=str((account or {}).get("name") or login),
        avatar_url=str((account or {}).get("avatar_url") or ""),
        kind=KIND_PERSONAL,
    )


def namespace_from_org_row(provider: str, row: Mapping[str, Any] | None) -> NamespaceRef | None:
    """各平台 org 行（登录名/路径字段并存）→ 组织命名空间 ref。"""
    raw = row or {}
    path = str(raw.get("login") or raw.get("path") or raw.get("full_path") or "").strip()
    if not path:
        return None
    return NamespaceRef(provider=provider, path=path,
                        name=str(raw.get("name") or path),
                        avatar_url=str(raw.get("avatar_url") or ""),
                        kind=KIND_ORG)


def compose_namespaces(provider: str, account: Mapping[str, Any] | None,
                       org_rows: list[Mapping[str, Any]] | None) -> list[NamespaceRef]:
    """个人（来自账号身份）+ 组织（来自 orgs 端点）合成完整清单（个人在前）。"""
    refs: list[NamespaceRef] = []
    personal = personal_namespace(provider, account)
    if personal is not None:
        refs.append(personal)
    for row in (org_rows or []):
        ref = namespace_from_org_row(provider, row)
        if ref is not None and all(r.path != ref.path for r in refs):
            refs.append(ref)
    return refs


def platform_login(provider: str, token: str) -> str:
    """用 token 解析**平台侧**登录名（GitLab /user 是 username，其它平台是 login）。

    为什么必须问平台（2026-11-20 实锤）：owner 校验曾把「本系统账号名」当平台登录名，
    `gvtest3` 的 gitlab 个人空间实为 `yangaiwu` —— 前端下拉里选个人空间会带
    owner=yangaiwu，被服务端清单（个人项按 gvtest3 合成）判 NAMESPACE_NOT_VISIBLE 400，
    而 owner=""（隐含个人）却能成功。个人空间的权威身份只能来自平台本身。
    """
    try:
        from src.domains.mgmt.vcs import create_provider

        info = create_provider(provider).verify_token(token) or {}
        if isinstance(info, dict):
            return str(info.get("login") or info.get("username") or "").strip()
    except Exception:  # noqa: BLE001 - 解析失败退回调用方给的账号信息
        return ""
    return ""


def list_namespaces(provider: str, token: str, account: Mapping[str, Any] | None = None) -> list[NamespaceRef]:
    """完整清单：个人空间在前，组织在后（个人项由平台身份合成，不依赖 orgs 端点）。"""
    org_rows = [ref.as_payload() for ref in list_org_namespaces(provider, token)]
    acct = dict(account or {})
    verified = platform_login(provider, token)
    if verified:                    # 平台身份优先：本系统账号名可能有别名/大小写差异
        acct["login"] = verified
    return compose_namespaces(provider, acct, org_rows)


# F3（A4）：`assert_owner_visible` 在建仓/查重/一键创建三条路径上被调用，原实现每次
# 都打 2 次上游（/user + 组织清单），/check-name 还会按候选名轮询 → 秒级时延。
# 同一 (provider, token 身份) 的清单在 30s 内复用：陈旧窗口内新增组织不可见，
# 用户重试/重开弹窗即可（与 mgmt/vcs/namespaces.py 的 create_targets 缓存同口径）。
# 30s 来源（2026-11-23 实测）：一次清单 = platform_login(/user) + list_org_namespaces
# 两次上游往返，自建 worker → gitlab.com 单次 0.7–1.7s，合计 2–4s；30s 是
# 「用户重试一次必然过 TTL」的量级，同时把 /check-name 轮询成本压到一次上游。
_NS_TTL_SECONDS = 30

def _ns_cache():
    from src.kernel.ttl_cache import TTLCache
    global _NS_CACHE
    if _NS_CACHE is None:
        _NS_CACHE = TTLCache("user_namespace_paths", lambda: _NS_TTL_SECONDS)
    return _NS_CACHE

_NS_CACHE = None


def _ns_cache_key(provider: str, token: str, account: Mapping[str, Any] | None) -> str:
    import hashlib
    acct = str((account or {}).get("login") or (account or {}).get("username") or "")
    return f"{provider}|{hashlib.sha256(str(token or '').encode()).hexdigest()[:16]}|{acct}"


def namespace_paths(provider: str, token: str, account: Mapping[str, Any] | None = None) -> set[str]:
    key = _ns_cache_key(provider, token, account)
    cached = _ns_cache().get(key)
    if cached is not None:
        return set(cached)
    paths = {ref.path for ref in list_namespaces(provider, token, account) if ref.path}
    _ns_cache().set(key, paths)
    return paths


def assert_owner_visible(provider: str, token: str, owner: str,
                         account: Mapping[str, Any] | None = None) -> None:
    """建仓前校验 owner ∈ 服务端清单（个人空间或该用户可见组织），否则拒绝。

    这是「授权不猜」的服务端一侧：前端下拉只是投影，用户可控的 owner 必须回服务端
    权威清单比对。GitLab 的组/子组用完整 full_path 比对（与建仓 API 同口径）。
    """
    wanted = str(owner or "").strip()
    if not wanted:
        return                      # 空 = 个人空间，由平台按 token 身份落，不需要清单
    visible = namespace_paths(provider, token, account)
    if wanted in visible:
        return
    raise NamespaceNotVisible(
        f"{provider}: 所属空间 {wanted} 不在当前账号可用的空间清单内"
    )