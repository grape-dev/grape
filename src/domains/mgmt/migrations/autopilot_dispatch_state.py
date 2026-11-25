"""autopilot_tasks「不可派发」状态三列 + 存量一次性幂等自检（Issue #3733）。

实现放在本域（mgmt 是 `autopilot_tasks` 的 owner 域），`src/api/db_setup.py` 只留
`register_migration` 接线，理由两条：

1. 补列必须由 owner 域执行——跨域迁移会被 `_ensure_column` 的域归属守卫跳过，
   而守卫跳过时迁移默认仍会盖章（见 2026-11-18 `category_dict` 事故），列就永不落地；
   本迁移对每条补列传 `raise_on_guard=True`，万一在非属主进程执行则显式抛错、不盖章；
2. 含表名的写语句若留在 api 层，会被 `tests/arch/test_foreign_table_refs.py` 判成
   新增跨域引用（`autopilot_tasks←api`），破棘轮。

契约：见 `docs/issue-3733/plan.md` 与
`tests/kernel/test_issue3733_autopilot_dispatch_state_migration.py`。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# 迁移注册（version / name / 域）只在 `src/api/db_setup.py` 的 register_migration 处维护：
# 仓库静态守卫 `tests/api/test_migration_track_guard.py` 只认模块级字符串字面量，
# 本模块不再导出同名常量（复审 Nit：避免同一条版本号双份维护、漂移后静默撞号）。

TABLE = "autopilot_tasks"

#: （列名, DDL）：TEXT 带非 NULL DEFAULT 在 MySQL 上走不了 INSTANT/INPLACE，
#: 补列时必须传 allow_copy=True（autopilot_tasks 是小表，COPY 安全）。
#: VARCHAR 而非 TEXT：MySQL 8.0.13 之前 TEXT 不支持 DEFAULT，且本仓实测该 DDL 落地后
#: 列变成 `text NOT NULL DEFAULT NULL`（strict 下任何不给值的 INSERT 直接 1364）——
#: 建单接口就是被这个打成 500 的。三列都是短码/时间戳，VARCHAR(255) 足够。
STATE_COLUMNS = (
    ("dispatch_state", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("dispatch_state_reason", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("dispatch_state_at", "VARCHAR(255) NOT NULL DEFAULT ''"),
)


def backfill_dispatch_state(cursor: Any, now: str = "") -> int:
    """补状态三列 + 把 enabled 且空指令的存量行标记为不可派发（幂等）。

    返回本次**新标记**的行数（0 = 重复执行，零改动）。

    - 只标记 `enabled=1 AND TRIM(instruction)=''`：指令非空 / 已停用的行不动；
    - **不改 `enabled`**：直接停用会掩盖「已启用却在空转」的事实，补齐指令即恢复派发；
    - `dispatch_state<>:blocked` 谓词使重复执行零写（注册式迁移盖章后不再执行，
      未盖章时每次启动重试），且不刷新 `dispatch_state_at`——该时间是状态进入时刻，
      也是跨进程判重的权威。
    """
    from src.api import db_setup as _ds

    from src.domains.mgmt.autopilot_state import (
        AUTOPILOT_EMPTY_INSTRUCTION_REASON,
        AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION,
    )

    for col, ddl in STATE_COLUMNS:
        # strict + raise_on_guard（复审 Required）：`_ensure_column` 在 INSTANT/INPLACE/COPY
        # 全失败、或被域归属守卫跳过时只记人工清单并**正常返回**（db_setup.py:1239-1250），
        # 而迁移执行器约定「fn 正常返回=盖章」→ 列没建成却把版本号盖死，下次启动不再重试。
        # 后果不止界面空白：`list_enabled_autopilot_tasks_any` 已显式 SELECT 这三列，
        # 缺列即 1054 → 调度器每 tick 抛错被兜底吞成 `Autopilot scheduler loop error`，
        # **全部 autopilot 任务一起停派**。故这里与 db_setup.py:10923 / 11051 同口径：
        # 补列失败必须抛错（不盖章，下次在属主域重试）。
        _ds._ensure_column(cursor, TABLE, col, ddl, allow_copy=True,
                           strict=True, raise_on_guard=True)
    _now = now or datetime.now(timezone.utc).isoformat()
    cur = cursor.execute(
        "UPDATE autopilot_tasks SET dispatch_state=?, dispatch_state_reason=?, "
        "dispatch_state_at=? WHERE enabled=1 AND TRIM(instruction)='' AND dispatch_state<>?",
        (AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION, AUTOPILOT_EMPTY_INSTRUCTION_REASON,
         _now, AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def run(conn: Any = None) -> int:
    """迁移入口：在 owner 连接上补列 + 自检（`conn` 缺省用进程主库）。

    失败一律 raise（不做「log 后正常返回」的静默降级）：执行器约定「抛错 = 不盖章，
    下次启动重试」；吞掉就会「列没建成、版本号却盖死」，线上查询立刻 1054。
    """
    from src.api import db_setup as _ds

    target = _ds.db if conn is None else conn
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            marked = backfill_dispatch_state(cursor)
            target.commit()
        finally:
            cursor.close()
    if marked:
        log.info(f"Autopilot: {marked} enabled task(s) marked as not dispatchable "
                 f"(empty instruction, Issue #3733)")
    return marked


def ensure_state_column_defaults(conn: Any = None) -> int:
    """把三列从「TEXT NOT NULL 无默认」修正为 `VARCHAR(255) NOT NULL DEFAULT ''`。

    背景（2026-11-25 部署验证实测）：#3733 首版 DDL 用 `TEXT NOT NULL DEFAULT ''`，
    MySQL 8.0 上落地结果是 `text NOT NULL DEFAULT NULL` —— strict 模式下任何不显式
    给值的 INSERT 直接 1364 `Field 'dispatch_state' doesn't have a default value`，
    建单接口 500。列已存在时 `_ensure_column` 不会改类型/默认值，所以需要本迁移补一刀。

    幂等：MODIFY 重复执行结果相同；SQLite 走同语义的 TEXT 列（DEFAULT '' 已生效）直接跳过。
    返回修正的列数。
    """
    from src.api import db_setup as _ds

    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        is_mysql = False
    if not is_mysql:
        return 0
    target = _ds.db if conn is None else conn
    fixed = 0
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            for col, ddl in STATE_COLUMNS:
                cursor.execute(f"ALTER TABLE {TABLE} MODIFY COLUMN {col} {ddl}")
                fixed += 1
            target.commit()
        finally:
            cursor.close()
    log.info(f"Autopilot: normalized {fixed} dispatch_state column default(s) (Issue #3733)")
    return fixed
