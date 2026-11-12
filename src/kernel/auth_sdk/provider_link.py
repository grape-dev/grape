"""oauth_fill 显式链接注册表（D40/S60）——登录域与仓库域唯一耦合点的根治形态。

领域规则（用户拍板，三域解耦）：
- 仓库平台卡（vcs_providers_config）/ OAuth 登录渠道（oauth_providers_config）
  / 账号体系（grape_accounts + external_identities）彼此解耦；
- oauth_fill 是唯一刻意耦合点：渠道卡与平台卡**同名就位**即链接成立
  （渠道目录 ∩ 平台目录派生，不靠调用点字符串相等散落路由）；
- 刷新能力是**注册声明**（provider → refresh 实现），未注册返回 None
  ——"config-only 渠道无 OAuth 刷新"是显式语义，不是 else 兜底。

用法：
    from src.kernel.auth_sdk.provider_link import oauth_fill_targets, refresh_token_for
    targets = oauth_fill_targets()          # [{"provider", "supports_refresh"}]
    token = refresh_token_for(user_id, p)   # 委托注册的刷新实现

**D10"三合一"终态收口（D100，2026-11-08）**：②仓库凭据自动填充正式定为
**用时取用 lazy**（仓库域凭据取用走运行时链 TokenResolver →
external_identities 实时读+刷新），**不做落库填充**——原 provisioner 契约
路径已随 upc 废弃整体删除（P2=a，登录成功后零额外落库）。本注册表的
现役职责收敛为两项：
1. refresh_token_for —— 刷新腿（deps._try_refresh_user_token 消费，在役）
2. oauth_fill_targets —— 渠道∩平台链接清单声明面（诊断/运维视角：
   哪些平台登录即具备资源访问能力；supports_refresh 能力位显式化）
不再有"落库消费端待接线"事项——lazy 是终态，不是临时等效。

刷新实现注册（避免反向依赖，由实现方所在模块完成）：
    register_refresh_implementation("gitcode", try_refresh_gitcode_token)
"""
import threading
from typing import Any, Callable

_refresh_registry: "dict[str, Callable[[int], 'str | None']]" = {}
_registry_guard = threading.Lock()


def register_refresh_implementation(provider: str, refresh_fn) -> None:
    """注册某渠道的 OAuth 刷新实现（幂等，后注册覆盖先注册）。"""
    with _registry_guard:
        _refresh_registry[provider] = refresh_fn


def _refresh_capability(provider: str) -> bool:
    with _registry_guard:
        return provider in _refresh_registry


def oauth_fill_targets() -> "list[dict]":
    """oauth_fill 链接清单：渠道目录 ∩ 平台目录（目录派生，非平台名单）。

    每项 {"provider": str, "supports_refresh": bool}——
    supports_refresh=False 表示渠道可授权但无刷新实现（短期 token，
    过期需重新授权，语义显式）。
    """
    try:
        from src.domains.mgmt.auth.providers import list_providers
        channels = set(list_providers() or [])
    except Exception:
        channels = set()
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        platforms = set(list_vcs_platforms() or [])
    except Exception:
        platforms = set()
    return [
        {"provider": p, "supports_refresh": _refresh_capability(p)}
        for p in sorted(channels & platforms)
    ]


def refresh_token_for(user_id: str, provider: str) -> "str | None":
    """按注册表派发刷新：渠道不在 oauth_fill 链接（无渠道卡）或无注册
    实现时返回 None；刷新责任全部留在登录域（仓库域只消费结果）。"""
    if not provider:
        return None
    with _registry_guard:
        fn = _refresh_registry.get(provider)
    if fn is None:
        return None
    try:
        return fn(user_id)
    except Exception:
        return None
