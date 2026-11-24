"""Issue #3820：opencode 404 `Model not found` 归类 config_error（不可重试）。

根因：配置模型名不在网关 key 可用列表时，opencode 报 `Not Found: Model not found`；
该文本被当作上游瞬时抖动进入 `upstream_transient` 重试链路，重试耗尽后必败。
本用例锁定公开契约：
  1. 404 + model not found → `config_error`（transient=False）；
  2. 首次失败即判死：`retry_transient_task` 返回 False、不安排 Timer、任务保持 running；
  3. 记录里只出现 config_error，不出现 upstream_transient；
  4. 反例（普通业务文本含 "model not found"）不误判，既有瞬时分类回归不变。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import transient_retry as tr

TID = "t_3820_config"
REPO = "gitcode:issue3820/test"


class _FakeTimer:
    """替身 threading.Timer：只记录安排，不启动线程。"""

    fired = []

    def __init__(self, delay, fn, args=None, kwargs=None):
        self.delay, self.fn, self.args = delay, fn, args or ()
        self.daemon = False

    def start(self):
        _FakeTimer.fired.append((self.delay, self.args))

    def cancel(self):
        pass


@pytest.fixture(autouse=True)
def _fixture(monkeypatch):
    monkeypatch.setattr(tr.threading, "Timer", _FakeTimer)
    _FakeTimer.fired.clear()
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
        except Exception:
            pass
        db.commit()


def _seed_task(status="running", attempts=0, errors=None, event_seq=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, attempts, "
            "stream_ended, self_reported, result_text, event_seq, errors, exit_code, started_at) "
            "VALUES (?,?,0,?, 'coding', 'a2a_event', ?, 'x', '', '{}', '{}', '#3820', ?, "
            "0, 0, '', ?, ?, NULL, '2020-01-01T00:00:00+00:00')",
            (TID, "sess-3820", REPO, status, attempts, event_seq,
             json.dumps(errors) if errors else None))
        db.commit()
    return TID


def _task_row():
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT status, attempts, errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()


def _task_errors():
    row = _task_row()
    if row and row[2]:
        try:
            return json.loads(row[2])
        except Exception:
            return []
    return []


# ── 分类：404 + Model not found → config_error ──

@pytest.mark.parametrize("txt", [
    "Not Found: Model not found",                       # opencode NDJSON error 事件实测文案
    "HTTP 404: Model not found",                        # HTTP 状态 + 模型不存在
    '{"error":"model_not_found","statusCode":404}',     # OpenAI 兼容错误体
    "AI_APICallError: Not Found: Model not found",      # ai-sdk 包装后的文案
])
def test_classify_model_not_found_as_config_error(txt):
    r = tr.classify_stream_failure(1, txt, has_output=False)
    assert r["error_class"] == tr.CONFIG_ERROR
    assert r["transient"] is False


def test_config_error_wins_over_retry_keywords():
    """即便文本同时带 retry 字样，配置错误也不得进重试。"""
    r = tr.classify_stream_failure(
        1, "Not Found: Model not found (retryable: true, please retry)", has_output=False)
    assert r["error_class"] == tr.CONFIG_ERROR
    assert r["transient"] is False


@pytest.mark.parametrize("txt", [
    "tests failed: model not found in expected list",   # 业务文案，无 404 状态上下文
    "assertion error: model file not found",
])
def test_plain_model_not_found_text_not_misclassified(txt):
    r = tr.classify_stream_failure(1, txt, has_output=True)
    assert r["error_class"] != tr.CONFIG_ERROR
    assert r["transient"] is False


def test_transient_classification_unchanged():
    """回归：既有瞬时分类不受新增 config 规则影响。"""
    r = tr.classify_stream_failure(-1, "connection reset by peer", has_output=False)
    assert r["transient"] is True and r["error_class"] == tr.TRANSIENT


# ── 判死：0 重试 + 记录无 upstream_transient ──

def test_retry_transient_task_does_not_retry_config_error():
    _seed_task()
    retried = tr.retry_transient_task(TID, 1, "Not Found: Model not found")
    assert retried is False
    assert _FakeTimer.fired == [], "config_error 不得安排退避重试"
    row = _task_row()
    assert row[0] == "running"          # 未进入 requeue（状态由 fail_task 判死）
    errs = _task_errors()
    assert any(isinstance(e, dict) and e.get("error_code") == "CONFIG_ERROR"
               and e.get("error_class") == tr.CONFIG_ERROR for e in errs)
    dumped = json.dumps(errs, ensure_ascii=False)
    assert "upstream_transient" not in dumped


def test_failure_summary_labels_config_error():
    text = tr.format_stream_failure_result(
        tr.CONFIG_ERROR, 0, 3, reason="Not Found: Model not found", exit_code=1)
    assert "模型配置错误（不可重试）" in text
    assert "未自动重试" in text
    assert "上游瞬时抖动" not in text