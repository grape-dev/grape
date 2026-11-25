"""Issue #3733：autopilot 任务出参与「保存指令 → 派发状态」联动（路由层契约）。

覆盖：
1. `_row_to_task` 出参新增 `dispatchable` / `dispatch_state` / `dispatch_state_reason` /
   `dispatch_state_at`，且**派发能力以当前 instruction 为准**——迁移还没跑、库内
   `dispatch_state` 仍是空串时，接口也不能说任务可派发（否则界面继续沉默）；
2. `PUT /tasks/{id}` 补齐非空指令 → 清空状态（cron 派发立即恢复）；提交空指令 → 立刻标记
   为不可派发（界面不必等下一轮扫描）；
3. `POST /tasks/{id}/trigger` 空指令 → 409 且 detail 带明确原因（不建 execution）。
"""
import sqlite3
import threading

import pytest
from fastapi import HTTPException

import src.domains.mgmt.routes.autopilot as ap
from src.domains.mgmt.autopilot_state import (
    AUTOPILOT_EMPTY_INSTRUCTION_REASON,
    AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION as BLOCKED,
)

_TASK_COLS = ("id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, "
              "enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
              "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids, "
              "dispatch_state, dispatch_state_reason, dispatch_state_at")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "ap3733.db"))
    conn.executescript("""
    CREATE TABLE autopilot_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT 'issue',
        instruction TEXT DEFAULT '',
        skill_name TEXT DEFAULT '',
        cron TEXT DEFAULT '*/30 * * * *',
        max_retry INTEGER DEFAULT 3,
        enabled INTEGER DEFAULT 1,
        last_run_at TEXT DEFAULT '',
        next_run_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        engine_runtime_ids TEXT DEFAULT '',
        custom_fields TEXT DEFAULT '',
        work_branch TEXT DEFAULT '',
        engine_runtime_uids TEXT DEFAULT '',
        dispatch_state TEXT NOT NULL DEFAULT '',
        dispatch_state_reason TEXT NOT NULL DEFAULT '',
        dispatch_state_at TEXT NOT NULL DEFAULT ''
    );
    """)
    conn.execute("INSERT INTO autopilot_tasks (name, repo_key, instruction, enabled) "
                 "VALUES ('Issue看板巡检', 'gitcode:acme/demo', '', 1)")
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(ap, "db", conn, raising=True)
    monkeypatch.setattr(ap, "db_lock", lock, raising=True)
    monkeypatch.setattr(ap, "check_repo_access", lambda *a, **k: None, raising=True)
    return conn, {"id": 0, "username": "alice", "auth_type": "session_token"}


