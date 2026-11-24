"""Issue #3733：autopilot_tasks「不可派发」状态列 + 存量一次性幂等自检（迁移 202611240004）。

锁四条契约（Spec 见 Issue #3733 评论 202816767，方案见 docs/issue-3733/plan.md）：
1. 三个状态列补齐（dispatch_state / dispatch_state_reason / dispatch_state_at）——
   存量库走注册式补列，旧 DDL 没有这三列；
2. 只标记 `enabled=1 AND TRIM(instruction)=''` 的行；指令非空 / 已停用的行零改动；
3. **不改 `enabled`**：直接停用会掩盖「已启用却在空转」的事实，补齐指令即恢复 cron 派发；
4. 幂等：重复执行（每次启动都会跑）零改动，`dispatch_state_at` 不被刷新。
"""
import sqlite3

import pytest

from src.domains.mgmt.migrations.autopilot_dispatch_state import (
    backfill_dispatch_state as _backfill_autopilot_dispatch_state,
)
from src.domains.mgmt.public import AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION

# 存量库形态：202611170046 之后的 autopilot_tasks（无状态三列）
_LEGACY_DDL = """
CREATE TABLE autopilot_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    repo_key        TEXT NOT NULL DEFAULT '',
    agent_type      TEXT NOT NULL DEFAULT 'issue',
    instruction     TEXT NOT NULL DEFAULT '',
    skill_name      TEXT NOT NULL DEFAULT '',
    cron            TEXT NOT NULL DEFAULT '*/30 * * * *',
    max_retry       INTEGER NOT NULL DEFAULT 3,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run_at     TEXT DEFAULT '',
    next_run_at     TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    created_by      INTEGER DEFAULT 0
)
"""

_ROWS = [
    # (name, instruction, enabled) —— 第 1/2 行 = 线上 id=6 同族；第 3/4 行 = 不受影响
    ("issue-patrol", "", 1),
    ("blank", "   ", 1),
    ("real", "跑一次构建", 1),
    ("off-empty", "", 0),
]


def _legacy_db(tmp_path):
    db = sqlite3.connect(str(tmp_path / "autopilot_3733.db"))
    c = db.cursor()
    c.execute(_LEGACY_DDL)
    c.executemany(
        "INSERT INTO autopilot_tasks (name, instruction, enabled) VALUES (?, ?, ?)", _ROWS)
    db.commit()
    return db


def _row(c, name):
    """(dispatch_state, dispatch_state_reason, dispatch_state_at, enabled)。"""
    return c.execute(
        "SELECT dispatch_state, dispatch_state_reason, dispatch_state_at, enabled "
        "FROM autopilot_tasks WHERE name=?", (name,)).fetchone()


def test_backfill_adds_state_columns_and_marks_empty_instruction_rows(tmp_path):
    db = _legacy_db(tmp_path)
    c = db.cursor()

    marked = _backfill_autopilot_dispatch_state(c)
    db.commit()

    assert marked == 2, "只应标记 enabled=1 且指令为空的两行"
    state, reason, at, enabled = _row(c, "issue-patrol")
    assert state == AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION
    assert reason and "指令" in reason, "原因需可直接展示给运营，不得为空"
    assert at, "状态进入时间必须落库（跨进程判重权威）"
    assert enabled == 1, "不得改动 enabled——停用会掩盖空转事实"
    # 纯空白等价于空指令
    assert _row(c, "blank")[0] == AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION
    # 指令非空 / 已停用：零改动
    assert _row(c, "real") == ("", "", "", 1)
    assert _row(c, "off-empty") == ("", "", "", 0)


def test_backfill_is_idempotent(tmp_path):
    db = _legacy_db(tmp_path)
    c = db.cursor()

    assert _backfill_autopilot_dispatch_state(c) == 2
    at_first = _row(c, "issue-patrol")[2]

    # 注册式迁移未盖章时每次启动重试；已盖章后不再执行，重试时应幂等（零改动 + 不刷时间戳）
    assert _backfill_autopilot_dispatch_state(c) == 0
    assert _row(c, "issue-patrol")[2] == at_first
    assert _row(c, "issue-patrol")[0] == AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION


def test_backfill_does_not_resurrect_cleared_state(tmp_path):
    """用户补齐指令 → 路由清状态；再跑一次迁移不得把已清掉的行重新标记。"""
    db = _legacy_db(tmp_path)
    c = db.cursor()
    _backfill_autopilot_dispatch_state(c)
    c.execute("UPDATE autopilot_tasks SET instruction='补齐后的巡检步骤', dispatch_state='', "
              "dispatch_state_reason='', dispatch_state_at='' WHERE name='issue-patrol'")
    db.commit()

    assert _backfill_autopilot_dispatch_state(c) == 0
    assert _row(c, "issue-patrol") == ("", "", "", 1)


def test_backfill_requires_strict_column_add(monkeypatch):
    """补列必须 `strict=True` + `raise_on_guard=True`（复审 Required）。

    `_ensure_column` 在 INSTANT/INPLACE/COPY 全失败、或被域归属守卫跳过时默认
    「只记人工处理清单后正常返回」，而执行器约定「fn 正常返回 = 盖章」→ 列没建成
    却把版本号盖死，下次启动不再重试，线上表现为查询该列 1054（调度器每 tick 抛错，
    autopilot 全量停派）。本用例锁住调用参数，防回归到静默盖章。
    """
    from src.api import db_setup as _ds
    from src.domains.mgmt.migrations import autopilot_dispatch_state as mig

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE autopilot_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, instruction TEXT DEFAULT '', "
        "enabled INTEGER DEFAULT 1, dispatch_state TEXT NOT NULL DEFAULT '', "
        "dispatch_state_reason TEXT NOT NULL DEFAULT '', dispatch_state_at TEXT NOT NULL DEFAULT '')")

    seen = []

    def _fake_ensure(c, tbl, col, ddl, **kw):
        seen.append((tbl, col, kw))

    monkeypatch.setattr(_ds, "_ensure_column", _fake_ensure, raising=False)
    mig.backfill_dispatch_state(db.cursor())

    assert [col for _tbl, col, _kw in seen] == [col for col, _ddl in mig.STATE_COLUMNS]
    assert all(_tbl == mig.TABLE for _tbl, _col, _kw in seen)
    assert all(kw.get("strict") is True and kw.get("raise_on_guard") is True
               for _tbl, _col, kw in seen), "补列失败必须抛错（不盖章），否则列缺失且永不重试"
    assert all(kw.get("allow_copy") is True for _tbl, _col, kw in seen), \
        "带 DEFAULT 的 TEXT 列在 MySQL 上需允许 COPY 降级"


def test_migration_registered_for_mgmt_domain():
    """autopilot_tasks 归 mgmt 域（configs/table_ownership.json）：迁移必须登记 mgmt 域，
    否则拆库拓扑下只有 runtime 进程跑它，列永远不在 mgmt 库落。"""
    from src.api import db_setup

    mers = [m for m in db_setup._MIGRATIONS if m.version == "202611240004"]
    assert len(mers) == 1, [m.version for m in db_setup._MIGRATIONS if "2026112200" in m.version]
    assert mers[0].name == "autopilot-task-dispatch-state"
    assert tuple(mers[0].domains) == ("mgmt",)
