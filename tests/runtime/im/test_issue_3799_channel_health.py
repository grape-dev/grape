"""Issue #3799：微信渠道 ret=-14 风控暂停 —— 状态机 / 告警 / 日志聚合 / 降级 / 恢复入口。

对照 Issue #3799 Spec 的验收标准：
  1. 渠道健康状态持久化（paused / last_pause_at / next_retry_at），管理台/API 可读；
  2. `session_paused_risk` 告警：同实例连续 ≥2 次（距上次暂停 ≤2h）或全实例 1h 均暂停；
  3. `notifystart/notifystop` 失败日志按 (instance, reason, 小时窗口) 聚合，窗口内只 1 条汇总；
  4. 暂停期间通知降级：不投递 paused 渠道，记降级结果；
  5. 人工 retry_now / relogin / clear_pause 入口，重试成功则 notifystart 重跑、getupdates 恢复。
"""
import asyncio
import json
import sqlite3
import uuid

import pytest

from src.domains.runtime.im import engine_wiring
from src.domains.runtime.im.channel.channel import CancelContext, ChannelConfig
from src.domains.runtime.im.channel.types import Type


# ---------------------------------------------------------------- helpers

class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _make_db(tmp_path, monkeypatch, name="health.db") -> str:
    """独立 sqlite 库：跑启动迁移（建出 grape_t_channel_health）。"""
    path = str(tmp_path / name)
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    engine_wiring._reset_backend_cache()
    engine_wiring.run_startup_migration(path)
    return path


@pytest.fixture()
def health_db(tmp_path, monkeypatch):
    from src.domains.runtime.im import channel_health
    path = _make_db(tmp_path, monkeypatch)
    channel_health._reset_for_tests()
    yield path
    channel_health._reset_for_tests()
    engine_wiring._reset_backend_cache()


def _store(path):
    from src.domains.runtime.im.channel_health import ChannelHealthStore
    return ChannelHealthStore(conn_factory=lambda: sqlite3.connect(path))


class FakeMetrics:
    def __init__(self):
        self.reasons = []
        self.channel_paused = {}

    def record_degraded(self, reason=""):
        self.reasons.append(reason)

    def record_channel_paused(self, installation_id=""):
        self.channel_paused[installation_id] = self.channel_paused.get(installation_id, 0) + 1


# ---------------------------------------------------------------- 1. 状态机