def _row(conn, task_id=1):
    return conn.execute(f"SELECT {_TASK_COLS} FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()


# ---------- 出参：不可派发原因 ----------

def test_row_to_task_reports_not_dispatchable_from_instruction(env):
    """迁移未跑、库内状态为空串：出参仍按当前 instruction 如实报告不可派发。"""
    conn, _user = env
    task = ap._row_to_task(_row(conn))
    assert task["dispatchable"] is False
    assert task["dispatch_state_reason"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON
    assert task["dispatch_state"] == ""
    assert task["dispatch_state_at"] == ""


def test_row_to_task_dispatchable_when_instruction_present(env):
    conn, _user = env
    conn.execute("UPDATE autopilot_tasks SET instruction='1. 检查 stale issue' WHERE id=1")
    conn.commit()
    task = ap._row_to_task(_row(conn))
    assert task["dispatchable"] is True
    assert task["dispatch_state_reason"] == ""


def test_row_to_task_normalizes_stale_blocked_state_when_instruction_present(env):
    """指令经非 PUT 路径补齐（直接改库 / 回滚后写入 / 其它写入方）→ 出参不得自相矛盾。

    复审 Optional：库内残留 `blocked_empty_instruction` 时，旧实现会同时给出
    `dispatchable=true` 与 `dispatch_state_reason=「任务指令为空」`。
    """
    conn, _user = env
    conn.execute("UPDATE autopilot_tasks SET instruction='1. 检查 stale issue', dispatch_state=?, "
                 "dispatch_state_reason=?, dispatch_state_at='2026-11-22T00:00:00+00:00' WHERE id=1",
                 (BLOCKED, AUTOPILOT_EMPTY_INSTRUCTION_REASON))
    conn.commit()

    task = ap._row_to_task(_row(conn))

    assert task["dispatchable"] is True
    assert task["dispatch_state"] == ""
    assert task["dispatch_state_reason"] == "", "可派发却报「任务指令为空」= 自相矛盾"
    assert task["dispatch_state_at"] == "", "状态已不存在，进入时刻同源归零"
    # 归一化只发生在出参：库内残留值不动（调度器/迁移仍以库内状态为判重权威）
    assert _row(conn)[18:21] == (BLOCKED, AUTOPILOT_EMPTY_INSTRUCTION_REASON,
                                 "2026-11-22T00:00:00+00:00")


def test_row_to_task_tolerates_legacy_18_column_row():
    """降级/旧库列未补齐时，出参不得抛 IndexError（列缺失按「可派发」处理）。"""
    legacy = (1, "old", "", "issue", "跑一次构建", "", "0 9 * * *", 3, 1, "", "", "", "", 0,
              "", "", "", "")
    task = ap._row_to_task(legacy)
    assert task["dispatchable"] is True
    assert task["dispatch_state"] == ""


def test_list_tasks_exposes_dispatch_fields(env):
    conn, user = env
    conn.execute("UPDATE autopilot_tasks SET repo_key='', dispatch_state=?, dispatch_state_reason=?, "
                 "dispatch_state_at='2026-11-22T00:00:00+00:00' WHERE id=1",
                 (BLOCKED, AUTOPILOT_EMPTY_INSTRUCTION_REASON))
    conn.commit()
    tasks = ap.list_tasks(user, repo_key="", enabled=None)["tasks"]
    assert tasks[0]["dispatch_state"] == BLOCKED
    assert tasks[0]["dispatchable"] is False
    assert tasks[0]["dispatch_state_reason"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON
    assert tasks[0]["dispatch_state_at"] == "2026-11-22T00:00:00+00:00"


# ---------- 保存指令 → 状态联动 ----------

def test_update_with_instruction_clears_blocked_state(env):
    conn, user = env
    conn.execute("UPDATE autopilot_tasks SET dispatch_state=?, dispatch_state_reason=?, "
                 "dispatch_state_at='2026-11-22T00:00:00+00:00' WHERE id=1",
                 (BLOCKED, AUTOPILOT_EMPTY_INSTRUCTION_REASON))
    conn.commit()

    ap.update_task(1, ap.UpdateTaskBody(instruction="1. 检查 stale issue\n2. 提醒更新或关闭"), user)

    state, reason, at, instr = conn.execute(
        "SELECT dispatch_state, dispatch_state_reason, dispatch_state_at, instruction "
        "FROM autopilot_tasks WHERE id=1").fetchone()
    assert state == "" and reason == "" and at == "", "补齐指令必须清状态，cron 派发即恢复"
    assert instr.startswith("1. 检查 stale issue")


def test_update_with_empty_instruction_marks_blocked(env):
    conn, user = env
    conn.execute("UPDATE autopilot_tasks SET instruction='跑一次构建' WHERE id=1")
    conn.commit()

    ap.update_task(1, ap.UpdateTaskBody(instruction="   "), user)

    state, reason, at = conn.execute(
        "SELECT dispatch_state, dispatch_state_reason, dispatch_state_at "
        "FROM autopilot_tasks WHERE id=1").fetchone()
    assert state == BLOCKED
    assert reason == AUTOPILOT_EMPTY_INSTRUCTION_REASON
    assert at, "标记时间必须落库（跨进程判重权威）"


def test_update_without_instruction_leaves_state_untouched(env):
    """只改 enabled/cron 等字段（批量启停路径）不得动状态列。"""
    conn, user = env
    conn.execute("UPDATE autopilot_tasks SET instruction='跑一次构建', dispatch_state='', "
                 "dispatch_state_reason='', dispatch_state_at='' WHERE id=1")
    conn.commit()

    ap.update_task(1, ap.UpdateTaskBody(enabled=False), user)

    assert _row(conn)[18:21] == ("", "", "")


# ---------- 手动触发：409 + 明确原因 ----------

def test_trigger_empty_instruction_returns_explicit_409(env, monkeypatch):
    import src.domains.runtime.public as rt_public

    calls = []

    async def _fake_trigger(task_id, params=None):
        calls.append(task_id)
        return {"ok": False, "message": AUTOPILOT_EMPTY_INSTRUCTION_REASON}

    monkeypatch.setattr(rt_public, "trigger_task_now", _fake_trigger, raising=True)

    _conn, user = env
    import asyncio
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ap.trigger_task(1, user))

    assert ei.value.status_code == 409
    assert ei.value.detail["message"] == AUTOPILOT_EMPTY_INSTRUCTION_REASON

def test_create_task_persists_state_columns_as_empty(env):
    """建单必须真的把三列落库为空串（MySQL strict 下不给值即 1364 → 500）。

    2026-11-25 部署验证实测：POST /api/autopilot/tasks 返回
    `OperationalError: (1364, "Field 'dispatch_state' doesn't have a default value")`。
    这里用真实执行取代文本断言（架构守护 C3：删掉一列时文本断言仍全绿）。
    """
    conn, user = env
    body = ap.CreateTaskBody(name="test_create_probe", repo_key="", instruction="probe")
    res = ap.create_task(body, user)

    row = conn.execute(
        "SELECT dispatch_state, dispatch_state_reason, dispatch_state_at "
        "FROM autopilot_tasks WHERE id=?", (res["id"],)).fetchone()
    assert tuple(row) == ("", "", ""), f"三列未落空串: {tuple(row)}"


def test_create_task_insert_column_list_is_exact():
    """列清单按逗号切分后与期望集合精确相等（子串断言会把 dispatch_state 误判为命中）。"""
    import inspect
    import re

    src = inspect.getsource(ap.create_task)
    sql = " ".join(re.findall(r'"([^"]*)"', src))
    m = re.search(r"INSERT INTO autopilot_tasks\s*\(([^)]*)\)", sql)
    assert m, "未找到 create_task 的 INSERT 列清单"
    cols = {c.strip() for c in m.group(1).split(",") if c.strip()}
    expected = {
        "name", "repo_key", "agent_type", "instruction", "skill_name", "cron",
        "max_retry", "enabled", "next_run_at", "created_at", "updated_at", "created_by",
        "engine_runtime_ids", "custom_fields", "work_branch", "engine_runtime_uids",
        "dispatch_state", "dispatch_state_reason", "dispatch_state_at",
    }
    assert cols == expected, f"列清单不符: 缺={sorted(expected-cols)} 多={sorted(cols-expected)}"


def test_state_column_ddl_uses_varchar_with_default():
    """列 DDL 不能再用 TEXT DEFAULT ''（8.0 落地成 DEFAULT NULL，strict 下 1364）。"""
    from src.domains.mgmt.migrations import autopilot_dispatch_state as mig

    for col, ddl in mig.STATE_COLUMNS:
        assert ddl.upper().startswith("VARCHAR"), f"{col} 应为 VARCHAR: {ddl}"
        assert "NOT NULL" in ddl.upper() and "DEFAULT ''" in ddl, f"{col} 缺默认值: {ddl}"


def test_schema_ddl_state_columns_match_varchar_ddl():
    """建表 DDL 与迁移 DDL 必须同型（PR #2157 门复审 ④）。

    `_AUTOPILOT_TASKS_DDL` 走 `mysql_compat` 翻译建表：三列若仍写 TEXT，MySQL 不接受
    TEXT 的字面量 DEFAULT，翻译层会把 `DEFAULT ''` 包成表达式默认值 → 新装库的
    `COLUMN_DEFAULT` 读不到 `''`，strict 下 1364 只等迁移 202611250007 补刀。
    这里锁死两边一致，避免「迁移改了、建表没改」再让新装库回到假绿。
    """
    from src.domains.mgmt.migrations import autopilot_dispatch_state as mig
    from src.domains.mgmt.schema_ddl import _AUTOPILOT_TASKS_DDL as ddl

    body = " ".join(ddl.split())
    for col, column_ddl in mig.STATE_COLUMNS:
        assert f"{col} {column_ddl}" in body, (
            f"建表 DDL 的 {col} 与迁移 DDL 不一致（应为 {column_ddl}）: {body[:400]}")


def test_schema_ddl_translated_for_mysql_keeps_literal_default():
    """MySQL 翻译后三列仍是 `VARCHAR(255) NOT NULL DEFAULT ''`（不是表达式默认值）。"""
    import re

    from src.domains.mgmt.schema_ddl import _AUTOPILOT_TASKS_DDL as ddl
    from src.kernel import mysql_compat

    translated = mysql_compat._translate_create_table(ddl)
    text = " ".join(translated.split())
    for col in ("dispatch_state", "dispatch_state_reason", "dispatch_state_at"):
        m = re.search(rf"\b{col} VARCHAR\(255\) NOT NULL DEFAULT ''", text)
        assert m, f"MySQL 翻译后 {col} 默认值不保真: {text[:400]}"
