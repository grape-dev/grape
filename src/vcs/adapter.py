"""VCS 事件归一化适配器：按 webhook 路径自动分发。"""
from .gitcode import GitCodeBackend


_BACKENDS = {
    "gitcode": GitCodeBackend(),
}

_WEBHOOK_PATH_MAP = {
    "/webhook/gitcode": "gitcode",
}


def normalize_event(raw, event_header, source: str = "gitcode") -> dict:
    """将平台专有 webhook 统一为内部事件格式。"""
    return _BACKENDS[source].normalize(raw, event_header)


def get_event_header(headers: dict) -> tuple[str, str]:
    """从 HTTP 头中提取事件类型和平台名称。

    Returns:
        (event_type_header, source_name)
    """
    for source, backend in _BACKENDS.items():
        header_value = headers.get(backend.event_header_key)
        if header_value:
            return header_value, source
    return headers.get("X-GitCode-Event", "unknown"), "gitcode"


def resolve_source_by_path(path: str) -> str:
    """根据 webhook 路径确定平台。"""
    return _WEBHOOK_PATH_MAP.get(path, "gitcode")