def test_record_pause_persists_state(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    state = tracker.record_pause("inst-1", "wechat", reason="ret=-14",
                                 next_retry_at=clock.now + 3600)
    assert state["paused"] is True
    assert state["consecutive_pauses"] == 1
    assert state["alert_state"] == "pending"          # 首次暂停只记事件，不升级告警
    assert state["last_pause_at"]                      # ISO 时间串（管理台可见）
    assert state["next_retry_at"]

    reloaded = _store(health_db).get("inst-1")
    assert reloaded is not None
    assert reloaded.paused is True
    assert reloaded.consecutive_pauses == 1
    assert reloaded.instance_id == "host-1"
    assert reloaded.last_pause_reason == "ret=-14"


def test_second_pause_within_window_fires_alert(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    alerts = []
    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock, alert_sink=lambda state, reason: alerts.append(
                                       (state["installation_id"], reason)))
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    clock.advance(3600)                                  # 小时级循环 < 2h 窗口
    state = tracker.record_pause("inst-1", "wechat", reason="ret=-14")

    assert state["consecutive_pauses"] == 2
    assert state["alert_state"] == "firing"
    assert "consecutive" in state["alert_reason"]
    assert alerts and alerts[0][0] == "inst-1"
    assert _store(health_db).get("inst-1").alert_state == "firing"


def test_pause_after_long_recovery_resets_consecutive(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    clock.advance(3 * 3600)                              # 超过 2h 重复窗口
    state = tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    assert state["consecutive_pauses"] == 1
    assert state["alert_state"] == "pending"


def test_all_instances_paused_within_window_alerts(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    alerts = []
    clock = FakeClock()
    tracker = ChannelHealthTracker(
        store=_store(health_db), instance_id="host-1", clock=clock,
        installations_provider=lambda: [{"installation_id": "inst-1", "channel_type": "wechat",
                                         "status": "active"},
                                        {"installation_id": "inst-2", "channel_type": "wechat",
                                         "status": "active"}],
        alert_sink=lambda state, reason: alerts.append((state["installation_id"], reason)))
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    clock.advance(120)
    state = tracker.record_pause("inst-2", "wechat", reason="ret=-14")
    assert state["alert_state"] == "firing"
    assert "all_instances_paused" in state["alert_reason"]
    assert alerts and alerts[0][0] == "inst-2"


def test_record_resume_clears_pause_and_resolves_alert(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    clock.advance(60)
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    assert tracker.is_paused("inst-1") is True

    clock.advance(60)
    state = tracker.record_resume("inst-1", "wechat")
    assert state is not None
    assert state["paused"] is False
    assert state["alert_state"] == "resolved"
    # consecutive_pauses = repeat 窗口（2h）内暂停次数，恢复不清零（否则小时级循环永不告警），
    # 只有窗口外的长健康期才归零。
    assert state["consecutive_pauses"] >= 1
    assert state["last_resume_at"]
    assert tracker.is_paused("inst-1") is False
    # 未暂停时再报恢复 → 不写库、不刷日志
    assert tracker.record_resume("inst-1", "wechat") is None


def test_stale_pause_row_not_treated_as_paused(health_db):
    """进程崩溃留下的 paused 行超过 TTL 后不再当成暂停（否则通知永远被降级）。"""
    from src.domains.runtime.im.channel_health import (
        PAUSE_STATE_TTL_SECONDS, ChannelHealthTracker)

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    clock.advance(PAUSE_STATE_TTL_SECONDS + 1)
    assert tracker.is_paused("inst-1") is False
    assert "inst-1" not in tracker.paused_ids()


def test_clear_pause_keeps_connection_but_resets_state(health_db):
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    state = tracker.clear_pause("inst-1", "wechat")
    assert state["paused"] is False
    assert state["last_recover_action"] == "clear_pause"
    assert state["alert_state"] == "resolved"


def test_alert_emits_searchable_error_log(health_db, caplog):
    """告警必须可检索：ERROR 行带 alert=<rule>（Loki/日志告警共用同一判据）。"""
    from src.domains.runtime.im.channel_health import ALERT_RULE, ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    with caplog.at_level("INFO"):
        tracker.record_pause("inst-1", "wechat", reason="ret=-14")
        clock.advance(3600)
        tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "第二次暂停未升级为 ERROR 告警"
    assert any(f"alert={ALERT_RULE}" in r.getMessage() for r in errors)
    assert any("wechat_channel_unavailable" in r.getMessage() for r in errors)
    # 首次暂停只是事件（WARNING），不冒充告警
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("alert=pending" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------- 2. 日志聚合

def test_hourly_log_aggregator_one_summary_per_window():
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    clock = FakeClock()
    agg = HourlyLogAggregator(window_seconds=3600, clock=clock)
    assert agg.note("inst-1", "notifystart", detail="boom") is None      # 同窗口内不刷屏
    assert agg.note("inst-1", "notifystart", detail="boom") is None
    assert agg.note("inst-1", "notifystart", detail="boom") is None
    clock.advance(3600)
    summary = agg.note("inst-1", "notifystart", detail="boom")
    assert summary is not None
    assert summary["instance"] == "inst-1"
    assert summary["reason"] == "notifystart"
    assert summary["count"] == 3
    assert summary["window_start"]
    # 不同 reason / 不同实例各自独立窗口
    assert agg.note("inst-1", "notifystop") is None


def test_hourly_log_aggregator_flush_reports_open_window():
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    clock = FakeClock()
    agg = HourlyLogAggregator(window_seconds=3600, clock=clock)
    agg.note("inst-1", "notifystop", detail="x")
    agg.note("inst-1", "notifystop", detail="x")
    flushed = agg.flush()
    assert len(flushed) == 1 and flushed[0]["count"] == 2
    assert agg.flush() == []           # 清空后不重复输出


# ---------------------------------------------------------------- 3. 渠道状态机

class FakeSender:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def capabilities(self):
        from src.domains.runtime.im.adapters.wechat.outbound import WECHAT_CAPABILITIES
        return WECHAT_CAPABILITIES

    async def send(self, out):
        from src.domains.runtime.im.channel.message import SendResult
        return SendResult(ok=True)

    async def send_typing(self, chat_id, status=1):
        return True

    async def notify_start(self):
        self.started += 1

    async def notify_stop(self):
        self.stopped += 1


class StopLoop(Exception):
    pass


_INSTALL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _cfg():
    return ChannelConfig(type=Type.WECHAT, raw={"bot_token": "bt-1", "app_id": "ib-1"})


def test_channel_pause_records_health_and_resume_clears_it(health_db):
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
    from src.domains.runtime.im.adapters.wechat.protocol import WechatApiError
    from src.domains.runtime.im.channel.channel import ChannelError
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    metrics = FakeMetrics()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock, metrics=metrics)

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                raise WechatApiError("paused", ret=-14)
            if self.calls == 2:
                return {"msgs": [], "get_updates_buf": "c-2"}
            raise StopLoop()

    sender = FakeSender()
    ch = WechatChannel(_cfg(), sender, update_fetcher=Fetcher(), metrics=metrics,
                       health=tracker, pause_seconds=0.01)

    asyncio.run(ch.connect(CancelContext()))     # 暂停后自然结束连接（不抛 ChannelError）

    assert ch.session_paused == 1
    assert "session_paused_risk" in metrics.reasons
    assert metrics.channel_paused                        # per-instance 计数
    assert tracker.is_paused(ch.health_key) is True
    assert sender.stopped >= 1


def test_channel_pause_then_successful_poll_resumes(health_db):
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
    from src.domains.runtime.im.adapters.wechat.protocol import WechatApiError
    from src.domains.runtime.im.channel.channel import ChannelError
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    cfg = ChannelConfig(type=Type.WECHAT, raw={"bot_token": "bt-1", "app_id": "ib-1"},
                        installation_id=_INSTALL_ID)

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                raise WechatApiError("paused", ret=-14)
            if self.calls == 2:
                return {"msgs": [], "get_updates_buf": "c-2"}
            raise StopLoop()

    ch = WechatChannel(cfg, FakeSender(), update_fetcher=Fetcher(), health=tracker,
                       pause_seconds=0.01)
    inst = str(cfg.installation_id)
    # 第一轮：暂停后 connect 返回（supervisor 会重连）
    asyncio.run(ch.connect(CancelContext()))
    assert tracker.is_paused(inst) is True
    # 模拟重连：首个成功轮询即清除 paused（getupdates 恢复）
    with pytest.raises(ChannelError):
        asyncio.run(ch.connect(CancelContext()))
    assert tracker.is_paused(inst) is False


def test_request_retry_wakes_pause_sleep_and_reissues_notifystart(health_db):
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
    from src.domains.runtime.im.adapters.wechat.protocol import WechatApiError
    from src.domains.runtime.im.channel.channel import ChannelError
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    clock = FakeClock()
    tracker = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                   clock=clock)
    cfg = ChannelConfig(type=Type.WECHAT, raw={"bot_token": "bt-1", "app_id": "ib-1"},
                        installation_id=_INSTALL_ID)

    class Fetcher:
        def __init__(self):
            self.calls = 0

        async def __call__(self, cursor, timeout):
            self.calls += 1
            if self.calls == 1:
                raise WechatApiError("paused", ret=-14)
            if self.calls == 2:
                return {"msgs": [], "get_updates_buf": "c-2"}
            raise StopLoop()

    sender = FakeSender()
    ch = WechatChannel(cfg, sender, update_fetcher=Fetcher(), health=tracker,
                       pause_seconds=3600)          # 默认 1h 暂停
    inst = str(cfg.installation_id)

    async def run():
        task = asyncio.ensure_future(ch.connect(CancelContext()))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ch.session_paused:
                break
        assert ch.session_paused == 1
        assert ch.request_retry() is True           # 人工 retry_now 唤醒
        with pytest.raises(ChannelError):           # 唤醒后继续轮询，第三次 StopLoop 抛错
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert sender.started == 2                      # 重试后 notifystart 重新执行
    assert tracker.is_paused(inst) is False         # getupdates 恢复 → paused 清除


def test_notify_failure_logs_aggregated_per_window(health_db, caplog):
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    clock = FakeClock()
    agg = HourlyLogAggregator(window_seconds=3600, clock=clock)

    class BadNotifier:
        async def notify_start(self):
            raise RuntimeError("notifystart boom")

        async def notify_stop(self):
            raise RuntimeError("notifystop boom")

    cfg = ChannelConfig(type=Type.WECHAT, raw={"bot_token": "bt-1", "app_id": "ib-1"},
                        installation_id=_INSTALL_ID)
    ch = WechatChannel(cfg, FakeSender(), client=BadNotifier(), log_agg=agg)

    async def run():
        for _ in range(3):
            await ch._notify_start()
            await ch._notify_stop()

    with caplog.at_level("INFO"):
        asyncio.run(run())
    lines = [r for r in caplog.text.splitlines() if "notifystart/notifystop" in r]
    assert len(lines) <= 1, f"同窗口内日志未聚合: {lines}"
    summary = agg.flush()
    assert {(s["reason"], s["count"]) for s in summary} == {("notifystart", 3), ("notifystop", 3)}


def test_sender_notify_failure_logs_aggregated(health_db, caplog):
    from src.domains.runtime.im.adapters.wechat.outbound import WechatSender
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    class BadClient:
        async def notify_start(self):
            raise RuntimeError("boom")

        async def notify_stop(self):
            raise RuntimeError("boom")

    agg = HourlyLogAggregator(window_seconds=3600, clock=FakeClock())
    sender = WechatSender(BadClient(), log_agg=agg)

    async def run():
        for _ in range(3):
            await sender.notify_start()
            await sender.notify_stop()

    with caplog.at_level("INFO"):
        asyncio.run(run())
    lines = [r for r in caplog.text.splitlines() if "notifystart/notifystop" in r]
    assert len(lines) <= 1
    assert {s["count"] for s in agg.flush()} == {3}


# ---------------------------------------------------------------- 4. 指标

def test_engine_metrics_counts_channel_paused():
    from src.domains.runtime.im.engine.metrics import EngineMetrics

    m = EngineMetrics()
    m.record_channel_paused("inst-1")
    m.record_channel_paused("inst-1")
    snap = m.snapshot()
    assert snap["channel_paused_by_instance"]["inst-1"] == 2
    assert "channel_paused_alerts" in snap


# ---------------------------------------------------------------- 5. 通知降级

@pytest.fixture()
def outbox_db(tmp_path, monkeypatch):
    from src.domains.runtime.im import channel_health
    path = _make_db(tmp_path, monkeypatch, name="outbox.db")
    channel_health._reset_for_tests()
    yield path
    channel_health._reset_for_tests()
    engine_wiring._reset_backend_cache()


def test_notify_user_skips_paused_channel_and_records_degradation(outbox_db):
    from src.domains.runtime.im import notify as notify_mod
    from src.domains.runtime.im.channel_health import ChannelHealthTracker
    from tests.runtime.im.conftest import FakeChannel, FakeEngine

    eng = FakeEngine({"inst-wx": FakeChannel(), "inst-fs": FakeChannel()})
    tracker = ChannelHealthTracker(store=_store(outbox_db), instance_id="host-1",
                                   clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    eng.health_tracker = tracker

    targets = [{"installation_id": "inst-wx", "channel_type": "wechat", "chat_id": "chat-wx"},
               {"installation_id": "inst-fs", "channel_type": "feishu", "chat_id": "chat-fs"}]
    notify_mod._reset_for_tests()
    res = asyncio.run(notify_mod.notify_user(1, "task", "hello", targets=targets,
                                             engine=eng, dedupe=False))
    assert res["sent"] == ["feishu"]                 # 暂停渠道不投递，由健康渠道送达
    assert res["skipped"]["wechat"].startswith("channel_paused")
    state = tracker.state("inst-wx")
    assert state["degraded_count"] == 1
    assert state["last_degraded_to"] == "feishu"
    assert state["last_degraded_ok"] is True


def test_notify_user_paused_without_fallback_is_recorded(outbox_db):
    from src.domains.runtime.im import notify as notify_mod
    from src.domains.runtime.im.channel_health import ChannelHealthTracker
    from src.domains.runtime.im import outbox as OB
    from tests.runtime.im.conftest import FakeChannel, FakeEngine

    eng = FakeEngine({"inst-wx": FakeChannel()})
    tracker = ChannelHealthTracker(store=_store(outbox_db), instance_id="host-1",
                                   clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    eng.health_tracker = tracker

    targets = [{"installation_id": "inst-wx", "channel_type": "wechat", "chat_id": "chat-wx"}]
    notify_mod._reset_for_tests()
    res = asyncio.run(notify_mod.notify_user(1, "task", "hello", targets=targets,
                                             engine=eng, dedupe=False))
    assert res["sent"] == []
    assert res["skipped"]["wechat"] == "channel_paused"
    state = tracker.state("inst-wx")
    assert state["degraded_count"] == 1
    assert state["last_degraded_to"] == ""
    assert state["last_degraded_ok"] is False
    # 内容不丢：outbox 行保持 pending，恢复后由消费者补投
    track = OB.out_track_id(1, "task", "", 0, "wechat", "inst-wx")
    row = OB.get_row(track)
    assert row is not None and row["status"] == "pending"


# ---------------------------------------------------------------- 6. 管理台 / 恢复入口

def test_channel_health_rows_exposes_state(outbox_db):
    from src.domains.runtime.im import channel_health

    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    rows = channel_health.health_rows()
    assert rows and rows[0]["installation_id"] == "inst-wx"
    assert rows[0]["paused"] is True
    assert rows[0]["last_pause_at"] and rows[0]["next_retry_at"]


def test_recover_channel_retry_now_wakes_channel(outbox_db):
    from src.domains.runtime.im import channel_health

    class Chan:
        retried = 0

        def request_retry(self) -> bool:
            self.retried += 1
            return True

    chan = Chan()

    class Sup:
        kicked = 0

        def channel_for(self, inst_id):
            return chan if inst_id == "inst-wx" else None

        def kick(self):
            Sup.kicked += 1

    class Eng:
        instance_id = "host-1"
        supervisor = Sup()

    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")

    res = channel_health.recover_channel("inst-wx", "retry_now", engine=Eng(), tracker=tracker)
    assert res["ok"] is True and res["action"] == "retry_now"
    assert chan.retried == 1
    state = tracker.state("inst-wx")
    assert state["last_recover_action"] == "retry_now"
    assert state["next_retry_at"]                    # 立即重试时间点可见

    res = channel_health.recover_channel("inst-wx", "relogin", engine=Eng(), tracker=tracker)
    assert res["ok"] is True and Sup.kicked == 1      # relogin 额外打断扫描窗口

    res = channel_health.recover_channel("inst-wx", "clear_pause", engine=Eng(), tracker=tracker)
    assert res["ok"] is True
    assert tracker.is_paused("inst-wx") is False


def test_recover_channel_without_engine_reports_unavailable(outbox_db):
    from src.domains.runtime.im import channel_health

    res = channel_health.recover_channel("inst-wx", "retry_now", engine=None, tracker=None)
    assert res["ok"] is False
    assert res["reason"] == "engine_unavailable"


def test_im_installations_include_health_fields(outbox_db, monkeypatch):
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import channel_health

    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, repo_key, config, status) "
        "VALUES (?,?,?,?,?,?,?)",
        ("inst-wx", "wechat", "user", "1234", "", json.dumps({"app_id": "ib-1"}), "active"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    monkeypatch.setattr(im_routes, "_connected_ids", lambda: set())
    monkeypatch.setattr(im_routes, "_lease_connected_owners", lambda: {})
    monkeypatch.setattr(im_routes, "_instance_id", lambda: "host-1")

    data = im_routes.im_installations(repo="", user={"id": "1234"})
    row = data["installations"][0]
    assert row["health"]["paused"] is True
    assert row["health"]["consecutive_pauses"] == 1
    assert row["paused"] is True
    assert row["last_pause_at"] and row["next_retry_at"]


def test_im_channel_health_endpoint(outbox_db, monkeypatch):
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import channel_health

    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, repo_key, config, status) "
        "VALUES (?,?,?,?,?,?,?)",
        ("inst-wx", "wechat", "user", "1234", "", json.dumps({"app_id": "ib-1"}), "active"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))

    data = im_routes.im_channel_health(user={"id": "1234"})
    assert data["total"] == 1 and data["paused"] == 1 and data["firing"] == 1
    assert data["channels"][0]["installation_id"] == "inst-wx"


def test_im_installation_recover_endpoint(outbox_db, monkeypatch):
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import channel_health

    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, repo_key, config, status) "
        "VALUES (?,?,?,?,?,?,?)",
        ("inst-wx", "wechat", "user", "1234", "", json.dumps({"app_id": "ib-1"}), "active"))
    conn.commit()
    conn.close()
    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")

    class Chan:
        def request_retry(self):
            return True

    class Sup:
        def channel_for(self, inst_id):
            return Chan()

        def kick(self):
            return True

    class Eng:
        instance_id = "host-1"
        supervisor = Sup()
        health_tracker = tracker

    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    monkeypatch.setattr(im_routes, "_engine", lambda: Eng())

    body = im_routes.RecoverBody(action="clear_pause")
    res = im_routes.im_installation_recover("inst-wx", body, None, user={"id": "1234"})
    assert res["ok"] is True and res["action"] == "clear_pause"
    assert res["health"]["paused"] is False

    body = im_routes.RecoverBody(action="bogus")
    with pytest.raises(Exception):
        im_routes.im_installation_recover("inst-wx", body, None, user={"id": "1234"})

# ---------------------------------------------------------------- 7. PR #1983 审查回归
#
# 审查意见（QA）：1 Critical（/channels/health 越权）+ Required（peer 状态 TTL 重读、
# flush 丢单次失败、outbox/recover 分支测试缺口）+ Optional（同类型降级误报、
# 健康渠道 retry_now 置闸滞留）。以下用例先红后绿。

def _insert_installation(db_path, inst_id, *, scope="user", owner="", repo_key=""):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, repo_key, config, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (inst_id, "wechat", scope, owner, repo_key, json.dumps({"app_id": "ib-1"}), "active"))
    conn.commit()
    conn.close()


