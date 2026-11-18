"""架构审查整改回归（PR #1697 第 1 轮：A13 / B2 / A8）。

三条被审查点名、且没有守卫就会静默退化的口径：

1. **A13（BLOCKED）**：企微 `disconnected_event` 被软处理（保留 socket、不重连）后不再走
   `except ChannelError` 路径 —— 若不补记归因，`/health/realtime` 的 im 段会显示 0 重连、
   无 flap 归因，"同一 bot 被别的会话持续顶掉"重新变成不可见的静默失效。
   本文件锁：渠道只暴露累计计数 → Supervisor 按增量并入既有 `flap_by_cause["takeover"]` 桶。

2. **B2（Critical）**：新增阈值不得是裸数字 —— 两个后端阈值必须可由环境变量覆盖，
   否则线上想按实测调参只能改代码。

3. **A8（Medium）**：装配层取渠道必须走 `Supervisor.channel_for()` 公开方法 + `Channel.send()`，
   不得再读 `_channels[...][0]._sender` 私有属性。
"""
import asyncio
import time

from src.domains.runtime.im.channel.channel import (
    CancelContext, Channel, ChannelConfig, SendResult,
)
from src.domains.runtime.im.channel.registry import ChannelRegistry
from src.domains.runtime.im.channel.types import Scope, Type
from src.domains.runtime.im.engine.metrics import EngineMetrics, FLAP_CAUSES
from src.domains.runtime.im.engine.supervisor import Supervisor


class _TakeoverChannel(Channel):
    """只暴露累计接管计数的渠道替身（企微的最小形状）。"""

    def __init__(self):
        self.takeover_count = 0
        self.sent = []

    def type(self) -> Type:
        return Type.WECOM

    async def connect(self, ctx: CancelContext) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, out):
        self.sent.append(out)
        return SendResult(ok=True, message_id="mid-1")


async def _async_noop():
    """Supervisor 期望的 provider 形状（协程返回安装列表）。"""
    return []


def _supervisor(metrics=None):
    return Supervisor(registry=ChannelRegistry(), instance_id="i1",
                      installation_provider=_async_noop, scan_interval=3600.0,
                      metrics=metrics)


def test_takeover_cause_is_a_first_class_flap_bucket():
    assert "takeover" in FLAP_CAUSES
    snap = EngineMetrics().snapshot()
    assert snap["flap_by_cause"]["takeover"] == 0


def test_supervisor_merges_channel_takeover_delta_into_flap_bucket():
    metrics = EngineMetrics()
    sup = _supervisor(metrics)
    ch = _TakeoverChannel()
    sup._channels["inst-1"] = (ch, CancelContext())

    ch.takeover_count = 2
    sup._sync_channel_counters()
    assert metrics.flap_by_cause["takeover"] == 2
    # 不重复计（按增量）
    sup._sync_channel_counters()
    assert metrics.flap_by_cause["takeover"] == 2
    # 渠道重建后计数归零，再被顶一次 = 1 次增量
    ch.takeover_count = 1
    sup._sync_channel_counters()
    assert metrics.flap_by_cause["takeover"] == 3


def test_sync_without_metrics_is_noop():
    sup = _supervisor(None)
    ch = _TakeoverChannel()
    sup._channels["inst-1"] = (ch, CancelContext())
    ch.takeover_count = 5
    sup._sync_channel_counters()      # 不抛、不建 metrics


def test_channel_for_is_public_and_returns_channel():
    sup = _supervisor()
    ch = _TakeoverChannel()
    sup._channels["inst-9"] = (ch, CancelContext())
    assert sup.channel_for("inst-9") is ch
    assert sup.channel_for("missing") is None
    channel = sup.channel_for("inst-9")
    assert channel is not None
    assert asyncio.run(channel.send(None)).ok is True


def test_wecom_connect_wait_is_configurable(monkeypatch):
    import importlib
    from src.domains.runtime.im.adapters.wecom import outbound as ob

    monkeypatch.setenv("GRAPE_WECOM_CONNECT_WAIT_SECS", "3.5")
    reloaded = importlib.reload(ob)
    try:
        assert reloaded.CONNECT_WAIT_SECS == 3.5
        assert reloaded.WecomTransport("bot").connect_wait == 3.5
    finally:
        monkeypatch.delenv("GRAPE_WECOM_CONNECT_WAIT_SECS", raising=False)
        importlib.reload(ob)


def test_wecom_connect_wait_falls_back_on_bad_env(monkeypatch):
    import importlib
    from src.domains.runtime.im.adapters.wecom import outbound as ob

    for bad in ("", "abc", "-1", "0"):
        monkeypatch.setenv("GRAPE_WECOM_CONNECT_WAIT_SECS", bad)
        reloaded = importlib.reload(ob)
        assert reloaded.CONNECT_WAIT_SECS == 8.0, bad
    monkeypatch.delenv("GRAPE_WECOM_CONNECT_WAIT_SECS", raising=False)
    importlib.reload(ob)


def test_chat_orphan_timeout_is_configurable(monkeypatch):
    from src.domains.runtime.routes import chat as chat_routes

    monkeypatch.setenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", "60")
    assert chat_routes._orphan_timeout_secs() == 60.0
    monkeypatch.delenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", raising=False)
    assert chat_routes._orphan_timeout_secs() == 30 * 60.0
    monkeypatch.setenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", "nope")
    assert chat_routes._orphan_timeout_secs() == 30 * 60.0
    monkeypatch.delenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", raising=False)


def test_orphan_turn_uses_configured_timeout_in_response(monkeypatch):
    """阈值可配时必须真正作用到 /status 的判定与文案（不是只存了个变量）。"""
    import datetime as _dt
    from src.domains.runtime.routes import chat as chat_routes

    def _ts(minutes_ago):
        t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
        return t.strftime("%Y-%m-%d %H:%M:%S")

    rows = [(1, "user", 1, "hi", "conv-1", _ts(10))]
    monkeypatch.setattr(chat_routes._rt, "read_all_any",
                        lambda name, params: rows if name == "messages_by_task" else [])
    monkeypatch.setattr("src.api.chat_task_manager.ChatTaskManager.get_status",
                        staticmethod(lambda task_id: None), raising=False)
    chat_routes._chat_tasks.clear()

    # 默认 30min：10 分钟前的孤儿轮次仍算 running
    assert chat_routes.chat_task_status("t_orphan_1", user={"id": 1})["status"] == "running"
    # 配置成 5min：同样一行变成"已中断"
    monkeypatch.setenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", "300")
    res = chat_routes.chat_task_status("t_orphan_1", user={"id": 1})
    assert res["status"] == "error"
    assert "5 分钟" in res["error"]
    monkeypatch.delenv("GRAPE_CHAT_ORPHAN_TURN_TIMEOUT_SECS", raising=False)


def test_engine_wiring_has_no_private_sender_penetration():
    """A8：装配层不得再读 supervisor._channels / channel._sender 私有属性。"""
    from pathlib import Path
    src = Path(__file__).resolve().parents[3] / "src" / "domains" / "runtime" / "im" / "engine_wiring.py"
    text = src.read_text(encoding="utf-8")
    assert '"_sender"' not in text
    assert "supervisor, \"_channels\"" not in text
