"""Issue #3969：health_scorer 心跳阈值对齐「连续 ≥3 个心跳间隔才判离线」。

漂移形态：`_HEARTBEAT_STALE_SECONDS=100` 只按 last_seen 年龄判定，未显式表达
「单次 502（被客户端重试覆盖）→ 不失联；连续 ≥3 个心跳间隔失败 → 判离线」。
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.domains.runtime.engine import health_scorer


def _patch_row(credential_id, status, last_seen):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (credential_id, status, last_seen)
    db = MagicMock()
    db.cursor.return_value = mock_cursor
    lock = MagicMock()
    lock.__enter__ = MagicMock()
    lock.__exit__ = MagicMock(return_value=False)
    return db, lock


class TestStaleThreshold:
    def test_default_keeps_100s(self, monkeypatch):
        monkeypatch.delenv("HEALTH_HEARTBEAT_STALE_SECONDS", raising=False)
        monkeypatch.delenv("RUNTIME_HEARTBEAT_INTERVAL", raising=False)
        monkeypatch.delenv("HEALTH_HEARTBEAT_MISSES_THRESHOLD", raising=False)
        assert health_scorer._heartbeat_stale_threshold() >= 100

    def test_three_missed_intervals_wins(self, monkeypatch):
        monkeypatch.setenv("HEALTH_HEARTBEAT_STALE_SECONDS", "5")
        monkeypatch.setenv("RUNTIME_HEARTBEAT_INTERVAL", "10")
        monkeypatch.setenv("HEALTH_HEARTBEAT_MISSES_THRESHOLD", "3")
        assert health_scorer._heartbeat_stale_threshold() == 30

    def test_existing_stale_wins_when_larger(self, monkeypatch):
        monkeypatch.setenv("HEALTH_HEARTBEAT_STALE_SECONDS", "120")
        monkeypatch.setenv("RUNTIME_HEARTBEAT_INTERVAL", "15")
        monkeypatch.setenv("HEALTH_HEARTBEAT_MISSES_THRESHOLD", "3")
        assert health_scorer._heartbeat_stale_threshold() == 120


class TestSingleVsConsecutive:
    """阈值=30s（心跳 10s × 3）场景：1 次错过（15s）在线，3 次错过（35s）离线。"""

    def _alive(self, age_seconds, monkeypatch):
        monkeypatch.setenv("HEALTH_HEARTBEAT_STALE_SECONDS", "5")
        monkeypatch.setenv("RUNTIME_HEARTBEAT_INTERVAL", "10")
        monkeypatch.setenv("HEALTH_HEARTBEAT_MISSES_THRESHOLD", "3")
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        db, lock = _patch_row(99, "online", ts)
        with patch.object(health_scorer, "_db", return_value=db), \
             patch.object(health_scorer, "_lock", return_value=lock):
            return health_scorer._fetch_heartbeat(3969001)

    def test_single_missed_heartbeat_stays_alive(self, monkeypatch):
        assert self._alive(15, monkeypatch) is True

    def test_two_missed_intervals_stays_alive(self, monkeypatch):
        assert self._alive(25, monkeypatch) is True

    def test_three_missed_intervals_goes_offline(self, monkeypatch):
        assert self._alive(35, monkeypatch) is False