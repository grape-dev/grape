"""VCS 事件归一化适配器：按 webhook 路径自动分发。"""
from .gitcode import GitCodeBackend


_BACKENDS = {
    "gitcode": GitCodeBackend(),
}


def normalize_event(raw, event_header, source: str = "gitcode") -> dict:
    """将平台专有 webhook 统一为内部事件格式。

    Args:
        raw: bytes 或 str 的请求体
        event_header: 事件类型头（如 X-GitCode-Event）
        source: 平台名称，默认 gitcode

    Returns:
        {"source", "type", "repository", "branch", "actor", "payload"}
    """
    return _BACKENDS[source].normalize(raw, event_header)
