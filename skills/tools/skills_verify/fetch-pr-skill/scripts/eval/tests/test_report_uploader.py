#!/usr/bin/env python3
import os
import sys
import json
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.report_uploader import (
    load_obs_config,
    build_report_content,
    upload_report,
    build_download_url,
    update_report_url,
)


def _write_cred_file(content):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "obs-cred.conf")
    with open(path, "w") as f:
        f.write(content)
    return path


def test_load_obs_config():
    path = _write_cred_file(
        "OBS_AK=ak123\nOBS_SK=sk456\nOBS_BUCKET=obs-hd-dev-static\n"
        "OBS_CDN_DOMAIN=skills.topxtopx.com\nOBS_REGION=cn-north-4\n"
    )
    with mock.patch("eval.report_uploader.OBS_CRED_FILE", path):
        cfg = load_obs_config()
    assert cfg["ak"] == "ak123"
    assert cfg["sk"] == "sk456"
    assert cfg["bucket"] == "obs-hd-dev-static"
    assert cfg["cdn_domain"] == "skills.topxtopx.com"
    assert cfg["region"] == "cn-north-4"


def test_load_obs_config_missing_file_returns_none():
    with mock.patch("eval.report_uploader.OBS_CRED_FILE", "/nonexistent/obs-cred.conf"):
        assert load_obs_config() is None


def test_build_report_content():
    class FakeResult:
        total_score = 8.0
        stability_score = 3.0
        cost_score = 1.0
        efficiency_score = 2.0
        security_score = 2.0
        item_scores = [
            type("S", (), {"category": "稳定性评估", "item_name": "功能可用", "max_score": 2.0, "actual_score": 2.0})(),
            type("S", (), {"category": "稳定性评估", "item_name": "提示词匹配稳定性", "max_score": 2.0, "actual_score": 1.0})(),
        ]

    class FakeMetrics:
        total_tokens = 1503311
        duration_seconds = 225
        tool_fail_count = 1
        retry_count = 1
        cli_call_count = 101
        sdk_call_count = 45
        api_call_count = 0
        llm_call_count = 28
        context_compressed = False
        has_bug = False
        security_risk_level = "low"

    content = build_report_content(
        "huawei-cloud-swr-list-query",
        "https://gitcode.com/x/y/merge_requests/106",
        FakeResult(),
        FakeMetrics(),
        "low",
        153,
        "session-123",
    )
    assert "huawei-cloud-swr-list-query" in content
    assert "merge_requests/106" in content
    assert "8.0" in content
    assert "153" in content
    assert "稳定性评估" in content
    assert "low" in content
    assert "session-123" in content


def test_upload_report_success():
    fake_obs = mock.MagicMock()
    fake_resp = mock.MagicMock()
    fake_resp.status = 200
    fake_obs.putContent.return_value = fake_resp
    cfg = {"ak": "a", "sk": "s", "bucket": "obs-hd-dev-static", "cdn_domain": "d", "region": "cn-north-4"}
    with mock.patch("eval.report_uploader.obs.ObsClient", return_value=fake_obs):
        ok = upload_report(cfg, "report/test.md", "content")
    assert ok is True
    fake_obs.putContent.assert_called_once_with("obs-hd-dev-static", "report/test.md", "content")


def test_upload_report_failure():
    fake_obs = mock.MagicMock()
    fake_obs.putContent.side_effect = Exception("obs down")
    cfg = {"ak": "a", "sk": "s", "bucket": "obs-hd-dev-static", "cdn_domain": "d", "region": "cn-north-4"}
    with mock.patch("eval.report_uploader.obs.ObsClient", return_value=fake_obs):
        assert upload_report(cfg, "report/test.md", "content") is False


def test_build_download_url():
    cfg = {"ak": "a", "sk": "s", "bucket": "obs-hd-dev-static", "cdn_domain": "skills.topxtopx.com", "region": "cn-north-4"}
    assert build_download_url(cfg, "report/test.md") == "http://skills.topxtopx.com/report/test.md"


def test_update_report_url_calls_db():
    fake_conn = mock.MagicMock()
    fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
    fake_cursor.rowcount = 1
    with mock.patch("eval.report_uploader._get_connection", return_value=fake_conn):
        assert update_report_url(153, "http://skills.topxtopx.com/report/x.md") is True
    sql = fake_cursor.execute.call_args[0][0]
    assert "report_url" in sql
    args = fake_cursor.execute.call_args[0][1]
    assert args[0] == "http://skills.topxtopx.com/report/x.md"
    assert args[1] == 153


def test_update_report_url_returns_false_on_exception():
    with mock.patch("eval.report_uploader._get_connection", side_effect=Exception("db down")):
        assert update_report_url(153, "http://x") is False
