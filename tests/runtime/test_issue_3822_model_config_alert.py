"""Issue #3822 轨 C-1：同一 task 24h 内 `Model not found` ≥2 次 → 升级「配置故障」告警。

背景：生产 24h 内 125 次真实 `Model not found`、39 个任务，同一任务被重试放大 3 倍，
错误以 `upstream_transient` 呈现，值班按「上游抖动」放行。与 #1985（引擎永久离线熔断）
区分：本条是「模型配置错位反复命中」，不是引擎下线。

本用例锁定公开契约：
  1. `record_model_config_error`：累计同一 task 24h 内次数（`agent_tasks.errors` 的
     `MODEL_CONFIG_ALERT` 条目唯一条目，含 count / first_seen_at / last_seen_at / escalated）；
  2. ≥2 次 → `escalated=True` + `log.error` 固定签名 `[MODEL_CONFIG_ALERT]`（Loki 可查）；
  3. 24h 窗口外的旧计数不参与累计（重置为 1）；
  4. `_do_requeue` 复位 errors 时保留计数器（跨轮次/跨 requeue 不丢）；
  5. `retry_transient_task` 的 config_error 分支不重试但仍完成累计与升级。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import transient_retry as tr

TID = "t_3822_config_alert"
REPO = "gitcode:issue3822/alert"
MODEL_ERR = "Not Found: Model not found"


class _FakeLog:
    def __init__(self):
        self.records = []

    def _rec(self, level, msg):
        self.records.append((level, msg))

    def info(self, msg, **kw):
        self._rec("info", msg)

    def warning(self, msg, **kw):
        self._rec("warning", msg)

    def error(self, msg, **kw):
        self._rec("error", msg)

    def debug(self, msg, **kw):
        self._rec("debug", msg)

    def errors(self):
        return [m for lvl, m in self.records if lvl == "error"]


@pytest.fixture(autouse=True)
def _fake_log(monkeypatch):
    fake = _FakeLog()
    monkeypatch.setattr(tr, "log", fake)
    _clean()
    yield fake
    _clean()


def _clean():
    with db_lock:
        try:
            db.cursor().execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
            db.commit()
        except Exception:
            pass


def _seed(errors=None, status="running", attempts=0, runtime_id=0, event_seq=None,
          pool_tag="", pool_agent_type=""):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, attempts, "
            "stream_ended, self_reported, result_text, event_seq, errors, exit_code, started_at, "
            "pool_tag, pool_agent_type) "
            "VALUES (?,?,?,?,'coding','a2a_event',?, 'x', '', '{}', '{}', '#3822', ?, 0, 0, '', ?, ?, NULL, "
            "'2020-01-01T00:00:00+00:00', ?, ?)",
            (TID, "sess-3822", runtime_id, REPO, status, attempts, event_seq,
             json.dumps(errors) if errors else None, pool_tag, pool_agent_type))
        db.commit()


def _errors():
    with db_lock:
        row = db.cursor().execute("SELECT errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []


def _alerts():
    return [e for e in _errors()
            if isinstance(e, dict) and e.get("error_code") == tr.MODEL_CONFIG_ALERT]


def _ts(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class TestRecordModelConfigError:
    def test_first_occurrence_counts_one_without_escalation(self, _fake_log):
        _seed()
        res = tr.record_model_config_error(TID, MODEL_ERR)
        assert res["count"] == 1 and res["escalated"] is False
        alerts = _alerts()
        assert len(alerts) == 1
        assert alerts[0]["count"] == 1 and alerts[0]["escalated"] is False
        assert alerts[0]["error_class"] == tr.CONFIG_ERROR
        assert not _fake_log.errors()

    def test_second_occurrence_escalates_with_log_signature(self, _fake_log):
        _seed()
        tr.record_model_config_error(TID, MODEL_ERR)
        res = tr.record_model_config_error(TID, MODEL_ERR)
        assert res["count"] == 2 and res["escalated"] is True
        alerts = _alerts()
        assert len(alerts) == 1, "同一 task 只保留一条 MODEL_CONFIG_ALERT 条目"
        assert alerts[0]["count"] == 2 and alerts[0]["escalated"] is True
        assert alerts[0].get("escalated_at")
        errs = _fake_log.errors()
        assert any("[MODEL_CONFIG_ALERT]" in m for m in errs)
        assert any("Model not found" in m for m in errs)

    def test_escalation_logged_once(self, _fake_log):
        _seed()
        for _ in range(3):
            tr.record_model_config_error(TID, MODEL_ERR)
        assert len([m for m in _fake_log.errors() if "[MODEL_CONFIG_ALERT]" in m]) == 1, \
            "升级日志只应在首次跨过阈值时打一次，避免刷屏"
        assert _alerts()[0]["count"] == 3

    def test_counts_outside_24h_window_reset(self):
        _seed(errors=[{
            "error_code": tr.MODEL_CONFIG_ALERT, "error_class": tr.CONFIG_ERROR,
            "count": 5, "first_seen_at": _ts(hours=30), "last_seen_at": _ts(hours=30),
            "escalated": True, "escalated_at": _ts(hours=30), "reason": MODEL_ERR,
        }])
        res = tr.record_model_config_error(TID, MODEL_ERR)
        assert res["count"] == 1 and res["escalated"] is False
        entry = _alerts()[0]
        assert entry["count"] == 1 and entry["escalated"] is False

    def test_other_error_entries_preserved(self):
        _seed(errors=[{"error_code": "NO_TOKEN", "reason": "no runtime token"}])
        tr.record_model_config_error(TID, MODEL_ERR)
        codes = [e.get("error_code") for e in _errors() if isinstance(e, dict)]
        assert "NO_TOKEN" in codes and tr.MODEL_CONFIG_ALERT in codes

    def test_missing_task_is_noop(self):
        res = tr.record_model_config_error("t_3822_absent", MODEL_ERR)
        assert res["count"] == 0 and res["escalated"] is False


class TestRetryPathIntegration:
    def test_config_error_still_not_retried_but_counted(self, _fake_log, monkeypatch):
        _seed()
        assert tr.retry_transient_task(TID, 1, MODEL_ERR) is False
        assert _alerts()[0]["count"] == 1
        assert tr.retry_transient_task(TID, 1, MODEL_ERR) is False
        assert _alerts()[0]["count"] == 2 and _alerts()[0]["escalated"] is True
        dumped = json.dumps(_errors(), ensure_ascii=False)
        assert "upstream_transient" not in dumped


class TestRequeuePreservesCounter:
    def test_requeue_keeps_model_config_alert_entry(self, monkeypatch):
        monkeypatch.setattr(tr.threading, "Timer", _NoopTimer)
        _seed(status="running", errors=[{
            "error_code": tr.MODEL_CONFIG_ALERT, "error_class": tr.CONFIG_ERROR,
            "count": 2, "first_seen_at": _ts(minutes=10), "last_seen_at": _ts(minutes=1),
            "escalated": True, "escalated_at": _ts(minutes=1), "reason": MODEL_ERR,
        }], pool_tag="coding", pool_agent_type="coding")
        tr._do_requeue(TID)
        entries = _errors()
        codes = [e.get("error_code") for e in entries if isinstance(e, dict)]
        assert "TRANSIENT_RETRY" in codes
        alerts = [e for e in entries if isinstance(e, dict)
                  and e.get("error_code") == tr.MODEL_CONFIG_ALERT]
        assert alerts and alerts[0]["count"] == 2 and alerts[0]["escalated"] is True

    def test_requeue_unaffected_without_alert_entry(self, monkeypatch):
        monkeypatch.setattr(tr.threading, "Timer", _NoopTimer)
        _seed(status="running", errors=[{"error_code": "NO_TOKEN", "reason": "x"}])
        tr._do_requeue(TID)
        codes = [e.get("error_code") for e in _errors() if isinstance(e, dict)]
        assert codes == ["TRANSIENT_RETRY"]


class _NoopTimer:
    def __init__(self, delay, fn, args=None, kwargs=None):
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass