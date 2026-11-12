"""VCS 事件归一化适配器：按 webhook 路径自动分发。

三层后端解析（D2/D10）：
1. 既有平台类（gitcode/github/gitlab，行为不变，S9 回归锁）
2. 平台卡 + 方言模板（GenericBackend）——新平台纯配置接入（S6/S17）
3. register_vcs_backend 插件口（escape hatch，保留）
"""
from .base import VcsBackend
from .gitcode import GitCodeBackend
from .github import GitHubBackend
from .gitlab import GitLabBackend


_BACKENDS = {
    "gitcode": GitCodeBackend(),
    "github": GitHubBackend(),
    "gitlab": GitLabBackend(),
}

_WEBHOOK_PATH_MAP = {
    "/webhook/gitcode": "gitcode",
    "/webhook/github": "github",
    "/webhook/gitlab": "gitlab",
}


def register_vcs_backend(name: str, backend):
    """注册第三方 VCS 后端（插件接口）"""
    _BACKENDS[name] = backend
    _WEBHOOK_PATH_MAP[backend.webhook_path] = name


def _generic_backend_for(provider: str):
    """按仓库平台卡构建 GenericBackend（方言策略 + 卡配置）。"""
    try:
        from .platform_card import get_platform_card
        from .dialects import get_dialect, GenericBackend
        card = get_platform_card(provider)
        if not card:
            return None
        dialect = get_dialect(card.dialect)
        if not dialect:
            return None
        return GenericBackend(card, dialect)
    except Exception:
        return None


def get_backend(source: str):
    """取平台后端：既有类优先 → 平台卡通用后端 → None。"""
    if not source:
        return None
    backend = _BACKENDS.get(source)
    if backend is not None:
        return backend
    return _generic_backend_for(source)


def list_webhook_providers() -> "list[tuple[str, str]]":
    """所有可用 webhook 后端 (provider, webhook_path)，供动态路由注册（D14/S17）。"""
    out = [(name, b.webhook_path) for name, b in _BACKENDS.items()]
    try:
        from .platform_card import list_vcs_platforms
        for provider in list_vcs_platforms():
            if provider in _BACKENDS:
                continue
            gb = _generic_backend_for(provider)
            if gb is not None:
                out.append((provider, gb.webhook_path))
    except Exception:
        pass
    return out


def normalize_event(raw, event_header, source: str = "gitcode") -> dict:
    """将平台专有 webhook 统一为内部事件格式。"""
    backend = get_backend(source) or _BACKENDS["gitcode"]
    return backend.normalize(raw, event_header)


def get_event_header(headers: dict) -> tuple[str, str]:
    """从 HTTP 头中提取事件类型和平台名称（大小写不敏感）。

    Returns:
        (event_type_header, source_name)
    """
    headers_lower = {k.lower(): v for k, v in headers.items()}
    for source, backend in ((s, get_backend(s)) for s, _ in list_webhook_providers()):
        if backend is None:
            continue
        header_value = headers_lower.get(backend.event_header_key.lower())
        if header_value:
            return header_value, source
    return headers_lower.get("x-gitcode-event", "unknown"), "gitcode"


def resolve_source_by_path(path: str) -> str:
    """根据 webhook 路径确定平台（含平台卡通用后端）。"""
    if path in _WEBHOOK_PATH_MAP:
        return _WEBHOOK_PATH_MAP[path]
    for provider, webhook_path in list_webhook_providers():
        if webhook_path == path:
            return provider
    return "gitcode"
