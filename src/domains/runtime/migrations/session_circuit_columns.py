"""agent_runtimes session 通道熔断列 + LLM provider 状态列 + agent_tasks 重试计数列
（迁移 202611250007）。

Issue #4346：`session/new` 100s 超时无分类、无换引擎重试、无 session 层熔断；
引擎缺 LLM provider 时派发期才 500。本迁移给存量库补列：

- agent_runtimes：
  - session_fail_streak / session_fail_total：连续/累计 session 失败计数；
  - session_circuit_until / session_circuit_half_open：session 通道熔断与半开探测占位；
  - session_last_error_code / session_last_failure_at / session_last_success_at：排障透出；
  - llm_provider_ok / llm_provider_reason：注册/启用期 provider 校验结果（0 = 不进调度池）。
- agent_tasks：
  - session_retry_count：session/new 超时「换引擎重试」专用计数。审查 Required：
    复用 attempts（#403 瞬时重试 / #537 回炉共用）会让历史任一轮 +1 之后的重试被
    静默跳过，spec 的「超时后至少重试 1 次」在该路径不成立。

归属：agent_runtimes / agent_tasks 均归 runtime（configs/table_ownership.json），补列
SQL 住本域 migrations 下（跨域守卫 P0-2）；注册由 src/api/db_setup.py 引导器做。
新装库由 schema_ddl._AGENT_RUNTIMES_DDL / _AGENT_TASKS_DDL 直接带列，本迁移只服务存量库。

版本号说明：基线 202611250001/.../202611250006 已被并行线占用（#4262 renumber 后），
本迁移取下一个空闲号 202611250007（同号会被判「已执行」而永久跳过）。
"""
from __future__ import annotations

TABLE = "agent_runtimes"
COLUMNS = (
    ("session_fail_streak", "INTEGER DEFAULT 0"),
    ("session_fail_total", "INTEGER DEFAULT 0"),
    ("session_circuit_until", "VARCHAR(64) DEFAULT ''"),
    ("session_circuit_half_open", "INTEGER DEFAULT 0"),
    ("session_last_error_code", "VARCHAR(48) DEFAULT ''"),
    ("session_last_failure_at", "VARCHAR(64) DEFAULT ''"),
    ("session_last_success_at", "VARCHAR(64) DEFAULT ''"),
    ("llm_provider_ok", "INTEGER DEFAULT 1"),
    ("llm_provider_reason", "VARCHAR(255) DEFAULT ''"),
)

TASKS_TABLE = "agent_tasks"
TASKS_COLUMNS = (
    ("session_retry_count", "INTEGER DEFAULT 0"),
)


def add_session_circuit_columns(conn=None) -> None:
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
        for col, ddl in TASKS_COLUMNS:
            ds._ensure_column(c, TASKS_TABLE, col, ddl,
                              allow_copy=ddl.startswith("TEXT"), strict=True)
        conn.commit()


def verify_session_circuit_columns(conn=None) -> None:
    """后置校验：agent_runtimes 九列 + agent_tasks 一列在位才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    missing = []
    for table, cols in ((TABLE, COLUMNS), (TASKS_TABLE, TASKS_COLUMNS)):
        present = {r[1] for r in
                   conn.cursor().execute(f"PRAGMA table_info({table})").fetchall()}
        missing.extend(f"{table}.{col}" for col, _ddl in cols if col not in present)
    if missing:
        raise RuntimeError(
            f"202611250007 post_check 未收敛：缺列 {missing}（不盖章，下次启动重试）")
    log.info(f"202611250007 post_check 通过：{TABLE} session 熔断/provider 列 + "
             f"{TASKS_TABLE} session_retry_count 在位")


MIGRATION = {
    "version": "202611250007",
    "name": "session-circuit-breaker-provider-columns",
    "kind": "safe",
    "fn": add_session_circuit_columns,
    "domains": ("runtime",),
    "post_check": verify_session_circuit_columns,
}