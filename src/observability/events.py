"""进程内事件总线（参考 multica events/bus.go）。

同步 pub/sub + panic recovery，用于解耦通知/指标记录/活动记录。

使用方式：
    from src.observability.events import bus

    # 订阅
    bus.subscribe("task:completed", lambda e: print(e))

    # 发布
    bus.publish(Event(type="task:completed", task_id="t_123", payload={"status": "ok"}))
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("devops-bot")


@dataclass
class Event:
    """领域事件。

    type: 事件类型（如 "task:created", "task:completed", "runtime:registered"）
    task_id: 关联的任务 ID（可选）
    payload: JSON 可序列化的负载
    """
    type: str
    task_id: str = ""
    payload: Any = None


Handler = Callable[[Event], None]


class EventBus:
    """同步事件总线。

    - type-specific handlers 先执行
    - global handlers (subscribe_all) 后执行
    - 单个 handler panic 不影响其他 handler（参考 multica events/bus.go Publish 的 recover）
    """

    def __init__(self):
        self._listeners: dict[str, list[Handler]] = {}
        self._globals: list[Handler] = []

    def subscribe(self, event_type: str, handler: Handler):
        """订阅特定类型的事件。"""
        self._listeners.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Handler):
        """订阅所有事件。global handlers 在 type-specific handlers 之后执行。"""
        self._globals.append(handler)

    def publish(self, event: Event):
        """同步发布事件。handlers 按注册顺序执行，panic 被 recover。"""
        # type-specific handlers
        for h in self._listeners.get(event.type, []):
            self._safe_call(h, event)
        # global handlers
        for h in self._globals:
            self._safe_call(h, event)

    def _safe_call(self, handler: Handler, event: Event):
        try:
            handler(event)
        except Exception as e:
            log.warning(f"event bus handler error: type={event.type} error={e}")


# 全局单例
bus = EventBus()
