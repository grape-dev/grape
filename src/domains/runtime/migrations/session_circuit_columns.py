"""agent_runtimes session 通道熔断列 + LLM provider 状态列（迁移 202611250007）。

Issue #4346：`session/new` 100s 超时无分类、无换引擎重试、无 session 层熔断；
引擎缺 LLM provider 时派发期才 500。本迁移给存量库补列：

- session_fail_streak / session_fail_total：连续/累计 session 失败计数；
- session_circuit_until / session_circuit_half_open：session 通道熔断与半开探测占位；
- session_last_error_code / session_last_failure_at / session_last_success_at：排障透出；
- llm_provider_ok / llm_provider_reason：注册/启用期 provider 校验结果（0 = 不进调度池）。

归属：agent_runtimes 归 runtime（configs/table_ownership.json），补列 SQL 住本域
migrations 下（跨域守卫 P0-2）；注册由 src/api/db_setup.py 引导器做。
新装库由 schema_ddl._AGENT_RUNTIMES_DDL 直接带列，本迁移只服务存量库。

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
        conn.commit()


def verify_session_circuit_columns(conn=None) -> None:
    """后置校验：九列在位才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    cols = {r[1] for r in conn.cursor().execute(f"PRAGMA table_info({TABLE})").fetchall()}
    missing = [col for col, _ddl in COLUMNS if col not in cols]
    if missing:
        raise RuntimeError(
            f"202611250007 post_check 未收敛：{TABLE} 缺列 {missing}（不盖章，下次启动重试）")
    log.info(f"202611250007 post_check 通过：{TABLE} session 熔断/provider 列在位")


MIGRATION = {
    "version": "202611250007",
    "name": "session-circuit-breaker-provider-columns",
    "kind": "safe",
    "fn": add_session_circuit_columns,
    "domains": ("runtime",),
    "post_check": verify_session_circuit_columns,
}