def test_im_channel_health_scoped_to_visible_installations(outbox_db, monkeypatch):
    """Critical（PR #1983 审查）：健康面板只返回当前用户可见安装的行。

    user 轨按 owner_user_id 过滤、repo 轨按仓库访问权限过滤——与 /installations
    同口径；否则任意登录用户可见其他用户/仓库渠道的 installation_id/暂停时间/告警原因。
    """
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import channel_health

    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    for inst in ("inst-mine", "inst-other-user", "inst-repo-ok", "inst-repo-denied"):
        tracker.record_pause(inst, "wechat", reason="ret=-14")
        tracker.record_pause(inst, "wechat", reason="ret=-14")   # 升级 firing 便于断言
    _insert_installation(outbox_db, "inst-mine", scope="user", owner="1234")
    _insert_installation(outbox_db, "inst-other-user", scope="user", owner="999")
    _insert_installation(outbox_db, "inst-repo-ok", scope="repo", repo_key="gitcode:ok/repo")
    _insert_installation(outbox_db, "inst-repo-denied", scope="repo", repo_key="gitcode:no/repo")

    allowed = {"gitcode:ok/repo"}

    def fake_check(user, repo_key):
        if repo_key not in allowed:
            raise PermissionError("403")

    monkeypatch.setattr(im_routes, "check_repo_access", fake_check)
    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))

    data = im_routes.im_channel_health(user={"id": "1234"})
    ids = {c["installation_id"] for c in data["channels"]}
    assert ids == {"inst-mine", "inst-repo-ok"}, "健康面板泄露了不可见安装"
    assert data["total"] == 2 and data["paused"] == 2 and data["firing"] == 2


