"""Issue #3763：runtime 库缺 autopilot_executions 时必须 fail-fast 告警（不静默吞异常）。

覆盖：
- 表存在性自检缺失 → AutopilotSchemaError，消息含表名与期望域 runtime；
- _create_execution 缺表 fail-fast；
- INSERT 命中 1146 时包装为 AutopilotSchemaError 并保留原始异常（cause）；
- _execute_task 不再把该异常吞成普通 ERROR（日志带 FAIL-FAST + 表名/域）。
"""
import asyncio
import contextlib

import pytest

from src.domains.runtime.engine import scheduler as sched


def test_missing_table_self_check_raises_with_table_and_domain(monkeypatch):
    monkeypatch.setattr(sched, "_autopilot_executions_table_present", lambda: False)
    with pytest.raises(sched.AutopilotSchemaError) as ei:
        sched._assert_autopilot_executions_table()
    msg = str(ei.value)
    assert "autopilot_executions" in msg
    assert "runtime" in msg


def test_create_execution_fails_fast_when_table_missing(monkeypatch):
    monkeypatch.setattr(sched, "_autopilot_executions_table_present", lambda: False)
    with pytest.raises(sched.AutopilotSchemaError) as ei:
        sched._create_execution(1, "issue")
    assert "autopilot_executions" in str(ei.value)
    assert "runtime" in str(ei.value)


def test_create_execution_wraps_1146_and_keeps_original_cause(monkeypatch):
    monkeypatch.setattr(sched, "_autopilot_executions_table_present", lambda: True)

    class _BoomCursor:
        def execute(self, *a, **k):
            raise RuntimeError(
                "(1146, \"Table 'grape_runtime_db.autopilot_executions' doesn't exist\")")

    @contextlib.contextmanager
    def _boom_cursor(domain, write=False):
        assert domain == "runtime", f"写路径应走 runtime 域游标，实际 {domain}"
        yield _BoomCursor()

    monkeypatch.setattr(sched, "_rt_cursor_for", _boom_cursor)

    with pytest.raises(sched.AutopilotSchemaError) as ei:
        sched._create_execution(1, "issue")
    assert "autopilot_executions" in str(ei.value)
    assert "runtime" in str(ei.value)
    assert ei.value.__cause__ is not None
    assert "1146" in str(ei.value.__cause__)


def test_table_presence_probe_routes_to_runtime_domain(monkeypatch):
    """三库拓扑下探针必须查 runtime owner 库，不能停在进程（mgmt）库。"""
    seen = []

    class _ProbeCursor:
        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), params))
            return self

        def fetchone(self):
            return (1,)

    @contextlib.contextmanager
    def _runtime_cursor(domain, write=False):
        seen.append(("DOMAIN", domain))
        yield _ProbeCursor()

    monkeypatch.setattr(sched, "_rt_cursor_for", _runtime_cursor)
    assert sched._autopilot_executions_table_present() is True
    assert ("DOMAIN", "runtime") in seen, seen


def test_execute_task_logs_schema_error_instead_of_swallowing(monkeypatch, caplog):
    monkeypatch.setattr(sched, "_update_task_run_info", lambda *a, **k: None)

    def _boom(*a, **k):
        raise sched.AutopilotSchemaError(
            "autopilot_executions 表缺失（期望域=runtime）：模拟缺表")

    monkeypatch.setattr(sched, "_create_execution", _boom)
    task = {
        "id": 3763, "name": "probe", "repo_key": "acme/demo", "agent_type": "issue",
        "instruction": "跑一次巡检", "skill_name": "", "cron": "*/30 * * * *",
        "max_retry": 1, "created_by": 0, "engine_runtime_ids": "",
        "custom_fields": "", "work_branch": "", "engine_runtime_uids": "",
    }

    with caplog.at_level("ERROR"):
        asyncio.run(sched._execute_task(task))

    assert 3763 not in sched._RUNNING_TASKS
    assert any(
        "FAIL-FAST" in r.message
        and "autopilot_executions" in r.message
        and "runtime" in r.message
        for r in caplog.records), [r.message for r in caplog.records]
