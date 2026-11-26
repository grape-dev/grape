"""runtime_credentials 的 /tmp janitor 清理计数列（迁移 202611250014）。

Issue #4445：worker 节点 ``/tmp`` 为 tmpfs（占用即钉住物理内存），长期被任务派生残留
占满；新增 janitor 按 TTL 回收后需要可回归的数值证据。worker 在
``/api/runtime/heartbeat`` 上报两项累计量，落 ``runtime_credentials``：

- ``tmp_janitor_removed_items_total``：累计清理条目数（INT）
- ``tmp_janitor_removed_bytes_total``：累计释放字节数（DOUBLE）

与 Issue #3773 同型风险：心跳 UPDATE 把两列写在 SET 列表里，**列不存在即 1054
Unknown column**，整条心跳 UPDATE 失败被吞 → 心跳与全部指标断掉。因此走注册式迁移
（kind=safe + post_check），存量库（legacy-stamp 已盖章）启动时自动补列。
"""
from __future__ import annotations

TABLE = "runtime_credentials"
#: (列名, 列定义) —— 与写侧 UPDATE/读侧 SELECT 的字段一一对应，逐字同 `routes/runtime.py`。
COLUMNS = (
    ("tmp_janitor_removed_items_total", "INT DEFAULT 0"),
    ("tmp_janitor_removed_bytes_total", "DOUBLE DEFAULT 0"),
)


def _columns_of(c, table: str) -> list:
    """当前表的列名（探列单一入口；MySQL 由 kernel mysql_compat 翻译）。"""
    from src.kernel.ddl_safety import table_columns

    return table_columns(c, table)


def add_tmp_janitor_metrics_columns(conn=None) -> None:
    """幂等补 2 列（存量库首跑/重跑都安全）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        # 三库边界：表不在本库 / 在本库是只读视图 → 跳过不盖章，由 runtime 库收敛。
        if not ds._table_present(c, TABLE) or not ds._is_base_table(c, TABLE):
            raise ds._NonOwnerObject(
                f"{TABLE} 不在本库或是视图（非 owner 域），"
                f"tmp_janitor_* 指标列由 runtime 库补齐")
        for col, ddl in COLUMNS:
            ds._ensure_column(c, TABLE, col, ddl, strict=True)
        conn.commit()


def verify_tmp_janitor_metrics_columns(conn=None) -> None:
    """后置校验：2 列齐全才盖章；缺任意一列即抛错（下次启动重试）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    present = set(_columns_of(conn.cursor(), TABLE))
    missing = [col for col, _ in COLUMNS if col not in present]
    if missing:
        raise RuntimeError(
            f"202611250014 post_check 未收敛：{TABLE} 缺列 {missing}"
            f"（不盖章，下次启动重试）")
    log.info(f"202611250014 post_check 通过：{TABLE} tmp janitor 指标列齐全"
             f"（{len(COLUMNS)} 列）")


MIGRATION = {
    "version": "202611250014",
    "name": "issue4445-tmp-janitor-metrics-columns",
    "kind": "safe",
    "fn": add_tmp_janitor_metrics_columns,
    "domains": ("runtime",),
    "post_check": verify_tmp_janitor_metrics_columns,
}