"""autopilot 派发端「空指令任务」守卫 + 不可派发状态（Issue #3733 回归）。

创建端已经拒收空指令（`src/domains/mgmt/routes/autopilot.py` 的 instruction_not_empty），
但存量行仍可能是 enabled + 空指令（历史数据、直接改库、早期版本写入）。PR #1675 起调度器
会拦下它们，但拦下之后**只靠日志表达**：每 tick 打一条 WARNING、7 天 9349 条，与真实 ERROR
同量级，而任务从来没被派发过（运营侧只看到「自动化一直在失败」）。

Issue #3733 把「拦截」升级成显式状态（库内为跨进程权威，进程内只做节流）：
- `_get_enabled_tasks()` 跳过语义不变，但只在**状态真的变更**时留痕一次，稳态零 WARNING；
- `_execute_task()` / `trigger_task_now()` 对空指令任务写状态 + 返回明确原因，
  不建 execution、不写 `last_run_at`；
- 指令补齐后（库里不再是空指令）自动恢复派发。

本文件用一张内存版 `autopilot_tasks` 表（读走 `list_enabled_autopilot_tasks_any`，
写走 `set_autopilot_dispatch_state_any`）驱动，`set_state` 返回 0 行表示库内状态未变——
这正是「跨进程判重以库内为准」的口径。
"""
import asyncio

import pytest

from src.domains.mgmt import public as mgmt_public
from src.domains.mgmt.autopilot_state import (
    AUTOPILOT_EMPTY_INSTRUCTION_REASON,
    AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION as BLOCKED,
)
from src.domains.runtime.engine import scheduler as sched

_AT = "2026-11-22T00:00:00+00:00"


class _FakeTaskTable:
    """内存版 autopilot_tasks：库内状态才是跨进程权威，写 0 行 = 状态未变更。"""

    def __init__(self, rows):
        self.rows = {int(r["id"]): dict(r, state=r.get("state", ""),
                                        reason=r.get("reason", ""), at=r.get("at", ""))
                     for r in rows}
        self.writes = []

    @staticmethod
    def _tuple(r):
        """list_enabled_autopilot_tasks_any 的行形态（21 列，列序见 _get_enabled_tasks）。

        Issue #3800 的退避/熔断五列（13..17）在状态三列之前，故 dispatch_state
        位于 18..20（与 `autopilot_tasks_enabled_full` / `autopilot_task_full_row` 一致）。
        """
        return (r["id"], r["name"], "acme/demo", "coding", r["instruction"], "",
                r.get("cron", "*/30 * * * *"), 1, 0, "", "", "", "",
                0, "", "", "", 0,
                r["state"], r["reason"], r["at"])

    def list_enabled(self):
        return [self._tuple(r) for r in self.rows.values()]

    def row(self, task_id):
        return self._tuple(self.rows[int(task_id)])

    def set_state(self, task_id, state, reason):
        self.writes.append((int(task_id), state, reason))
        r = self.rows.get(int(task_id))
        if r is None or r["state"] == state:
            return 0          # 库内已是该状态 → 0 行（并发副本里只有一处能写到）
        r["state"], r["reason"], r["at"] = state, reason, _AT
        return 1

    def install(self, monkeypatch):
        monkeypatch.setattr(mgmt_public, "list_enabled_autopilot_tasks_any", self.list_enabled)
        monkeypatch.setattr(mgmt_public, "autopilot_task_row_any", self.row)
        monkeypatch.setattr(mgmt_public, "set_autopilot_dispatch_state_any",
                            self.set_state, raising=False)


@pytest.fixture(autouse=True)
def _reset_process_memo(monkeypatch):
    """进程内节流记忆是模块级状态：逐用例清空，避免跨用例互相干扰。"""
    monkeypatch.setattr(sched, "_NOT_DISPATCHABLE_SEEN", {}, raising=False)


def _warns(caplog):
    return [r.message for r in caplog.records
            if r.levelname == "WARNING" and "empty instruction" in r.message]


def _patched_side_effects(monkeypatch):
    """execution / last_run_at 计数：空指令任务这两条都不许碰。"""
    called = {"create": 0, "last_run": 0}

    def _create(*a, **k):
        called["create"] += 1
        return 1

    def _run_info(*a, **k):
        called["last_run"] += 1

    monkeypatch.setattr(sched, "_create_execution", _create)
    monkeypatch.setattr(sched, "_update_task_run_info", _run_info)
    return called


def _task(task_id=6, name="Issue看板巡检", instruction="", state="", cron="0 9 * * *"):
    return {"id": task_id, "name": name, "repo_key": "acme/demo", "agent_type": "coding",
            "instruction": instruction, "skill_name": "", "cron": cron, "max_retry": 1,
            "created_by": 0, "engine_runtime_ids": "", "custom_fields": "",
            "work_branch": "", "engine_runtime_uids": "", "dispatch_state": state}


# ---------- cron 派发路径：多 tick 单次留痕 + 状态落库 ----------

