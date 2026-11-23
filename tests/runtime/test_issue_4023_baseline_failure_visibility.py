"""Issue #4023：Task failed 日志不再丢失 WorkspaceBaselineError 六要素。

锁定四条公开契约：
1. 基线异常日志单行固定七要素、不截断（对象与 worker 上报字符串两条来源）；
2. 非基线长异常超限时带 `…` 标记且可辨被截长度；
3. 完整错误以 JSON 结构化写入 agent_tasks.errors（字段名与 to_dict() 一致）；
4. 同 issue 同 reason 连续 3 次失败触发 `baseline_mismatch_repeat` ERROR 打点，
   2 次不触发；reason 变化会重置连续链。
"""
import json
import logging
from datetime import datetime, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.runtime.engine.repogit import (
    WorkspaceBaselineError, parse_baseline_error_text,
)

REPO = "gitcode:issue4023/test"
PATH = "/root/grape-workspaces/gitcode/grape-dev/grape/issues/3564/coding"
BASELINE_TEXT = (
    "workspace baseline mismatch: issue=#3564 "
    f"path={PATH} expected_baseline=arch-weekly-11-ljt "
    "actual_head=feat/issue-3564@abc123 ahead=2 behind=5 "
    "reason=diverged requested_baseline=main"
)


@pytest.fixture(autouse=True)
def _isolate():
    _clear_state()
    _wipe()
    yield
    _wipe()
    _clear_state()


def _clear_state():
    with service._baseline_failure_lock:
        service._baseline_failure_state.clear()


def _wipe():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _insert_event(seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, issue_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (seq, REPO, "a2a_event", status, "#3564"),
        )
        db.commit()


def _insert_task(task_id, seq, issue_id="#3564", status="running"):
    ts = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
            "issue_id, repo_key, agent_type, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "sess-4023", 7, seq, issue_id, REPO, "coding", "p", status, ts),
        )
        db.commit()


def _task_errors(task_id):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return json.loads(row[0]) if row and row[0] else []


def _baseline_error():
    return WorkspaceBaselineError(3564, PATH, "arch-weekly-11-ljt",
                                  "feat/issue-3564@abc123", 2, 5, "diverged", "main")


class TestBaselineErrorFormatting:
    def test_format_one_line_keeps_all_fields_without_truncation(self):
        err = _baseline_error()
        line = err.format_one_line()
        assert line == str(err)
        assert len(line) > 120, "六要素完整文本必然超过旧 120 字符上限"
        for frag in ("issue=#3564", f"path={PATH}",
                     "expected_baseline=arch-weekly-11-ljt",
                     "actual_head=feat/issue-3564@abc123",
                     "ahead=2", "behind=5", "reason=diverged",
                     "requested_baseline=main"):
            assert frag in line
        assert line.endswith("requested_baseline=main"), "不得以半截单词结尾"

    def test_requested_baseline_only_appended_when_different(self):
        err = WorkspaceBaselineError(3564, PATH, "arch-weekly-11-ljt",
                                     "feat/issue-3564@abc123", 0, 0, "unreachable")
        line = err.format_one_line()
        assert line == str(err)
        assert "reason=unreachable" in line
        assert "requested_baseline=" not in line

    def test_parse_baseline_text_round_trips_to_dict(self):
        err = _baseline_error()
        assert parse_baseline_error_text(str(err)) == err.to_dict()

    def test_parse_requested_equals_expected_round_trips_to_dict(self):
        err = WorkspaceBaselineError(3564, PATH, "arch-weekly-11-ljt",
                                     "feat/issue-3564@abc123", 2, 5, "diverged")
        parsed = parse_baseline_error_text(str(err))
        assert parsed == err.to_dict()
        assert parsed["requested_baseline"] == "", \
            "requested == expected 时 errors JSON 仍须含 requested_baseline 字段"


class TestFailureErrorFormatting:
    def test_baseline_string_kept_full_and_structured(self):
        text, struct = service._format_failure_error(BASELINE_TEXT)
        assert text == BASELINE_TEXT
        assert struct["issue"] == "3564"
        assert struct["expected_baseline"] == "arch-weekly-11-ljt"
        assert struct["actual_head"] == "feat/issue-3564@abc123"
        assert struct["ahead"] == 2 and struct["behind"] == 5
        assert struct["reason"] == "diverged"
        assert struct["requested_baseline"] == "main"

    def test_generic_long_error_truncated_with_marker(self, monkeypatch):
        monkeypatch.setenv("GRAPE_FAILURE_LOG_LIMIT", "400")
        text, struct = service._format_failure_error("x" * 1000)
        assert struct is None
        assert text.startswith("x" * 400)
        assert text.endswith("…(truncated 600 chars)")

    def test_generic_short_error_unchanged(self):
        text, struct = service._format_failure_error("engine crashed")
        assert text == "engine crashed"
        assert struct is None


