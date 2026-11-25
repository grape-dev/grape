"""引擎级健康计数器（Issue #870：/health/realtime im 段 legacy 字段对齐）。

计数点全部落在既有钩子上（入站分发 / 发送器结果 / 连接重连 / 异常分类），
仅 O(1) 累加，不落库、不改消息链路语义。

字段名与 legacy BotManager.health_check（feishu_async.py L1902-1913）对齐；
`reconnects` 为 Spec 命名，与 legacy 键 `total_reconnects` 同值双发，
保证两态（engine=on/off）字段集合一致且看板零改动兼容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 与 legacy 断开归因三分类一致（cluster-im-lease-stability-design.md §2.5）
# 2026-11-18 架构审查 A13：企微 disconnected_event（同一 bot 被新会话顶掉的会话接管事件）
# 被软处理后不再走 ChannelError 路径，必须在这里留一个归因桶，否则"被抢连接"从看板上
# 彻底消失（修复前正是靠这一项 13 次/10 分钟才发现问题）。
# Issue #3698：飞书 3001（同 app 并发注册上限）单独归因——与普通链路抖动分开，
# 便于看板/告警区分"网络断了"与"连接数超限被拒登"。
FLAP_CAUSES = (
    "lease", "ws_transport", "shutdown", "ilink_longpoll", "takeover",
    "ws_register_conflict",
)

# Issue #1095：卡片降级归因（/health/realtime im 段 degraded_by_reason）
# Issue #1728：微信渠道媒体/入站/风控降级归因
DEGRADED_REASONS = (
    "card_text_fallback", "card_action_unsupported", "card_empty_guard",
    "media_download_failed", "media_upload_failed", "inbound_empty",
    "session_paused_risk",
)

# Issue #3795：WS 关闭码归因键族（`ws_close_<code>`）。
# flap_by_cause 是常驻字典，键必须有界：应用自定义码（4000+，各渠道自定义语义）
# 统一进 `ws_close_4xxx` 桶，取不到码进 `ws_close_unknown`，避免看板键无界增长。
WS_CLOSE_APP_CODE_FLOOR = 4000


def ws_close_cause(code: Any) -> str:
    """关闭码 → 有界的归因键（数字码原样，应用自定义码分桶，无码 unknown）。"""
    if isinstance(code, bool) or not isinstance(code, int):
        return "ws_close_unknown"
    if code >= WS_CLOSE_APP_CODE_FLOOR:
        return "ws_close_4xxx"
    return f"ws_close_{code}"


@dataclass
class EngineMetrics:
    """新引擎健康计数器（engine=on 态的 im 段数据源）。"""

    messages_received: int = 0
    messages_sent: int = 0
    messages_failed: int = 0
    messages_degraded: int = 0   # Issue #1008：能力位不足降级发送的出站数（未丢内容）
    reconnects: int = 0
    flap_by_cause: dict = field(
        default_factory=lambda: {cause: 0 for cause in FLAP_CAUSES})
    degraded_by_reason: dict = field(
        default_factory=lambda: {reason: 0 for reason in DEGRADED_REASONS})
    # Issue #3799：渠道风控暂停（ret=-14）按安装维度的暂停次数与告警次数。
    # `session_paused_risk` 的实例维度展开——看板/告警按 instance 分桶，避免
    # 「同一时刻只有总数、看不出是哪台/哪个渠道被风控」。
    channel_paused_by_instance: dict = field(default_factory=dict)
    channel_paused_alerts: int = 0

    def record_received(self) -> None:
        self.messages_received += 1

    def record_sent(self) -> None:
        self.messages_sent += 1

    def record_failed(self) -> None:
        self.messages_failed += 1

    def record_degraded(self, reason: str = "") -> None:
        """出站降级（Issue #1008）：能力位缺失 → 文本/追加降级，内容未丢失。

        Issue #1095：reason 非空时按原因记 degraded_by_reason（未知原因按需建键）。
        """
        self.messages_degraded += 1
        if reason:
            self.degraded_by_reason[reason] = self.degraded_by_reason.get(reason, 0) + 1

    def record_reconnect(self, cause: str) -> None:
        """链路断开 → 将触发退避重连：reconnects +1 并按原因记抖动。"""
        self.reconnects += 1
        self._bump(cause)

    def record_disconnect(self, cause: str) -> None:
        """断开不重连（让渡/停机）：仅按原因记抖动。"""
        self._bump(cause)

    def record_ws_close(self, close_code: Any) -> str:
        """Issue #3795：按 WS 关闭码记一次抖动归因（键有界，见 ``ws_close_cause``）。

        关闭同时已被记一次 ``ws_transport``（reconnects 口径不变），这里只补关闭码
        维度，供看板区分 3003 ping timeout / 1006 异常断连 / 4000+ 应用自定义码。
        """
        cause = ws_close_cause(close_code)
        self._bump(cause)
        return cause

    def _bump(self, cause: str) -> None:
        self.flap_by_cause[cause] = self.flap_by_cause.get(cause, 0) + 1

    def record_channel_paused(self, installation_id: str = "") -> None:
        """Issue #3799：渠道被风控暂停一次 → 按安装维度 +1（`session_paused_risk`）。

        注意：`degraded_by_reason["session_paused_risk"]` 由渠道侧
        (`channel._record_degraded`) 记一次，这里不重复累加，避免同一暂停双计。
        """
        key = str(installation_id or "unknown")
        self.channel_paused_by_instance[key] = \
            self.channel_paused_by_instance.get(key, 0) + 1

    def record_channel_alert(self, rule: str = "") -> None:
        """Issue #3799：渠道不可用告警触发一次（按规则名汇总，当前只有一条规则）。"""
        self.channel_paused_alerts += 1

    def snapshot(self) -> dict:
        """legacy 对齐键集合；缺失语义 = 0（不省略键）。"""
        return {
            "total_messages_received": self.messages_received,
            "total_messages_sent": self.messages_sent,
            "total_messages_failed": self.messages_failed,
            "total_messages_degraded": self.messages_degraded,   # Issue #1008：出站降级计数
            "reconnects": self.reconnects,
            "total_reconnects": self.reconnects,   # legacy 同名键（双发兼容）
            "flap_by_cause": dict(self.flap_by_cause),
            "degraded_by_reason": dict(self.degraded_by_reason),   # Issue #1095
            # Issue #3799：渠道风控暂停（ret=-14）实例维度 + 告警计数
            "channel_paused_by_instance": dict(self.channel_paused_by_instance),
            "channel_paused_alerts": self.channel_paused_alerts,
        }


class CountingSender:
    """发送器结果钩子（Issue #870）：ok → sent，失败/异常 → failed。

    透明包装 ChannelSender 协议（send + capabilities），
    Replier / channel_builder 感知不到它的存在。
    """

    def __init__(self, inner: Any, metrics: EngineMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def capabilities(self):
        return self._inner.capabilities()

    async def send(self, out):
        try:
            result = await self._inner.send(out)
        except Exception:
            self._metrics.record_failed()
            raise
        if result is not None and result.ok:
            self._metrics.record_sent()
        else:
            self._metrics.record_failed()
        return result


__all__ = ["EngineMetrics", "CountingSender", "FLAP_CAUSES", "DEGRADED_REASONS",
           "ws_close_cause"]
