"""指标落库必须走双后端可翻译的方言（架构审查 B1）。

`record_metric` 曾直接写 MySQL 专有的 `ON DUPLICATE KEY UPDATE ... VALUES(x)`：
连接层只翻译 `ON CONFLICT` / `INSERT OR REPLACE` 一类子句，sqlite 模式下该 SQL
语法非法 —— 双后端（sqlite 本地 / MySQL 生产）里就有一端直接炸。
"""
import pathlib

from src.domains.insight import public as insight_public


def test_record_metric_delegates_to_rollup_writer(monkeypatch):
    calls = {}

    def _fake_write_bucket(resolution, bucket_start, rows):
        calls["resolution"] = resolution
        calls["rows"] = rows
        return 1

    import src.domains.insight.observability.metric_rollup as mr

    monkeypatch.setattr(mr, "write_bucket", _fake_write_bucket)
    insight_public.record_metric("webhook_inbox_pending", 3, dim_key="global",
                                 resolution="minute", bucket_start="2026-11-23T02:00:00+00:00")
    assert calls["resolution"] == "minute"
    assert calls["rows"] == {"global": {"webhook_inbox_pending": (3.0, 0)}}


def test_insight_public_has_no_mysql_only_upsert():
    src = pathlib.Path(insight_public.__file__).read_text(encoding="utf-8")
    assert "ON DUPLICATE KEY UPDATE value=VALUES" not in src, \
        "insight public 不得直接使用 MySQL 专有 upsert（sqlite 模式会失败）"