def test_im_outbound_scoped_to_visible_installations(outbox_db, monkeypatch):
    """Critical（PR #1983 审查）：/outbound 明细同样按可见安装裁剪。"""
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import outbox as OB

    _insert_installation(outbox_db, "inst-mine", scope="user", owner="1234")
    _insert_installation(outbox_db, "inst-other-user", scope="user", owner="999")
    for inst in ("inst-mine", "inst-other-user"):
        OB.enqueue(out_track_id=OB.out_track_id(1, "task", "", 0, "wechat", inst),
                   installation_id=inst, channel_type="wechat", channel_chat_id="c-1",
                   payload={"kind": "task", "text": "x", "user_id": 1},
                   expires_at=OB.new_expiry())

    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    data = im_routes.im_outbound(status="", limit=50, user={"id": "1234"})
    ids = {r.get("installation_id") for r in data["outbound"]}
    assert ids == {"inst-mine"}, "outbound 明细泄露了其他用户的投递行"


def test_alert_all_instances_rule_refreshes_peer_state_from_store(health_db):
    """Required（PR #1983 审查）：多主控下 peer 在本进程启动后才暂停 →
    全实例判定须按短 TTL 重读 store，否则长跑进程只看到启动快照、漏告警。"""
    from src.domains.runtime.im.channel_health import ChannelHealthTracker

    alerts = []
    clock = FakeClock()
    provider = lambda: [{"installation_id": "inst-1", "channel_type": "wechat",
                         "status": "active"},
                        {"installation_id": "inst-2", "channel_type": "wechat",
                         "status": "active"}]
    host1 = ChannelHealthTracker(store=_store(health_db), instance_id="host-1",
                                 clock=clock, installations_provider=provider,
                                 alert_sink=lambda s, r: alerts.append(r))
    host1.record_pause("inst-1", "wechat", reason="ret=-14")   # 本进程自己的暂停

    clock.advance(7210)          # 超出 2h repeat 窗口 → 后续 consecutive 归 1
    host2 = ChannelHealthTracker(store=_store(health_db), instance_id="host-2",
                                 clock=clock, installations_provider=provider,
                                 alert_sink=lambda s, r: alerts.append(r))
    host2.record_pause("inst-2", "wechat", reason="ret=-14")   # 另一主控刚暂停（1h 窗口内）

    state = host1.record_pause("inst-1", "wechat", reason="ret=-14")
    assert state["consecutive_pauses"] == 1        # 隔离 consecutive 规则（超窗口归 1）
    assert state["alert_state"] == "firing", "全实例规则未看到 store 里的 peer 暂停态"
    assert "all_instances_paused" in state["alert_reason"]