def test_multi_tick_marks_state_once_and_warns_once(monkeypatch, caplog):
    table = _FakeTaskTable([
        {"id": 6, "name": "Issue看板巡检", "instruction": ""},
        {"id": 9, "name": "real", "instruction": "跑一次构建"},
    ])
    table.install(monkeypatch)

    with caplog.at_level("WARNING"):
        seen = [sched._get_enabled_tasks() for _ in range(5)]

    # 跳过语义不变：空指令任务永不进派发列表
    assert [[t["id"] for t in tick] for tick in seen] == [[9]] * 5
    # 5 个 tick 只留痕一次（日志量与 tick 频率解耦）
    assert len(_warns(caplog)) == 1, _warns(caplog)
    # 状态落库：后续 tick 从库内读到的已是 blocked → 不再写库（只写了一次）
    assert table.rows[6]["state"] == BLOCKED
    assert table.rows[6]["reason"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON
    assert table.rows[6]["at"] == _AT
    assert len(table.writes) == 1, table.writes
    # 指令非空的任务零改动、不被标记
    assert table.rows[9]["state"] == ""


def test_already_blocked_task_is_silent_across_ticks(monkeypatch, caplog):
    """库内已是 blocked（别的副本写过 / 本进程重启前写过）：不重复写、不刷 WARNING。"""
    table = _FakeTaskTable([
        {"id": 6, "name": "Issue看板巡检", "instruction": "", "state": BLOCKED,
         "reason": AUTOPILOT_EMPTY_INSTRUCTION_REASON, "at": _AT},
    ])
    table.install(monkeypatch)

    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert sched._get_enabled_tasks() == []

    assert _warns(caplog) == []
    assert table.writes == [], "库内已是权威状态，不得重复写"


def test_filling_instruction_resumes_dispatch(monkeypatch, caplog):
    """补齐指令后（库内不再是空指令）立刻恢复派发，状态列由接口侧清理。"""
    table = _FakeTaskTable([
        {"id": 6, "name": "Issue看板巡检", "instruction": "1. 检查 stale issue\n2. 提醒更新或关闭",
         "state": BLOCKED, "reason": AUTOPILOT_EMPTY_INSTRUCTION_REASON, "at": _AT},
    ])
    table.install(monkeypatch)

    with caplog.at_level("WARNING"):
        tasks = sched._get_enabled_tasks()

    assert [t["id"] for t in tasks] == [6]
    assert tasks[0]["instruction"].startswith("1. 检查 stale issue")
    assert table.writes == []
    assert _warns(caplog) == []


# ---------- 手动触发路径：写状态 + 明确原因，零副作用 ----------

def test_trigger_now_refuses_empty_instruction(monkeypatch, caplog):
    table = _FakeTaskTable([{"id": 6, "name": "Issue看板巡检", "instruction": ""}])
    table.install(monkeypatch)
    called = _patched_side_effects(monkeypatch)
    executed = {"n": 0}

    async def _fake_execute(task):
        executed["n"] += 1

    monkeypatch.setattr(sched, "_execute_task", _fake_execute)

    with caplog.at_level("WARNING"):
        result = asyncio.run(sched.trigger_task_now(6))

    assert result["ok"] is False
    assert result["message"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON
    assert called == {"create": 0, "last_run": 0}, "拒绝路径不得建 execution / 写 last_run_at"
    assert executed["n"] == 0, "拒绝路径不得调度执行协程"
    assert table.rows[6]["state"] == BLOCKED, "拒绝也要落库状态（界面据此展示原因）"
    assert len(_warns(caplog)) == 1


def test_trigger_now_on_blocked_task_does_not_repeat_warning(monkeypatch, caplog):
    table = _FakeTaskTable([
        {"id": 6, "name": "Issue看板巡检", "instruction": "", "state": BLOCKED,
         "reason": AUTOPILOT_EMPTY_INSTRUCTION_REASON, "at": _AT},
    ])
    table.install(monkeypatch)
    called = _patched_side_effects(monkeypatch)

    with caplog.at_level("WARNING"):
        results = [asyncio.run(sched.trigger_task_now(6)) for _ in range(3)]

    assert all(r["ok"] is False for r in results)
    assert all(r["message"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON for r in results)
    assert called == {"create": 0, "last_run": 0}
    assert _warns(caplog) == [], "稳态手动触发不得刷 WARNING"
    assert table.writes == [], "状态未变更不重复写库"


def test_execute_task_refuses_empty_instruction(monkeypatch, caplog):
    """其它调用方直调 `_execute_task` 的兜底守卫：不建 execution、不写 last_run。"""
    table = _FakeTaskTable([{"id": 6, "name": "Issue看板巡检", "instruction": ""}])
    table.install(monkeypatch)
    called = _patched_side_effects(monkeypatch)

    with caplog.at_level("WARNING"):
        asyncio.run(sched._execute_task(_task()))

    assert called == {"create": 0, "last_run": 0}
    assert 6 not in sched._RUNNING_TASKS
    assert table.rows[6]["state"] == BLOCKED
    assert len(_warns(caplog)) == 1


def test_execute_task_refusal_logs_once_across_repeated_calls(monkeypatch, caplog):
    """每次调用都带上库内最新状态（真实调用方每次重读行）：只留痕一次、只写一次。"""
    table = _FakeTaskTable([{"id": 6, "name": "Issue看板巡检", "instruction": ""}])
    table.install(monkeypatch)
    _patched_side_effects(monkeypatch)

    with caplog.at_level("WARNING"):
        for _ in range(4):
            asyncio.run(sched._execute_task(_task(state=table.rows[6]["state"])))

    assert len(_warns(caplog)) == 1, _warns(caplog)
    assert len(table.writes) == 1, table.writes
