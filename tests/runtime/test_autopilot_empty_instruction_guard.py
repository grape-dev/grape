"""autopilot 派发端「空指令任务」守卫（回归）。

创建端已经拒收空指令（`src/domains/mgmt/routes/autopilot.py` 的 instruction_not_empty），
但存量行仍可能是 enabled + 空指令（历史数据、直接改库、早期版本写入）。调度器此前不拦，
于是按 cron 反复生成空壳 execution 与空壳 agent_task：每 30 分钟一次，执行必然空转失败，
运营侧只看到「自动化任务一直在失败」。

这里锁住两处入口：cron 派发（`_get_enabled_tasks` 过滤）与手动触发/其它调用
（`_execute_task` 早退，且不写 last_run、不建 execution）。
"""
import asyncio

import pytest

from src.domains.mgmt import public as mgmt_public
from src.domains.runtime.engine import scheduler as sched


def _row(task_id, name, instruction, cron="*/30 * * * *"):
    """list_enabled_autopilot_tasks_any 的行形态（13 列，列序见 _get_enabled_tasks）。"""
    return (task_id, name, "acme/demo", "coding", instruction, "", cron, 1, 0, "", "", "", "")


def test_get_enabled_tasks_skips_empty_instruction(monkeypatch, caplog):
    monkeypatch.setattr(
        mgmt_public, "list_enabled_autopilot_tasks_any",
        lambda: [_row(8, "placeholder", "   "), _row(9, "real", "跑一次构建")])

    with caplog.at_level("WARNING"):
        tasks = sched._get_enabled_tasks()

    assert [t["id"] for t in tasks] == [9]
    assert any("empty instruction" in r.message and "8(" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_execute_task_refuses_empty_instruction(monkeypatch, caplog):
    called = {"create": 0, "last_run": 0}
    monkeypatch.setattr(sched, "_create_execution",
                        lambda *a, **k: called.__setitem__("create", called["create"] + 1) or 1)
    monkeypatch.setattr(sched, "_update_task_run_info",
                        lambda *a, **k: called.__setitem__("last_run", called["last_run"] + 1))

    task = {"id": 8, "name": "placeholder", "repo_key": "acme/demo", "agent_type": "coding",
            "instruction": "  ", "skill_name": "", "cron": "*/30 * * * *", "max_retry": 1,
            "created_by": 0, "engine_runtime_ids": "", "custom_fields": "",
            "work_branch": "", "engine_runtime_uids": ""}

    with caplog.at_level("WARNING"):
        asyncio.run(sched._execute_task(task))

    assert called == {"create": 0, "last_run": 0}
    assert 8 not in sched._RUNNING_TASKS
    assert any("empty instruction" in r.message for r in caplog.records)
