"""#2790 回归：会话 token 签发失败/派发上下文缺失必须留可诊断痕迹，不得静默降级。

旧行为：`enqueue_task` 里签发异常只 `log.warning` 后继续入队，任务日志/errors
看不到任何线索，agent 只能盲搜 `.grape-sess-*.tok` 自救。

口径（QA 复审后拆分）：
- 签发异常 → errors(error_code=SESSION_TOKEN_ISSUE_FAILED) + log.error；
- 根本没尝试签发（repo_key/agent_type 为空）→ errors(SESSION_TOKEN_CONTEXT_MISSING)
  + log.warning，避免正常任务（旧事件透传空值）被误记错误；
- 诊断条目带 message/diagnostic，events_view 不再显示裸 code，
  friendly_failure_hint 也不会把它当成真实失败原因。
"""
import json
import uuid
from unittest.mock import patch

from src.domains.runtime.engine.service import (
    SESSION_TOKEN_CONTEXT_MISSING,
    SESSION_TOKEN_ISSUE_FAILED,
    enqueue_task,
    friendly_failure_hint,
    issue_and_inject_session_token,
    _token_diag_entry,
)
from src.kernel.session_manager import db, db_lock


def _seed():
    with db_lock:
        c = db.cursor()
        sid = "t2790-%s" % uuid.uuid4().hex[:8]
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, skill) "
            "VALUES (?,?,?,?,'m2skill')", (sid, "gitcode:o/r", "vod", "idle"))
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, "
                  "healthy, status, scope, owner_id) "
                  "VALUES ('t2790rt','t2790rt',0,1,1,'online','global','')")
        rid = c.lastrowid
        db.commit()
    return sid, rid


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE agent_session_id LIKE 't2790-%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 't2790-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name='t2790rt'")
        db.commit()


def _errors_of(tid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT errors, env_extra FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    return json.loads(row[0] or "[]"), json.loads(row[1] or "{}")


def _enqueue_raises(sid, rid):
    with patch("src.kernel.auth_sdk.auth_token.issue_session_token",
               side_effect=RuntimeError("signer boom")):
        return enqueue_task(
            agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
            event_type="test", repo_key="gitcode:o/r", agent_type="vod",
            skill="m2skill", env_extra={})


def test_token_issue_failure_is_recorded_in_task_errors():
    _cleanup()
    sid, rid = _seed()
    tid = _enqueue_raises(sid, rid)

    errors, env = _errors_of(tid)
    codes = [e.get("error_code") for e in errors if isinstance(e, dict)]
    assert SESSION_TOKEN_ISSUE_FAILED in codes, f"应留签发失败诊断，实际 {errors}"
    entry = next(e for e in errors if e.get("error_code") == SESSION_TOKEN_ISSUE_FAILED)
    assert "signer boom" in entry["reason"], f"诊断应含原始异常，实际 {entry['reason']}"
    assert not env.get("GRAPE_SESSION_TOKEN"), "签发失败时不得写入空/伪 token"
    _cleanup()


def test_missing_dispatch_context_uses_separate_code_and_warning():
    """else 分支（根本没尝试签发）不得复用签发失败 code，也不得打 log.error。"""
    _cleanup()
    sid, rid = _seed()
    with patch("src.domains.runtime.engine.service.log.error") as err_log, \
            patch("src.domains.runtime.engine.service.log.warning") as warn_log:
        tid = enqueue_task(
            agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
            event_type="test", repo_key="", agent_type="",
            skill="m2skill", env_extra={})
    errors, env = _errors_of(tid)
    codes = [e.get("error_code") for e in errors if isinstance(e, dict)]
    assert codes == [SESSION_TOKEN_CONTEXT_MISSING], f"正常任务不应记成签发失败，实际 {errors}"
    assert not env.get("GRAPE_SESSION_TOKEN")
    assert err_log.call_count == 0, "未尝试签发（上下文缺失）不应打 log.error"
    assert warn_log.call_count >= 1, "上下文缺失应留一条 warning"
    _cleanup()


def test_token_diag_has_message_and_events_view_shows_it():
    """T3：新 error_code 必须带 message，否则 events_view 显示裸 code。"""
    from src.domains.insight.observability.events_view import _err_text
    entry = _token_diag_entry(SESSION_TOKEN_ISSUE_FAILED, "signer boom")
    assert entry.get("diagnostic") is True
    assert entry["message"] and "session token" in entry["message"].lower()
    assert _err_text(entry) == entry["message"]


def test_shared_helper_appends_diagnostic_to_existing_task_row():
    """direct/event_pipeline 两路复用同一 helper，签发失败要落到既有任务行。"""
    _cleanup()
    sid, rid = _seed()
    tid = enqueue_task(
        agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
        event_type="test", repo_key="gitcode:o/r", agent_type="vod",
        skill="m2skill", env_extra={})
    with patch("src.kernel.auth_sdk.auth_token.issue_session_token",
               side_effect=RuntimeError("direct boom")):
        entry = issue_and_inject_session_token(
            {}, session_key=tid, repo_key="gitcode:o/r", agent_type="vod",
            task_id=tid, scope="direct")
    assert entry and entry["error_code"] == SESSION_TOKEN_ISSUE_FAILED
    errors, _ = _errors_of(tid)
    assert any(e.get("error_code") == SESSION_TOKEN_ISSUE_FAILED for e in errors)
    _cleanup()


def test_friendly_failure_hint_does_not_hijack_failure_reason():
    """诊断在 errors 里，但真实失败原因优先（不得被 token 诊断抢占）。"""
    diag = _token_diag_entry(SESSION_TOKEN_ISSUE_FAILED, "signer boom")
    task = {
        "errors": json.dumps([diag, {"error_code": "REAL_FAIL", "reason": "db down"}],
                             ensure_ascii=False),
        "result_text": "", "stderr": "", "exit_code": 1,
    }
    hint, _action = friendly_failure_hint(task)
    assert "db down" in hint, f"应以真实错误为准，实际 {hint}"
    assert "session token" not in hint.lower(), f"诊断不得抢占失败原因，实际 {hint}"

    task2 = {"errors": json.dumps([diag], ensure_ascii=False),
             "result_text": "real engine crash", "stderr": "", "exit_code": 1}
    hint2, _ = friendly_failure_hint(task2)
    assert "real engine crash" in hint2, f"仅有诊断时应回退 result_text，实际 {hint2}"


def test_direct_paths_use_shared_token_diagnostic_helper():
    """静态守卫：event_pipeline / direct_executor / chat_session_env 不再各自静默 warning。"""
    import os
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    targets = {
        "src/domains/runtime/pipeline/event_pipeline.py": "issue_and_inject_session_token",
        "src/domains/runtime/engine/direct_executor.py": "issue_and_inject_session_token",
        "src/api/chat_session_env.py": "issue_and_inject_session_token",
    }
    for rel, marker in targets.items():
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            content = f.read()
        assert marker in content, f"{rel} 应复用统一诊断 helper"
        assert "issue session token failed: {e}" not in content, (
            f"{rel} 仍保留旧的静默 log.warning 路径")
