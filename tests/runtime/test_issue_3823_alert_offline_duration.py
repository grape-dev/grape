"""Issue #3823: 熔断结构化告警必须携带离线时长（task id / 引擎 id / 离线时长）。

背景：#3800 的熔断告警已含 `task` / `engine_ids` / `consecutive_failures`，
但 #3823 的验收要求告警同时给出**离线时长**，值守才能据此判断引擎掉了多久。
本用例锁定 `_alert_task_failure` 的结构化日志行与订阅者摘要两个出口；
悬空绑定（`BINDING_STALE`）没有可度量的在线时长，须显式输出「未知」而非 0s。
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine import scheduler as S


def _capture_alert(monkeypatch):
    alerts = []
    monkeypatch.setattr(
        S, "_notify_subscribers",
        lambda task_id, exec_id, status, summary, findings:
        alerts.append((task_id, exec_id, status, summary, findings)))
    logs = []
    monkeypatch.setattr(S.log, "error", lambda msg: logs.append(str(msg)))
    return alerts, logs


def test_alert_carries_task_engine_and_offline_duration(monkeypatch):
    task = {"id": 83, "engine_runtime_uids": json.dumps(["17877-uid"])}
    alerts, logs = _capture_alert(monkeypatch)

    S._alert_task_failure(
        task, 25467, S.ERROR_ENGINE_OFFLINE,
        f"task engines [17877] {S._OFFLINE_LOG_FINGERPRINT}",
        4, True, offline_seconds=420.7)

    assert len(logs) == 1, logs
    line = logs[0]
    assert "task=83" in line, line
    assert "engine_ids=" in line and "17877-uid" in line, line
    assert "offline_seconds=420" in line, line

    assert len(alerts) == 1, alerts
    summary = alerts[0][3]
    assert "17877-uid" in summary, summary
    assert "离线" in summary and "420" in summary, summary


def test_binding_stale_alert_reports_unknown_duration(monkeypatch):
    """悬空绑定首派无在线时长：输出「未知」，不得让值守读成「刚掉线」（0s）。"""
    task = {"id": 83, "engine_runtime_uids": json.dumps(["ghost-uid"])}
    alerts, logs = _capture_alert(monkeypatch)

    S._alert_task_failure(task, 25467, S.ERROR_BINDING_STALE, "binding stale", 1, False,
                          offline_seconds=0.0)

    assert len(logs) == 1, logs
    line = logs[0]
    assert "offline_seconds=unknown" in line, line
    assert "offline_seconds=0" not in line, line

    assert len(alerts) == 1, alerts
    summary = alerts[0][3]
    assert "未知" in summary, summary
    assert "离线时长 0s" not in summary, summary
