"""Autopilot 环境性失败状态列（迁移 202611230003）：autopilot_tasks 退避/熔断 5 列。

Issue #3800：任务绑定的引擎永久离线时，调度器每 cron tick 建一条秒失败的 exec，
1 小时约 60 条 failed 记录 + 60+ 条 ERROR/WARNING，且没有任何告警。修复把环境性
失败改成「指数退避 → 连续同因熔断 → 一次性结构化告警 → 引擎恢复自愈」，状态必须
落库（进程重启后退避仍生效），故 `autopilot_tasks` 增 5 列。

归属：`autopilot_tasks` 归 mgmt（configs/table_ownership.json），补列 SQL 住本域
migrations 下（跨域守卫 P0-2）；注册由 `src/api/db_setup.py` 引导器做
（register_migration）。新装库由 `src/domains/mgmt/schema_ddl.py::_AUTOPILOT_TASKS_DDL`
直接带列，本迁移只服务存量库。

失败语义：补列失败要抛出（executor 约定「抛错=不盖章，下次启动重试」），
post_check 断言 5 列齐全才盖章（Issue #3648）——否则列没建成、版本号却被盖死，
线上表现为查询该列 1054 且调度器异常被吞成「无任务」。

幂等：`_ensure_column` 先 `PRAGMA table_info` 探测，可重复执行。
domains=("mgmt",)：单库 / 测试不筛域、全量执行。
"""
from __future__ import annotations

#: 要补的列（列名, 列定义）——与 mgmt/schema_ddl.py::_AUTOPILOT_TASKS_DDL 逐字一致。
#: 时间列用可空 `TEXT DEFAULT ''`（等价空串哨兵，避免 MySQL 加 TEXT NOT NULL DEFAULT
#: 需要 COPY 锁全表）；计数/开关用 `INTEGER NOT NULL DEFAULT 0`。
COLUMNS = (
    ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error_code", "TEXT DEFAULT ''"),
    ("offline_since", "TEXT DEFAULT ''"),
    ("backoff_until", "TEXT DEFAULT ''"),
    ("circuit_paused", "INTEGER NOT NULL DEFAULT 0"),
)

TABLE = "autopilot_tasks"


def _columns_of(c, table: str) -> list:
    """当前表的列名（PRAGMA 口径；MySQL 由 kernel mysql_compat 翻译，与 db_setup 同）。"""
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def add_failure_state_columns(conn=None) -> None:
    """幂等补 5 列（存量库首跑/重跑都安全）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        for col, ddl in COLUMNS:
            ds._ensure_column(c, TABLE, col, ddl)
        conn.commit()


def verify_failure_state_columns(conn=None) -> None:
    """后置校验：5 列齐全才盖章；缺任意一列即抛错（下次启动重试）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    present = set(_columns_of(conn.cursor(), TABLE))
    missing = [col for col, _ in COLUMNS if col not in present]
    if missing:
        raise RuntimeError(
            f"202611230003 post_check 未收敛：{TABLE} 缺列 {missing}（不盖章，下次启动重试）")
    log.info(f"202611230003 post_check 通过：{TABLE} 失败状态列齐全（{len(COLUMNS)} 列）")


MIGRATION = {
    "version": "202611230003",
    "name": "autopilot-task-failure-state-columns",
    "kind": "safe",
    "fn": add_failure_state_columns,
    "domains": ("mgmt",),
    "post_check": verify_failure_state_columns,
}