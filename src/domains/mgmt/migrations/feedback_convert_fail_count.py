"""user_feedback.convert_fail_count 列（迁移 202611240004）：反馈→Issue 连续失败轮次。

Issue #3731 的转换失败计数只落在日志里（7 天 20.7 万条 WARNING），故障不可感知；
修复把连续失败轮次落库（跨 worker 共享库可见、admin 可查、达阈值升级 ERROR、
建单成功后清零）。读侧 `mgmt.public` 的 ``feedback_pending_rows`` 已经把这列写进
SELECT，所以**列不存在时读侧首轮即 1054 Unknown column**。

为什么单独一条注册式迁移（部署验证实测缺口）
--------------------------------------------
原实现把这行 DDL 写进了 `src/domains/mgmt/migrations_ddl.py::ALTERS_USER_FEEDBACK_COLUMNS`，
而该常量只被 `src/api/db_setup.py::_migrate_tables()`（**冻结老轨**）消费：

- 存量库的 ``grape_t_schema_migrations`` 早已被 legacy-stamp 盖帽（202610300001），
  启动时老轨整段不执行 → 库里永远没有这一列 → 转换读侧每轮 1054，转换链路继续全停；
- 全新库靠老轨补列，存量库靠注册式迁移补列，两条路径口径不一致，守卫也抓不到
  （`tests/api/test_migration_track_guard.py` 只比对 `_run_all_migrations()` 的调用
  清单，不校验 ``ALTERS_*`` 常量内容）。

因此本列的口径统一到注册式迁移，``ALTERS_USER_FEEDBACK_COLUMNS`` 不再保留该行。

归属：``user_feedback`` 归 mgmt（configs/table_ownership.json），补列 SQL 住本域
migrations 下（跨域守卫 P0-2）；注册由 ``src/api/db_setup.py`` 引导器做
（``register_migration``）。

失败语义：补列失败要抛出（执行器约定「抛错=不盖章，下次启动重试」），
``post_check`` 断言列在位才盖章（Issue #3648）——否则列没建成、版本号却被盖死，
线上表现为读侧 1054 且异常被吞成「无待转换反馈」。

幂等：``_ensure_column`` 先探列（PRAGMA table_info / information_schema），
可重复执行。``domains=("mgmt",)``：三库拓扑下由 mgmt 域进程执行。
"""
from __future__ import annotations

TABLE = "user_feedback"
COLUMN = "convert_fail_count"
#: 与 `db_setup``_NEW_TABLES` 之外的存量补列口径一致：计数列非空默认 0，
#: MySQL 侧 INTEGER 带 DEFAULT 可走 ALGORITHM=INSTANT（不锁表）。
COLUMN_DDL = "INTEGER NOT NULL DEFAULT 0"


def _columns_of(c, table: str) -> list:
    """当前表的列名（探列单一入口；MySQL 由 kernel mysql_compat 翻译）。"""
    from src.kernel.ddl_safety import table_columns

    return table_columns(c, table)


def add_convert_fail_count_column(conn=None) -> None:
    """幂等补 ``user_feedback.convert_fail_count``（存量库首跑/重跑都安全）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        # 三库边界：表不在本库 / 在本库是只读视图 → 跳过不盖章，由 mgmt 库收敛。
        if not ds._table_present(c, TABLE) or not ds._is_base_table(c, TABLE):
            raise ds._NonOwnerObject(
                f"{TABLE} 不在本库或是视图（非 owner 域），"
                f"{COLUMN} 列由 mgmt 库补齐")
        ds._ensure_column(c, TABLE, COLUMN, COLUMN_DDL, strict=True)
        conn.commit()


def verify_convert_fail_count_column(conn=None) -> None:
    """后置校验：列在位才盖章；缺列即抛错（下次启动重试）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    if COLUMN not in _columns_of(conn.cursor(), TABLE):
        raise RuntimeError(
            f"202611240004 post_check 未收敛：{TABLE} 缺列 {COLUMN}"
            f"（不盖章，下次启动重试）")
    log.info(f"202611240004 post_check 通过：{TABLE}.{COLUMN} 在位")


MIGRATION = {
    "version": "202611240004",
    "name": "issue3731-user-feedback-convert-fail-count",
    "kind": "safe",
    "fn": add_convert_fail_count_column,
    "domains": ("mgmt",),
    "post_check": verify_convert_fail_count_column,
}