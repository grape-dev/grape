"""IM 通道抽象基类 — 支持飞书/企业微信/钉钉等即时通讯平台扩展"""
from abc import ABC, abstractmethod


class IMChannel(ABC):
    """IM 通道基类，所有 IM 平台实现需继承此类。"""
    name = ""  # 唯一标识符，如 "feishu", "wecom", "dingtalk"
    display_name = ""  # 前端展示名，如 "飞书", "企业微信", "钉钉"

    @abstractmethod
    def send_message(self, target: str, text: str, msg_type: str = "text") -> bool:
        """发送消息到指定目标（群聊或用户）"""
        ...

    @abstractmethod
    def send_card(self, target: str, title: str, content: str, actions: list = None) -> bool:
        """发送卡片消息"""
        ...

    @abstractmethod
    def get_channel_info(self) -> dict:
        """返回通道状态信息，用于前端配置页展示"""
        ...

    def validate_config(self, config: dict) -> tuple[bool, str]:
        """验证通道配置有效性，返回 (is_valid, error_message)"""
        return True, ""
