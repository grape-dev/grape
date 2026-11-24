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
     `attempts=… max_retry=… retry_skip=…`（attempts 含首次执行）。
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


def _exec_status(exec_id: int) -> str:
    with db_lock:
        row = db.cursor().execute(
            "SELECT status FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
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


def test_failure_cause_tolerates_missing_exit_code():
    """QA R1 Required：done event 的 exit_code 可为 None（CommandResult 初值）。

    修复前 `int(None)` 抛 TypeError，异常从 `_finalize_autopilot_task` 逃逸 →
    exec 行停留 running、结果/订阅/用量收尾全跳过。契约：容错映射 -1 且保留
    「未上报」注记（与真实返回 -1 可区分）。
    """
    cause = S._autopilot_failure_cause(None, "agent log\nError: boom")
    assert cause.strip()
    assert "exit_code=-1" in cause
    assert "None" in cause
    assert "Error: boom" in cause


def test_failure_cause_tolerates_non_numeric_exit_code():
    cause = S._autopilot_failure_cause("not-a-code", "boom")
    assert "exit_code=-1" in cause
    assert "boom" in cause


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


def test_finalize_with_none_exit_code_reaches_terminal_state(monkeypatch):
    """QA R1 Required 回归：exit_code=None 时收尾不得抛异常，exec 必须离开 running。

    修复前 `_autopilot_failure_cause(None, ...)` 抛 TypeError，从 `_finalize_autopilot_task`
    逃逸（:917/:934 无 try 包裹）：exec 行停留 running，`_update_execution_result` /
    订阅通知 / 用量收尾全部跳过。
    """
    _clean()
    exec_id = EXEC_BASE + 2
    _mk_exec(exec_id)

    monkeypatch.setattr("src.domains.runtime.engine.service.update_task_status",
                        lambda *a, **kw: None)
    monkeypatch.setattr(S, "_notify_subscribers", lambda *a, **kw: None)

    S._finalize_autopilot_task(str(TASK_ID), exec_id, None, "agent log\nError: boom")

    assert _exec_status(exec_id) == "failed"
    err = _exec_error(exec_id)
    assert "exit_code=-1" in err
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
