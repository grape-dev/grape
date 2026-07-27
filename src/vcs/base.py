"""VCS 后端抽象基类"""
import json


class VcsBackend:
    """VCS 后端基类，所有版本控制平台实现需继承此类。"""
    name = ""
    webhook_path = ""
    event_header_key = ""

    def normalize(self, raw, event_header) -> dict:
        """将平台专有 webhook → 统一事件格式。

        返回:
            {"source": str, "type": str, "repository": str,
             "branch": str, "actor": str, "payload": dict}
        """
        raise NotImplementedError

    def is_raw_bytes(self, raw) -> bool:
        return isinstance(raw, bytes)

    def decode_payload(self, raw) -> dict:
        """将 bytes/str 统一解析为 dict。"""
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
            if "\ufffd" in text:
                try:
                    text = raw.decode("gbk")
                except Exception:
                    text = raw.decode("utf-8", errors="replace")
            return json.loads(text)
        elif isinstance(raw, str):
            return json.loads(raw)
        return raw
