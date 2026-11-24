"""Issue #3772：no_progress 扫描在 MySQL 兼容 cursor 上恒失败（回归用例）。

约束：扫描入口不得依赖 cursor 的隐式迭代协议（MySQLCompatCursor 历史上只实现
fetchone/fetchall）；失败必须计数并支持连续失败升级 ERROR。
"""
import logging
import os
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine import scheduler


class _NonIterableCursor:
    """模拟旧 MySQLCompatCursor：只有 fetchone/fetchall，没有 __iter__。"""

    def __init__(self, rows):
        self._rows = list(rows)
        self.fetchall_called = False

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        self.fetchall_called = True
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _BoomCursor:
    def execute(self, sql, params=None):
        raise RuntimeError("simulated scan failure")

    def fetchall(self):
        raise RuntimeError("simulated scan failure")

    def fetchone(self):
        raise RuntimeError("simulated scan failure")

    def close(self):
        pass


class _FakeDB:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _patch_db(monkeypatch, cursor):
    monkeypatch.setattr(scheduler, "db", _FakeDB(cursor))
    monkeypatch.setattr(scheduler, "db_lock", nullcontext())


def _young_row(task_id="t_i3772_young"):
    # started_at 为当前时刻 → silent_sec 远低于阈值，扫描后直接跳过，不再触达 DB。
    return [(task_id, 1, 0, _iso_now(), "coding", "gitcode:issue3772/no-progress", "{}", None)]


class TestNoProgressScanDoesNotDependOnCursorIteration:
    def test_scan_uses_fetchall_not_implicit_iteration(self, monkeypatch):
        cur = _NonIterableCursor(_young_row())
        _patch_db(monkeypatch, cur)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert cur.fetchall_called is True


class TestNoProgressScanFailureObservability:
    def test_failure_streak_and_metric(self, monkeypatch, caplog):
        from src.domains.insight.observability import NO_PROGRESS_SCAN_FAILURES

        monkeypatch.setattr(scheduler, "_NO_PROGRESS_SCAN_FAILURE_STREAK", 0, raising=False)
        _patch_db(monkeypatch, _BoomCursor())
        before = NO_PROGRESS_SCAN_FAILURES._value.get()
        threshold = scheduler._NO_PROGRESS_SCAN_FAILURE_ALERT_THRESHOLD

        with caplog.at_level(logging.ERROR):
            for _ in range(threshold):
                scheduler._sweep_no_progress_tasks(now_epoch=time.time())

        assert NO_PROGRESS_SCAN_FAILURES._value.get() - before == threshold
        assert scheduler._NO_PROGRESS_SCAN_FAILURE_STREAK == threshold
        assert any(
            r.levelno == logging.ERROR and "no_progress" in r.getMessage()
            for r in caplog.records
        )

    def test_success_resets_failure_streak(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_NO_PROGRESS_SCAN_FAILURE_STREAK", 3, raising=False)
        _patch_db(monkeypatch, _NonIterableCursor(_young_row()))
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert scheduler._NO_PROGRESS_SCAN_FAILURE_STREAK == 0


class TestNoProgressScanAlertRuleDriftGuard:
    """QA R1 看护：告警规则消费的指标名必须与指标出口实际暴露的一致。"""

    def test_alert_rule_consumes_exposed_metric_name(self):
        import yaml
        from src.domains.insight.observability import NO_PROGRESS_SCAN_FAILURES_METRIC

        alerts_path = os.path.join(ROOT, "deploy", "alerts.yml")
        with open(alerts_path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)

        rule = None
        for group in rules.get("groups", []):
            for candidate in group.get("rules", []):
                if candidate.get("alert") == "GrapeNoProgressScanFailing":
                    rule = candidate
        assert rule is not None, "deploy/alerts.yml 缺少 GrapeNoProgressScanFailing 规则"
        assert NO_PROGRESS_SCAN_FAILURES_METRIC in rule["expr"], (
            f"告警 expr 未消费指标 {NO_PROGRESS_SCAN_FAILURES_METRIC}: {rule['expr']}"
        )
        assert rule["labels"]["severity"] == "warning"
        assert rule["for"] == "1m"

    def test_counter_registers_metric_name_constant(self):
        from src.domains.insight.observability import (
            NO_PROGRESS_SCAN_FAILURES,
            NO_PROGRESS_SCAN_FAILURES_METRIC,
        )
        # prometheus_client Counter 的 `_name` 会剥掉 `_total` 后缀，以样本名为准。
        samples = [s.name for m in NO_PROGRESS_SCAN_FAILURES.collect()
                   for s in m.samples]
        assert NO_PROGRESS_SCAN_FAILURES_METRIC in samples