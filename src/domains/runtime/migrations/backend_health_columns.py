"""agent_runtimes 后端能力 + 熔断列（迁移 202611240004）。

Issue #3928：缺 CLI 的 codearts runtime 仍参与认领 → 认领后 3s 内 exit=-1 丢任务。
本迁移给存量库补：
- backend_status / backend_reason / backend_checked_at：注册/心跳能力观测；
- circuit_open_until / circuit_fail_count / circuit_half_open：runtime 级熔断与半开试探。

归属：agent_runtimes 归 runtime（configs/table_ownership.json），补列 SQL 住本域
migrations 下（跨域守卫 P0-2）；注册由 src/api/db_setup.py 引导器做。
新装库由 schema_ddl._AGENT_RUNTIMES_DDL 直接带列，本迁移只服务存量库。
"""
from __future__ import annotations

TABLE = "agent_runtimes"
COLUMNS = (
    ("backend_status", "VARCHAR(32) DEFAULT ''"),
    ("backend_reason", "TEXT DEFAULT ''"),
    ("backend_checked_at", "VARCHAR(64) DEFAULT ''"),
    ("circuit_open_until", "VARCHAR(64) DEFAULT ''"),
    ("circuit_fail_count", "INTEGER DEFAULT 0"),
    ("circuit_half_open", "INTEGER DEFAULT 0"),
)


def add_backend_health_columns(conn=None) -> None:
    """幂等补列（TEXT 带 DEFAULT 走 allow_copy=True）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        for col, ddl in COLUMNS:
            ds._ensure_column(c, TABLE, col, ddl,
                              allow_copy=ddl.startswith("TEXT"), strict=True)
        conn.commit()


def verify_backend_health_columns(conn=None) -> None:
    """后置校验：六列在位才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    cols = {r[1] for r in conn.cursor().execute(f"PRAGMA table_info({TABLE})").fetchall()}
    missing = [col for col, _ddl in COLUMNS if col not in cols]
    if missing:
        raise RuntimeError(
            f"202611240004 post_check 未收敛：{TABLE} 缺列 {missing}（不盖章，下次启动重试）")
    log.info(f"202611240004 post_check 通过：{TABLE} 后端能力/熔断列在位")


MIGRATION = {
    "version": "202611240004",
    "name": "backend-capability-circuit-breaker-columns",
    "kind": "safe",
    "fn": add_backend_health_columns,
    "domains": ("runtime",),
    "post_check": verify_backend_health_columns,
}