def test_hourly_log_aggregator_flush_outputs_single_failure():
    """Required（PR #1983 审查）：窗口内只失败 1 次也必须落日志，不得丢单次失败。"""
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    clock = FakeClock()
    agg = HourlyLogAggregator(window_seconds=3600, clock=clock)
    agg.note("inst-1", "notifystop", detail="x")
    flushed = agg.flush()
    assert len(flushed) == 1, "单次失败窗口被 flush 静默丢弃"
    assert flushed[0]["count"] == 1
    assert agg.flush() == []           # 清空后不重复输出


def test_wechat_sender_flush_outputs_pending_window(caplog):
    """Required（PR #1983 审查）：WechatSender 有 flush 调用点——
    渠道断开时把 sender 侧未滚动窗口的失败汇总落日志。"""
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
    from src.domains.runtime.im.adapters.wechat.outbound import WechatSender
    from src.domains.runtime.im.channel_health import HourlyLogAggregator

    class BadClient:
        async def notify_start(self):
            raise RuntimeError("boom")

        async def notify_stop(self):
            raise RuntimeError("boom")

    agg = HourlyLogAggregator(window_seconds=3600, clock=FakeClock())
    sender = WechatSender(BadClient(), log_agg=agg)
    cfg = ChannelConfig(type=Type.WECHAT, raw={"bot_token": "bt-1", "app_id": "ib-1"},
                        installation_id=_INSTALL_ID)
    ch = WechatChannel(cfg, sender, client=BadClient(), log_agg=HourlyLogAggregator(
        window_seconds=3600, clock=FakeClock()))

    with caplog.at_level("INFO"):
        asyncio.run(ch._notify_start())          # 经 channel 的 client → sender 均计数
        asyncio.run(ch.disconnect())             # 断开收尾：两套聚合器都要 flush
    assert sender._log_agg.pending("wechat-sender", "notifystart") == 0, \
        "disconnect 后 sender 聚合器仍有未输出窗口"
    summaries = [r.getMessage() for r in caplog.records
                 if "notifystart/notifystop" in r.getMessage() and "count=1" in r.getMessage()]
    assert summaries, "单次失败经 flush 后未落日志"


