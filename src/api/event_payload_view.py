"""事件 payload 对外投影（Issue #996）。

events.payload 可能包含完整 webhook 体，接口层只需 files / guard_impact
两个视图。集中此处，避免 event_store 与 routes/issues 各写一份 JSON 解析。
"""
import json

from src.config import log


def payload_view(payload) -> dict:
    """从 payload dict 提取 files / guard_impact；其余字段不外泄。"""
    if not isinstance(payload, dict):
        return {}
    view = {}
    if payload.get("files"):
        view["files"] = payload["files"]
    if payload.get("guard_impact"):
        view["guard_impact"] = payload["guard_impact"]
    return view


def payload_view_from_json(raw, seq=None) -> dict:
    """解析 payload JSON 文本并投影；解析失败返回空视图并记录日志。"""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception as e:
        log.warning(f"event payload view: parse failed seq={seq}: {e}")
        return {}
    return payload_view(payload)