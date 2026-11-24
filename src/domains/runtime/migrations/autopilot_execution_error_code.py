"""autopilot_executions.error_code 列（迁移 202611230004）：失败分类落库。

Issue #3800：环境性失败（`ENGINE_OFFLINE` / `BINDING_STALE`）必须能被调度收口段识别，
不能靠解析 `error` 文案（文案随排查口径改动，改一次断一次链路）。故执行记录新增
`error_code`：派发侧写入分类，`_execute_task` 读到环境性错误码就改走退避/熔断通道，
而不是按普通失败重试并推进 cron。

归属：`autopilot_executions` 归 runtime（configs/table_ownership.json），补列 SQL 住
本域 migrations 下（跨域守卫 P0-2）；注册由 `src/api/db_setup.py` 引导器做
（register_migration）。新装库由 `src/domains/runtime/schema_ddl.py::_AUTOPILOT_EXECUTIONS_DDL`
直接带列，本迁移只服务存量库。

失败语义：补列失败要抛出（不盖章、下次启动重试）；post_check 断言列在位才盖章。
幂等：`_ensure_column` 先 PRAGMA 探测，可重复执行。domains=("runtime",)。
"""
from __future__ import annotations

TABLE = "autopilot_executions"
COLUMN = "error_code"
COLUMN_DDL = "TEXT DEFAULT ''"


def add_error_code_column(conn=None) -> None:
    """幂等补 `autopilot_executions.error_code`。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        ds._ensure_column(c, TABLE, COLUMN, COLUMN_DDL)
        conn.commit()


def verify_error_code_column(conn=None) -> None:
    """后置校验：列在位才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    cols = [r[1] for r in conn.cursor().execute(f"PRAGMA table_info({TABLE})").fetchall()]
    if COLUMN not in cols:
        raise RuntimeError(
            f"202611230004 post_check 未收敛：{TABLE} 缺列 {COLUMN}（不盖章，下次启动重试）")
    log.info(f"202611230004 post_check 通过：{TABLE}.{COLUMN} 在位")


MIGRATION = {
    "version": "202611230004",
    "name": "autopilot-execution-error-code",
    "kind": "safe",
    "fn": add_error_code_column,
    "domains": ("runtime",),
    "post_check": verify_error_code_column,
}