def test_outbox_consumer_defers_paused_channel_row(outbox_db):
    """Required（PR #1983 审查）：消费者分支——_deliver 返回 channel_paused 时
    行回 pending（release_sending）、attempt/退避不递增、不真正 send。"""
    from src.domains.runtime.im import outbox as OB
    from src.domains.runtime.im.channel_health import ChannelHealthTracker
    from tests.runtime.im.conftest import FakeChannel, FakeEngine

    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) "
        "VALUES ('inst-1','wechat','user','1','active')")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_lease "
        "(lease_key, channel_type, installation_id, lease_owner, updated_at) VALUES (?,?,?,?,?)",
        ("wechat:inst-1", "wechat", "inst-1", "owner-A", "2026-11-23 00:00:00"))
    conn.commit()
    conn.close()

    tracker = ChannelHealthTracker(store=_store(outbox_db), instance_id="owner-A",
                                   clock=FakeClock())
    tracker.record_pause("inst-1", "wechat", reason="ret=-14")
    ch = FakeChannel()
    eng = FakeEngine({"inst-1": ch})
    eng.health_tracker = tracker

    tid = OB.out_track_id(1, "task", "gitcode:o/r", 5, "wechat", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="wechat",
               channel_chat_id="wx_1",
               payload={"kind": "task", "text": "x", "repo_key": "gitcode:o/r",
                        "issue_number": 5, "user_id": 1},
               expires_at=OB.new_expiry())
    before = OB.get_row(tid)

    consumer = OB.OutboxConsumer(eng, instance_id="owner-A")
    asyncio.run(consumer.run_once())

    assert ch.calls == 0, "暂停渠道不应真正 send"
    row = OB.get_row(tid)
    assert row["status"] == "pending", "channel_paused 行应回 pending（release_sending）"
    assert row["attempt_count"] == (before["attempt_count"] or 0), \
        "channel_paused 不算发送失败，attempt/退避不得递增"


