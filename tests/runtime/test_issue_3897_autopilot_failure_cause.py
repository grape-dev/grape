"""Issue #3897 A 组：Autopilot 失败必须有 cause，且能解释 `0 retries`。

现场（生产 Loki，grape-worker，每 60s 一条）：

    Autopilot: task 83 exec <id> failed after 0 retries (failed)

日志只有结果没有原因。代码级根因：`_finalize_autopilot_task` 在 exit_code != 0 时只写
`status=failed`、`error=""`；`_execute_task` 读到空 error 后回落到字面量
`failed after {max_retry} retries` —— 既没有 cause，也不解释为什么 max_retry=0 一次都不重试。

本文件锁三条契约（公开 seam）：
  1. `_autopilot_failure_cause(exit_code, final_text)`：恒返回非空 cause（含 exit_code 与输出尾行）；
  2. 失败 exec 的 `autopilot_executions.error` 落结构化 cause（`_finalize_autopilot_task`）；
  3. `_retry_skip_reason(...)` 把「为什么 0 重试」写成可读原因，收口日志必须带
     `retries_attempted=… max_retry=… retry_skip=…`。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import scheduler as S

EXEC_BASE = 389_700
TASK_ID = 389_700


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id BETWEEN ? AND ?",
                  (EXEC_BASE, EXEC_BASE + 99))
        db.commit()


def _mk_exec(exec_id: int) -> None:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions (id, task_id, status, agent_type, error) "
            "VALUES (?, ?, 'running', 'issue', '')", (exec_id, TASK_ID))
        db.commit()


def _exec_error(exec_id: int) -> str:
    with db_lock:
        row = db.cursor().execute(
            "SELECT error FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
    return (row[0] if row else "") or ""


# ---------------------------------------------------------------- 1. cause 纯函数

def test_failure_cause_carries_exit_code_and_output_tail():
    cause = S._autopilot_failure_cause(1, "some log line\nfinal agent output")
    assert "exit_code=1" in cause
    assert "final agent output" in cause


def test_failure_cause_is_never_empty_when_agent_prints_nothing():
    cause = S._autopilot_failure_cause(1, "")
    assert cause.strip()
    assert "exit_code=1" in cause
    assert "no output" in cause


def test_failure_cause_truncates_long_output():
    cause = S._autopilot_failure_cause(2, "x" * 5000)
    assert "exit_code=2" in cause
    assert len(cause) < 1000


# ---------------------------------------------------------------- 2. 落库 cause

def test_finalize_writes_failure_cause_into_execution_row(monkeypatch):
    """exit_code != 0 → autopilot_executions.error 必须是可自诊断的 cause，不再是空串。"""
    _clean()
    exec_id = EXEC_BASE + 1
    _mk_exec(exec_id)

    monkeypatch.setattr("src.domains.runtime.engine.service.update_task_status",
                        lambda *a, **kw: None)
    monkeypatch.setattr(S, "_notify_subscribers", lambda *a, **kw: None)

    S._finalize_autopilot_task(str(TASK_ID), exec_id, 1, "agent log\nError: boom")

    err = _exec_error(exec_id)
    assert err, "失败 exec 必须落 cause（修复前恒为空，日志回落到 failed after 0 retries）"
    assert "exit_code=1" in err
    assert "Error: boom" in err


# ---------------------------------------------------------------- 3. 0 retries 依据

def test_retry_skip_reason_explains_zero_retries_config():
    reason = S._retry_skip_reason(max_retry=0, attempt=0, error_code="")
    assert "max_retry=0" in reason
    assert "no retry" in reason


def test_retry_skip_reason_reports_env_failure_code():
    reason = S._retry_skip_reason(max_retry=2, attempt=0,
                                  error_code=S.ERROR_ENGINE_OFFLINE)
    assert S.ERROR_ENGINE_OFFLINE in reason