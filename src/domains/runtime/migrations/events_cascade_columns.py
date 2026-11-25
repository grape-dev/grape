"""events 表级联补偿五列（登记式迁移 202611250001）。

Issue #3877：分库（三库）拓扑下 **存量** runtime 库的 ``events`` 表永久缺
``cascade_reason`` / ``report_missing`` / ``triggered_by`` / ``cascade_task_status`` /
``cascade_prev_event_status``，``_cascade_event_terminal()`` 的终态收敛 UPDATE 每次抛
``1054 Unknown column 'cascade_reason' in 'field list'``：

- 事件终态无法收敛（任务已终态、事件永停 running/pending）；
- ``event_active_guard`` 释放被跳过（原实现在 UPDATE 之后同一 try 内），只能等 TTL 30min；
- 失败被外层 ``except`` 吞成 WARNING（72 小时 54 次，无指标无告警）。

为什么必须是一条**登记式**迁移
------------------------------
五列此前只登记在两处，两处对存量库都不生效：

1. ``src/domains/runtime/schema_ddl.py::_EVENTS_TABLE_DDL`` —— 只服务**新装库**的
   ``CREATE TABLE``；
2. ``src/domains/runtime/migrations_ddl.py::ALTERS_SESSION_EVENT_COLUMNS`` —— 只被
   ``db_setup._migrate_tables()`` 消费，而该方法属于**已冻结的 legacy 轨道**
   ``_run_all_migrations()``：仅「全新空库首跑」或执行器异常兜底时才跑，
   已盖 legacy-stamp（202610300001）的存量库启动时整段跳过
   （模块内注释自己写着「加了不报错、也不执行，直到线上查询报 1054 才暴露」）。

``tests/api/test_migration_track_guard.py`` 只比对 ``_run_all_migrations()`` 的调用名清单、
不校验 ``ALTERS_*`` 常量内容，所以门禁抓不到「新增列只登记在冻结老轨」这一类缺口
（同类事故样本：#3773 的 ``acp_state_db_*`` 四列、#3731 的 ``convert_fail_count``）。

归属：``events`` 归 runtime（``configs/table_ownership.json``），补列 SQL 住本域
``migrations`` 下（跨域守卫 P0-2）；注册由 ``src/api/db_setup.py`` 引导器做。
新装库由 ``schema_ddl`` 直接带列，本迁移只服务存量库。

失败语义：补列失败要抛出（不盖章、下次启动重试）；``post_check`` 断言五列齐全才盖章。
非 owner 域（三库下 mgmt/insight 只看到跨库只读视图）抛 ``_NonOwnerObject``，
执行器「跳过且不盖章」，等 runtime 库补列后重建视图自然收敛。

幂等：``_ensure_column`` 先探列，可重复执行；MySQL 侧 TEXT 默认值由
``ddl_safety`` 剥离后走 INSTANT 补列（不触发 COPY 重建）。
"""
from __future__ import annotations

TABLE = "events"

#: (列名, 列定义) —— 与 ``schema_ddl._EVENTS_TABLE_DDL`` 逐字一致。
#: 五列都是 ``_cascade_event_terminal_impl`` UPDATE / ``cascade.fetch_display_meta``
#: SELECT 的字段，缺任意一列即 1054。
COLUMNS = (
    ("cascade_reason", "TEXT DEFAULT ''"),
    ("report_missing", "INTEGER DEFAULT 0"),
    ("triggered_by", "TEXT DEFAULT ''"),
    ("cascade_task_status", "TEXT DEFAULT ''"),
    ("cascade_prev_event_status", "TEXT DEFAULT ''"),
)


def _columns_of(c) -> set:
    """当前表的列名（探列单一入口；MySQL 由 kernel mysql_compat 翻译）。"""
    from src.kernel.ddl_safety import table_columns

    return set(table_columns(c, TABLE))


def _assert_owner_base_table(c) -> None:
    """非 owner 库（表缺失 / 是跨库只读视图）→ 跳过不盖章，由 runtime 库收敛。"""
    from src.api import db_setup as ds

    if not ds._table_present(c, TABLE) or not ds._is_base_table(c, TABLE):
        raise ds._NonOwnerObject(
            f"{TABLE} 不在本库或是视图（非 owner 域），"
            f"cascade 五列由 runtime 库补齐")


def add_events_cascade_columns(conn=None) -> None:
    """幂等补 5 列（存量库首跑/重跑都安全）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        _assert_owner_base_table(c)
        for col, ddl in COLUMNS:
            # raise_on_guard=True：本迁移只在 events 属主域（runtime）有效，
            # 被域归属守卫跳过（本进程非属主）就是「这条迁移没干活」——必须抛错不盖章，
            # 而不是静默返回 True 让版本号盖死（202611171846 category_dict 事故同族）。
            ds._ensure_column(c, TABLE, col, ddl, strict=True, raise_on_guard=True)
        conn.commit()


def verify_events_cascade_columns(conn=None) -> None:
    """后置校验：五列齐全才盖章；缺任意一列即抛错（下次启动重试）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    _assert_owner_base_table(c)
    present = _columns_of(c)
    missing = [col for col, _ddl in COLUMNS if col not in present]
    if missing:
        raise RuntimeError(
            f"202611250001 post_check 未收敛：{TABLE} 缺列 {missing}"
            f"（不盖章，下次启动重试）")
    log.info(f"202611250001 post_check 通过：{TABLE} cascade 补偿五列齐全（{len(COLUMNS)} 列）")


MIGRATION = {
    "version": "202611250001",
    "name": "events-cascade-compensation-columns",
    "kind": "safe",
    "fn": add_events_cascade_columns,
    "domains": ("runtime",),
    "post_check": verify_events_cascade_columns,
}