def test_recover_route_409_when_channel_not_running(outbox_db, monkeypatch):
    """Required（PR #1983 审查）：recover 在本进程没有连接循环时 409
    channel_not_running，且不改健康状态（不假装成功）。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes import im as im_routes
    from src.domains.runtime.im import channel_health

    _insert_installation(outbox_db, "inst-wx", scope="user", owner="1234")
    tracker = channel_health.ChannelHealthTracker(store=channel_health.default_store(),
                                                  instance_id="host-1", clock=FakeClock())
    tracker.record_pause("inst-wx", "wechat", reason="ret=-14")

    class Sup:
        def channel_for(self, inst_id):
            return None

    class Eng:
        instance_id = "host-1"
        supervisor = Sup()
        health_tracker = tracker

    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    monkeypatch.setattr(im_routes, "_engine", lambda: Eng())

    body = im_routes.RecoverBody(action="retry_now")
    with pytest.raises(HTTPException) as ei:
        im_routes.im_installation_recover("inst-wx", body, None, user={"id": "1234"})
    assert ei.value.status_code == 409
    assert ei.value.detail.get("reason") == "channel_not_running"
    state = tracker.state("inst-wx")
    assert state["paused"] is True and not state["last_recover_action"], \
        "409 路径不得改健康状态"


def test_recover_route_409_when_engine_unavailable(outbox_db, monkeypatch):
    """Required（PR #1983 审查）：无引擎进程（B/C/D）→ 409 engine_unavailable。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes import im as im_routes

    _insert_installation(outbox_db, "inst-wx", scope="user", owner="1234")
    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    monkeypatch.setattr(im_routes, "_engine", lambda: None)

    body = im_routes.RecoverBody(action="retry_now")
    with pytest.raises(HTTPException) as ei:
        im_routes.im_installation_recover("inst-wx", body, None, user={"id": "1234"})
    assert ei.value.status_code == 409
    assert ei.value.detail.get("reason") == "engine_unavailable"


