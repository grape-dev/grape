#!/usr/bin/env python3
import os
import sys
import json
import tempfile
from unittest import mock

os.environ.setdefault("EVAL_DB_HOST", "localhost")
os.environ.setdefault("EVAL_DB_USER", "root")
os.environ.setdefault("EVAL_DB_PASSWORD", "test-password")
os.environ.setdefault("EVAL_DB_NAME", "test_db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import eval_skill


def _make_session(status="success", duration=10):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "output.log"), "w") as f:
        f.write("level=INFO ok\n")
    with open(os.path.join(d, "result.json"), "w") as f:
        json.dump({"status": status, "duration_seconds": duration, "exit_code": 0}, f)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({"skill_name": "test-skill", "skill_path": "/nonexistent/skill"}, f)
    return d


def _run(args):
    with mock.patch("sys.argv", ["eval_skill.py"] + args):
        try:
            eval_skill.main()
            return 0
        except SystemExit as e:
            return e.code


def test_success_path_sets_done():
    s = _make_session()
    flow_calls = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", side_effect=lambda *a, **k: flow_calls.append(a) or True), \
         mock.patch("eval_skill.parse_logs"), \
         mock.patch("eval_skill.evaluate_security", return_value=("low", [])), \
         mock.patch("eval_skill.get_skill_id", return_value="skill_001"), \
         mock.patch("eval_skill.get_baseline", return_value=None), \
         mock.patch("eval_skill.score"), \
         mock.patch("eval_skill.write_all", return_value=1), \
         mock.patch("eval_skill.write_security_issues", return_value=0), \
         mock.patch("eval.resource_cleaner.cleanup_session", return_value=(mock.MagicMock(), [])), \
         mock.patch("eval.resource_cleaner.send_failure_notification"), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None):
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode"])
    statuses = [c[1] for c in flow_calls]
    assert code == 0
    assert statuses == ["done"], statuses


def test_dry_run_sets_done_and_skips_write():
    s = _make_session()
    flow_calls = []
    write_called = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", side_effect=lambda *a, **k: flow_calls.append(a) or True), \
         mock.patch("eval_skill.parse_logs"), \
         mock.patch("eval_skill.evaluate_security", return_value=("low", [])), \
         mock.patch("eval_skill.get_skill_id", return_value="skill_001"), \
         mock.patch("eval_skill.get_baseline", return_value=None), \
         mock.patch("eval_skill.score"), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None):
        def _fake_write(*a, **k):
            write_called.append(1)
            return 1
        with mock.patch("eval_skill.write_all", side_effect=_fake_write):
            code = _run([s, "--skill-name", "test-skill", "--agent", "opencode", "--dry-run"])
    statuses = [c[1] for c in flow_calls]
    assert code == 0
    assert "done" in statuses
    assert write_called == [], "dry-run should not call write_all"


def test_failure_path_sets_failed():
    s = _make_session()
    flow_calls = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", side_effect=lambda *a, **k: flow_calls.append(a) or True), \
         mock.patch("eval_skill.parse_logs", side_effect=Exception("parse boom")), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None):
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode"])
    statuses = [c[1] for c in flow_calls]
    assert code == 1
    assert "failed" in statuses, statuses


def test_orchestration_called_on_success():
    s = _make_session()
    orch_calls = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", return_value=True), \
         mock.patch("eval_skill.parse_logs"), \
         mock.patch("eval_skill.evaluate_security", return_value=("low", [])), \
         mock.patch("eval_skill.get_skill_id", return_value="skill_001"), \
         mock.patch("eval_skill.get_baseline", return_value=None), \
         mock.patch("eval_skill.score"), \
         mock.patch("eval_skill.write_all", return_value=1), \
         mock.patch("eval_skill.write_security_issues", return_value=0), \
         mock.patch("eval.resource_cleaner.cleanup_session", return_value=(mock.MagicMock(), [])), \
         mock.patch("eval.resource_cleaner.send_failure_notification"), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None), \
         mock.patch("eval_skill.run_pr_orchestration", side_effect=lambda **k: orch_calls.append(k)):
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode", "--pr-url", "https://gitcode.com/o/r/merge_requests/1"])
    assert code == 0
    assert len(orch_calls) == 1, orch_calls
    assert orch_calls[0]["eval_ok"] is True
    assert orch_calls[0]["issue_id"] == "issue-1"
    assert orch_calls[0]["pr_url"] == "https://gitcode.com/o/r/merge_requests/1"


def test_retrieval_falls_back_to_default_repo():
    s = _make_session()
    index_calls = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", return_value=True), \
         mock.patch("eval_skill.parse_logs"), \
         mock.patch("eval_skill.evaluate_security", return_value=("low", [])), \
         mock.patch("eval_skill.get_skill_id", return_value="skill_001"), \
         mock.patch("eval_skill.get_baseline", return_value=None), \
         mock.patch("eval_skill.score"), \
         mock.patch("eval_skill.write_all", return_value=1), \
         mock.patch("eval_skill.write_security_issues", return_value=0), \
         mock.patch("eval.resource_cleaner.cleanup_session", return_value=(mock.MagicMock(), [])), \
         mock.patch("eval.resource_cleaner.send_failure_notification"), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None), \
         mock.patch("eval_skill.load_default_repo", return_value="huaweiskilldev/huaweicloud-skillsdev"), \
         mock.patch("eval_skill.build_skill_index", side_effect=lambda path: index_calls.append(path) or {}):
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode"])
    assert code == 0
    assert any("/tmp/fetch-pr-skill/huaweiskilldev_huaweicloud-skillsdev" in p for p in index_calls), index_calls


def test_orchestration_not_called_without_pr_url():
    s = _make_session()
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", return_value=True), \
         mock.patch("eval_skill.parse_logs"), \
         mock.patch("eval_skill.evaluate_security", return_value=("low", [])), \
         mock.patch("eval_skill.get_skill_id", return_value="skill_001"), \
         mock.patch("eval_skill.get_baseline", return_value=None), \
         mock.patch("eval_skill.score"), \
         mock.patch("eval_skill.write_all", return_value=1), \
         mock.patch("eval_skill.write_security_issues", return_value=0), \
         mock.patch("eval.resource_cleaner.cleanup_session", return_value=(mock.MagicMock(), [])), \
         mock.patch("eval.resource_cleaner.send_failure_notification"), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None), \
         mock.patch("eval_skill.run_pr_orchestration") as fake_orch:
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode"])
    assert code == 0
    fake_orch.assert_not_called()


def test_orchestration_called_when_eval_fails():
    s = _make_session()
    orch_calls = []
    with mock.patch("eval_skill.get_multica_issue_id", return_value="issue-1"), \
         mock.patch("eval_skill.build_verify_url", return_value="https://pr"), \
         mock.patch("eval_skill.update_verify_status", return_value=True), \
         mock.patch("eval_skill.parse_logs", side_effect=Exception("parse boom")), \
         mock.patch("eval.report_uploader.load_obs_config", return_value=None), \
         mock.patch("eval_skill.run_pr_orchestration", side_effect=lambda **k: orch_calls.append(k)):
        code = _run([s, "--skill-name", "test-skill", "--agent", "opencode", "--pr-url", "https://gitcode.com/o/r/merge_requests/1"])
    assert code == 1, "eval failure should still exit 1"
    assert len(orch_calls) == 1, orch_calls
    assert orch_calls[0]["eval_ok"] is False
