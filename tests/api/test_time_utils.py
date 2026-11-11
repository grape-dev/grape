"""时间归一化工具测试（docs/event-time-localization-design.md）。

覆盖：MySQL（CST 无时区→UTC）、SQLite（UTC 无时区→加 Z）、带时区 ISO 原样、
+08:00 偏移转 UTC、Z 结尾、空值/坏值兜底。
"""
import pytest
from src.api.time_utils import normalize_event_time


@pytest.fixture(autouse=True)
def _sqlite_backend(monkeypatch):
    """默认测试环境是 sqlite 语义（conftest 强制 DEVOPS_DB_BACKEND=sqlite）。"""
    monkeypatch.setattr("src.api.time_utils._db_backend", lambda: "sqlite")
    yield


class TestNormalizeEventTime:
    def test_mysql_cst_naive_to_utc(self, monkeypatch):
        monkeypatch.setattr("src.api.time_utils._db_backend", lambda: "mysql")
        # MySQL now() 存 CST（UTC+8）无时区 → 转 UTC
        assert normalize_event_time("2026-08-17 20:46:19") == "2026-08-17T12:46:19+00:00"

    def test_sqlite_utc_naive_kept(self):
        # SQLite datetime('now') 存 UTC 无时区 → 标注 +00:00
        assert normalize_event_time("2026-08-17 12:46:19") == "2026-08-17T12:46:19+00:00"

    def test_iso_with_offset_passthrough(self):
        # 已带时区（UTC ISO，代码 datetime.now(timezone.utc).isoformat() 产物）→ 原样语义
        assert normalize_event_time("2026-08-17T12:46:19.163342+00:00") == "2026-08-17T12:46:19.163342+00:00"

    def test_iso_positive_offset_converted(self):
        # +08:00 偏移 → 转 UTC
        assert normalize_event_time("2026-08-17T20:46:19+08:00") == "2026-08-17T12:46:19+00:00"

    def test_iso_negative_offset_converted(self):
        assert normalize_event_time("2026-08-17T07:46:19-05:00") == "2026-08-17T12:46:19+00:00"

    def test_z_suffix(self):
        assert normalize_event_time("2026-08-17T12:46:19Z") == "2026-08-17T12:46:19+00:00"

    def test_mysql_naive_with_microseconds(self, monkeypatch):
        monkeypatch.setattr("src.api.time_utils._db_backend", lambda: "mysql")
        assert normalize_event_time("2026-08-17 20:46:19.500000") == "2026-08-17T12:46:19.500000+00:00"

    def test_empty_and_none(self):
        assert normalize_event_time("") == ""
        assert normalize_event_time(None) is None

    def test_bad_value_passthrough(self):
        assert normalize_event_time("not-a-time") == "not-a-time"
        assert normalize_event_time("2026-13-99 99:99:99") == "2026-13-99 99:99:99"

    def test_idempotent(self):
        # 已归一化结果再归一化不变（带时区路径）
        once = normalize_event_time("2026-08-17 20:46:19")  # sqlite 语义
        twice = normalize_event_time(once)
        assert once == twice == "2026-08-17T20:46:19+00:00"
