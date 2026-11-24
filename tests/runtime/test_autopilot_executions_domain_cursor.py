# -*- coding: utf-8 -*-
"""autopilot_executions 读写必须走 runtime 域游标（手动触发 1146 守护）。

autopilot_executions 归 runtime 域（configs/table_ownership.json），但手动触发
（POST /autopilot/tasks/{id}/trigger）跑在 mgmt 进程：进程库（session_manager.db）
是 mgmt 库，表不在 → 1146 → execution 永远建不出来、页面点「立即触发」无任何
反馈（fire-and-forget 吞掉异常）。定时触发在 worker 进程不受影响。
"""
import inspect


def test_scheduler_imports_runtime_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    src = inspect.getsource(sch)
    assert 'from src.kernel.domain_db import cursor as _rt_domain_cursor' in src


def test_create_execution_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._create_execution)
    assert '_rt_domain_cursor("runtime")' in body, (
        "_create_execution 必须走 runtime 域游标——进程库(mgmt)里没有 "
        "autopilot_executions，手动触发会 1146")


def test_execute_task_result_read_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._execute_task)
    assert 'SELECT status, error FROM autopilot_executions' in body
    assert '_rt_domain_cursor("runtime")' in body, (
        "执行结果查询必须走 runtime 域游标（mgmt 进程 1146）")


def test_poll_dispatched_completion_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._poll_dispatched_completion)
    assert '_rt_domain_cursor("runtime")' in body, (
        "dispatched 轮询查 exec 行 + agent_tasks 都归 runtime——"
        "mgmt 进程手动触发路径下两表都不在进程库")


def test_update_execution_result_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._update_execution_result)
    assert '_rt_domain_cursor("runtime")' in body