def test_recover_route_403_for_other_users_installation(outbox_db, monkeypatch):
    """Required（PR #1983 审查）：user 轨 scope 校验——其他用户操作 → 403。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes import im as im_routes

    _insert_installation(outbox_db, "inst-wx", scope="user", owner="1234")
    monkeypatch.setattr(im_routes, "_db_conn", lambda: sqlite3.connect(outbox_db))
    monkeypatch.setattr(im_routes, "_engine", lambda: None)

    body = im_routes.RecoverBody(action="clear_pause")
    with pytest.raises(HTTPException) as ei:
        im_routes.im_installation_recover("inst-wx", body, None, user={"id": "999"})
    assert ei.value.status_code == 403


# ---------------------------------------------------------------- 8. Optional（PR #1983 审查）

def test_notify_user_degraded_to_same_type_other_installation(outbox_db):
    """Optional（PR #1983 审查）：同一收件人另有**同类型**健康安装送达时，
    降级记录应记 ok=True（按 installation 区分「其它渠道」，不按 channel_type 去重）。"""
    from src.domains.runtime.im import notify as notify_mod
    from src.domains.runtime.im.channel_health import ChannelHealthTracker
    from tests.runtime.im.conftest import FakeChannel, FakeEngine

    eng = FakeEngine({"inst-wx1": FakeChannel(), "inst-wx2": FakeChannel()})
    tracker = ChannelHealthTracker(store=_store(outbox_db), instance_id="host-1",
                                   clock=FakeClock())
    tracker.record_pause("inst-wx1", "wechat", reason="ret=-14")
    eng.health_tracker = tracker

    targets = [{"installation_id": "inst-wx1", "channel_type": "wechat", "chat_id": "c-1"},
               {"installation_id": "inst-wx2", "channel_type": "wechat", "chat_id": "c-2"}]
    notify_mod._reset_for_tests()
    res = asyncio.run(notify_mod.notify_user(1, "task", "hello", targets=targets,
                                             engine=eng, dedupe=False))
    assert res["sent"] == ["wechat"]
    state = tracker.state("inst-wx1")
    assert state["last_degraded_ok"] is True, \
        "同类型不同安装已送达，仍被记成 no_fallback"
    assert state["last_degraded_to"] == "wechat"


def test_request_retry_on_healthy_channel_does_not_arm_wake_gate(health_db):
    """Optional（PR #1983 审查）：健康渠道上点 retry_now 不得置唤醒闸——
    闸滞留到下一次 ret=-14 会跳过 1h 等待立即重试（账号级风控下有加重风险）。"""
    from src.domains.runtime.im.adapters.wechat.channel import WechatChannel

    ch = WechatChannel(_cfg(), FakeSender())
    ch._loop_running = True
    assert ch.request_retry() is True          # 连接在跑 → 不报 channel_not_running
    assert ch._retry_wake.is_set() is False, "未暂停时置闸会滞留到下一次暂停"
