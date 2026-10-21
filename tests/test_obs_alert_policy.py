"""告警策略层测试（P1-1 / D1）：级别 / 通知时段 / 重复抑制 / 聚合 / 升级。

纯单元（不碰库）——策略层是这次改动最容易回归的地方：
钉死「窗口外只记录、窗口内补推一次」「repeat_sec=0 只通知一次」「聚合窗口口径」
「非法配置不写库也不炸求值器」这几条语义。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import alert_policy as p  # noqa: E402

CST = timezone(timedelta(hours=8))
UTC = timezone.utc


def _cst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CST)


# ---------------------------------------------------------------- 级别

def test_severity_normalization_and_ranking():
    assert p.normalize_severity("CRITICAL") == "critical"
    assert p.normalize_severity("severe") == "severe"
    assert p.normalize_severity("") == "warning"          # 空 → 默认
    assert p.normalize_severity("nonsense") == "warning"   # 非法 → 默认（不炸）
    assert p.severity_rank("critical") < p.severity_rank("warning") < p.severity_rank("info")
    assert p.more_severe("warning", "critical") == "critical"
    assert p.more_severe("info", "warning") == "warning"
    assert len(p.SEVERITIES) == 5


# ---------------------------------------------------------------- 通知时段

def test_parse_notify_window():
    assert p.parse_notify_window("") is None          # 空 = 全天
    assert p.parse_notify_window(None) is None
    assert p.parse_notify_window("{not json") is None  # 非法 → 全天（记日志不抛）
    w = p.parse_notify_window('{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}')
    assert w is not None
    assert w == {"days": [1, 2, 3, 4, 5], "start": 540, "end": 1140}
    w2 = p.parse_notify_window({"start": "22:00", "end": "06:00"})
    assert w2 is not None
    assert w2["days"] == [1, 2, 3, 4, 5, 6, 7]        # days 省略 = 每天
    assert w2["start"] == 1320 and w2["end"] == 360


def test_in_notify_window_weekday_and_hours():
    w = p.parse_notify_window('{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}')
    # 2026-10-19 是周一
    assert p.in_notify_window(w, _cst(2026, 10, 19, 10, 0)) is True
    assert p.in_notify_window(w, _cst(2026, 10, 19, 8, 59)) is False   # 早于窗口
    assert p.in_notify_window(w, _cst(2026, 10, 19, 19, 1)) is False   # 晚于窗口
    # 2026-10-18 是周日 → 不在 days 里
    assert p.in_notify_window(w, _cst(2026, 10, 18, 10, 0)) is False
    # 无窗口 = 全天真
    assert p.in_notify_window(None, _cst(2026, 10, 18, 3, 0)) is True
    # naive 时间按 UTC 解释：UTC 01:00 = CST 09:00 → 在窗口内
    assert p.in_notify_window(w, datetime(2026, 10, 19, 1, 0)) is True


def test_in_notify_window_cross_midnight():
    w = p.parse_notify_window('{"start":"22:00","end":"06:00"}')
    assert p.in_notify_window(w, _cst(2026, 10, 19, 23, 30)) is True   # 当晚
    assert p.in_notify_window(w, _cst(2026, 10, 20, 5, 0)) is True     # 次日凌晨
    assert p.in_notify_window(w, _cst(2026, 10, 20, 12, 0)) is False   # 白天不在


# ---------------------------------------------------------------- 升级

def test_parse_escalation():
    assert p.parse_escalation("") is None
    assert p.parse_escalation('{"after_sec":0}') is None      # 0 秒 = 不升级
    assert p.parse_escalation("garbage") is None
    cfg = p.parse_escalation('{"after_sec":1800,"severity":"critical","channels":"feishu"}')
    assert cfg == {"after_sec": 1800, "severity": "critical", "channels": "feishu"}


def test_escalation_due_only_after_threshold_and_once():
    rule = {"escalate_json": json.dumps({"after_sec": 1800, "severity": "critical"})}
    now = datetime.now(UTC)
    young = {"first_fired_at": (now - timedelta(seconds=600)).isoformat(), "escalated_at": ""}
    old = {"first_fired_at": (now - timedelta(seconds=2000)).isoformat(), "escalated_at": ""}
    done = {"first_fired_at": (now - timedelta(seconds=2000)).isoformat(),
            "escalated_at": now.isoformat()}
    assert p.escalation_due(rule, young, now) is None
    assert p.escalation_due(rule, old, now) is not None
    assert p.escalation_due(rule, done, now) is None       # 已升级过不重复
    assert p.escalation_due({}, old, now) is None          # 未配置不升级


# ---------------------------------------------------------------- 策略归一化

def test_sanitize_policy_accepts_valid_and_normalizes_window():
    clean, errors = p.sanitize_policy({
        "severity": "severe", "repeat_sec": 600, "aggregate_mode": "rule",
        "aggregate_window_sec": 0, "notify_window_json": '{"days":[1],"start":"09:00","end":"10:00"}',
        "escalate_json": '{"after_sec":900}', "notify_channels": "feishu", "enabled": True,
    })
    assert errors == []
    assert clean["severity"] == "severe" and clean["repeat_sec"] == 600
    assert clean["enabled"] == 1
    assert clean["aggregate_window_sec"] == 300            # mode!=none 且为 0 → 补默认 300
    assert json.loads(clean["notify_window_json"])["days"] == [1]
    assert json.loads(clean["escalate_json"])["after_sec"] == 900


def test_sanitize_policy_rejects_invalid():
    clean, errors = p.sanitize_policy({"severity": "super-critical", "repeat_sec": -5,
                                       "aggregate_mode": "hourly",
                                       "notify_window_json": "{bad",
                                       "escalate_json": '{"after_sec":0}'})
    assert errors and len(errors) == 5
    assert clean == {}                                      # 非法 → 一个字段都不写


def test_sanitize_policy_clears_window_and_escalation():
    clean, errors = p.sanitize_policy({"notify_window_json": "", "escalate_json": ""})
    assert errors == [] and clean["notify_window_json"] == "" and clean["escalate_json"] == ""


# ---------------------------------------------------------------- 发不发

def _rule(**kw):
    base = {"notify_channels": "feishu", "severity": "warning", "aggregate_mode": "none",
            "repeat_sec": 0, "notify_window_json": ""}
    base.update(kw)
    return base


def test_decide_notify_basic_paths():
    now = datetime.now(UTC)
    # 无渠道 → skip（只记录）
    assert p.decide_notify(_rule(notify_channels=""), {}, now) == ("skip", "no_channel")
    # 静默中 → skip
    fut = (now + timedelta(minutes=30)).isoformat()
    assert p.decide_notify(_rule(), {"silenced_until": fut}, now)[1] == "silenced"
    # 正常 → send
    assert p.decide_notify(_rule(), {"silenced_until": ""}, now) == ("send", "ok")


def test_decide_notify_repeat_semantics():
    now = datetime.now(UTC)
    # repeat_sec=0 且已通知过 → 不再推（旧行为）
    ev = {"silenced_until": "", "last_notified_at": (now - timedelta(hours=1)).isoformat()}
    assert p.decide_notify(_rule(repeat_sec=0), ev, now) == ("skip", "already_notified")
    # repeat_sec=3600，才过 60s → 抑制
    ev2 = {"silenced_until": "", "last_notified_at": (now - timedelta(seconds=60)).isoformat()}
    assert p.decide_notify(_rule(repeat_sec=3600), ev2, now) == ("skip", "repeat_suppressed")
    # repeat_sec=30，已过 60s → 复推（"还在告警"）
    assert p.decide_notify(_rule(repeat_sec=30), ev2, now) == ("send", "ok")


def test_repeat_due_semantics():
    """repeat_sec：从未通知过 → 提名；已通知 → 等满间隔才再提名。"""
    now = datetime.now(UTC)
    assert p.repeat_due(_rule(repeat_sec=0), {}, now) is False          # 0 = 只通知一次
    r = _rule(repeat_sec=600)
    assert p.repeat_due(r, {"last_notified_at": ""}, now) is True        # 从未通知过
    assert p.repeat_due(r, {"last_notified_at": (now - timedelta(seconds=100)).isoformat()},
                        now) is False
    assert p.repeat_due(r, {"last_notified_at": (now - timedelta(seconds=700)).isoformat()},
                        now) is True
    assert p.repeat_due(r, {"last_notified_at": "garbage"}, now) is False


def test_decide_notify_window_and_aggregate():
    now = _cst(2026, 10, 19, 3, 0)          # 周一凌晨 3 点
    win = '{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}'
    assert p.decide_notify(_rule(notify_window_json=win),
                           {"silenced_until": ""}, now) == ("defer", "out_of_notify_window")
    inside = _cst(2026, 10, 19, 10, 0)
    assert p.decide_notify(_rule(notify_window_json=win),
                           {"silenced_until": ""}, inside)[0] == "send"
    # 聚合规则 → 交给缓冲
    assert p.decide_notify(_rule(aggregate_mode="rule", aggregate_window_sec=300),
                           {"silenced_until": ""}, now)[0] == "aggregate"


# ---------------------------------------------------------------- 聚合缓冲

def test_aggregator_rule_mode_window_from_first_event_and_dedupe():
    agg = p.Aggregator()
    t0 = datetime.now(UTC)
    rule = {"id": 7, "name": "task_stuck", "level": "user", "severity": "warning",
            "aggregate_mode": "rule", "aggregate_window_sec": 300}
    agg.add(rule, {"id": 1, "dim_key": "repo:a/b", "repo_key": "repo:a/b", "value": 2,
                   "metric": "task_stuck", "threshold": 0}, t0)
    agg.add(rule, {"id": 2, "dim_key": "repo:c/d", "repo_key": "repo:c/d", "value": 5,
                   "metric": "task_stuck", "threshold": 0}, t0 + timedelta(seconds=60))
    assert agg.pending() == 1                              # 同一规则合并成一组
    assert agg.due(t0 + timedelta(seconds=100)) == []      # 窗口未到不发
    due = agg.due(t0 + timedelta(seconds=301))
    assert len(due) == 1
    g = due[0]
    assert g["dims"] == ["repo:a/b", "repo:c/d"] and g["event_ids"] == [1, 2]
    assert g["repos"] == ["repo:a/b", "repo:c/d"] and g["max_value"] == 5
    assert agg.pending() == 0                              # 取走即清


def test_aggregator_period_mode_is_clock_aligned():
    agg = p.Aggregator()
    rule = {"id": 9, "name": "task_failure_rate", "aggregate_mode": "period",
            "aggregate_window_sec": 300}
    base = datetime(2026, 10, 19, 10, 7, 10, tzinfo=UTC)     # 10:07:10 → 节拍到 10:10:00
    agg.add(rule, {"id": 1, "dim_key": "repo:a/b"}, base)
    g = agg._groups[list(agg._groups)[0]]
    assert g["window_end"] == datetime(2026, 10, 19, 10, 10, 0, tzinfo=UTC)
    assert agg.due(datetime(2026, 10, 19, 10, 9, 59, tzinfo=UTC)) == []
    assert len(agg.due(datetime(2026, 10, 19, 10, 10, 1, tzinfo=UTC))) == 1


def test_aggregator_keeps_rules_separate_and_picks_up_escalation():
    agg = p.Aggregator()
    t0 = datetime.now(UTC)
    r1 = {"id": 1, "name": "r1", "aggregate_mode": "rule", "aggregate_window_sec": 300}
    r2 = {"id": 2, "name": "r2", "aggregate_mode": "rule", "aggregate_window_sec": 300}
    agg.add(r1, {"id": 1, "dim_key": "d1"}, t0)
    agg.add(r2, {"id": 2, "dim_key": "d2"}, t0)
    agg.add(r1, {"id": 3, "dim_key": "d3", "escalated": True}, t0 + timedelta(seconds=1))
    assert agg.pending() == 2                              # 两条规则互不干扰
    due = {g["rule_name"]: g for g in agg.due(t0 + timedelta(seconds=400))}
    assert due["r1"]["escalated"] is True and len(due["r1"]["event_ids"]) == 2
    assert due["r2"]["escalated"] is False