class TestFailTaskBaselineVisibility:
    def test_task_failed_log_and_structured_errors(self, caplog):
        caplog.set_level(logging.INFO)
        _insert_event(40231)
        _insert_task("t_4023_visible", 40231)

        service.fail_task("t_4023_visible", BASELINE_TEXT, exit_code=-1)

        failed = [r.getMessage() for r in caplog.records
                  if r.getMessage().startswith("Task failed:")]
        assert failed, "必须产出 Task failed 日志行"
        msg = failed[-1]
        assert msg.endswith("requested_baseline=main")
        assert "issue=#3564" in msg
        assert f"path={PATH}" in msg
        assert "expected_baseline=arch-weekly-11-ljt" in msg
        assert "actual_head=feat/issue-3564@abc123" in msg
        assert "ahead=2" in msg and "behind=5" in msg
        assert "reason=diverged" in msg

        errors = _task_errors("t_4023_visible")
        rec = errors[0]
        assert isinstance(rec, dict), "完整错误应结构化落库（JSON 优先）"
        assert rec["error_code"] == "WORKSPACE_BASELINE_MISMATCH"
        assert rec["issue"] == "3564"
        assert rec["path"] == PATH
        assert rec["expected_baseline"] == "arch-weekly-11-ljt"
        assert rec["requested_baseline"] == "main"
        assert rec["actual_head"] == "feat/issue-3564@abc123"
        assert rec["ahead"] == 2 and rec["behind"] == 5
        assert rec["reason"] == "diverged"
        assert "workspace baseline mismatch" in rec["message"]


def _fail_once(i):
    seq = 40240 + i
    tid = f"t_4023_repeat_{i}"
    _insert_event(seq)
    _insert_task(tid, seq)
    service.fail_task(tid, BASELINE_TEXT, exit_code=-1)


class TestBaselineRepeatAlert:
    def test_third_consecutive_failure_triggers_error_alert(self, caplog):
        caplog.set_level(logging.INFO)
        for i in range(3):
            _fail_once(i)

        alerts = [r for r in caplog.records
                  if "baseline_mismatch_repeat" in r.getMessage()]
        assert len(alerts) == 1, "第 3 次才触发打点，不应重复"
        assert alerts[0].levelno == logging.ERROR, "达阈值须升级 ERROR"
        alert = alerts[0].getMessage()
        assert "issue=#3564" in alert
        assert "reason=diverged" in alert
        assert "count=3" in alert
        assert "threshold=3" in alert

    def test_two_consecutive_failures_do_not_trigger(self, caplog):
        caplog.set_level(logging.INFO)
        for i in range(2):
            _fail_once(i)

        alerts = [r for r in caplog.records
                  if "baseline_mismatch_repeat" in r.getMessage()]
        assert alerts == [], "未达阈值（2<3）不得打点"

    def test_reason_change_resets_consecutive_chain(self):
        for _ in range(2):
            service._record_baseline_failure("3564", "diverged")
        service._record_baseline_failure("3564", "unreachable")
        rec = service._record_baseline_failure("3564", "diverged")
        assert rec["count"] == 1
        assert rec["triggered"] is False

class TestBaselineFailureKeyNormalization:
    def test_success_reset_with_hash_prefix_clears_chain(self):
        for _ in range(2):
            service._record_baseline_failure("3564", "diverged")
        rec = service._record_baseline_failure("3564", "diverged")
        assert rec["count"] == 3
        assert rec["triggered"] is True

        service._reset_baseline_failure("#3564")

        rec = service._record_baseline_failure("3564", "diverged")
        assert rec["count"] == 1, "成功后失败链须清空，再次失败应从 1 重新计数"
        assert rec["triggered"] is False

    def test_issue_key_normalizes_hash_prefix(self):
        assert service._baseline_issue_key("#3564") == "3564"
        assert service._baseline_issue_key("3564") == "3564"
        assert service._baseline_issue_key(" #3564 ") == "3564"
        assert service._baseline_issue_key("") == "-"
        assert service._baseline_issue_key(None) == "-"
