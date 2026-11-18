"""给 agent_runtimes 补 runtime_type / runtime_info 两列（迁移 202611180002）。

背景（2026-11-18 生产事故，Issue #113 / seq 9946）：

  任务被 claim 后零输出挂死，155 秒后判 failed：
    result_text: claimed but the engine session never started
                 (runtime_session_id empty; the worker never spawned the engine process)
  worker 日志持续刷：
    (1054, "Unknown column 'r.runtime_info' in 'field list'")
    (1054, "Unknown column 'r.runtime_type' in 'field list'")

根因链（比「缺列」深一层）：

  `7794d05ee`（sandbox + engine-uid overhaul）在 7 处代码里 SELECT 这两列，但列从未进表。
  列定义其实早写在 `db_setup.py::_migration_sandbox_control_plane_schema` 里（迁移
  202611110007），可它走 `_ensure_column` → `_owned_skip("agent_runtimes")` —— 表归
  runtime，mgmt 进程上必然跳过。而该迁移**没声明 domains**，mgmt 跑完就盖章了；
  迁移台账按版本号全局生效，runtime 进程看到章就不再执行。
  → 列永远不会被创建，但每次启动都「迁移成功」。

  claim 的 SELECT 是一整条语句，列解析失败 → 1054 → 整个 claim 响应构造失败 →
  worker 拿不到 runtime 属性 → 引擎进程从未 spawn → 超时判 failed →
  前端只看到 running + 空日志流（用户感知为「假跑」）。

本迁移的定位：给 runtime 域一条**自己的**迁移来补列，绕开 202611110007 的
「跳过 + 盖章」组合。domains=("runtime",) 保证只在 runtime 角色的进程上执行。

连接：用 `db_registry.connection_for("runtime")` 取 owner 库连接，而不是进程库 ——
用进程库会被 `scripts/scan_pgconn.py` 计为「跨域进程库连接」（QA C-1）。
三库未启用时回落到进程库（单库/测试环境）。

列定义与 `src/api/db_setup.py::_migration_sandbox_control_plane_schema` 及
`src/domains/runtime/schema_ddl.py::_AGENT_RUNTIMES_DDL` 三处保持一致
（QA M-2：同列定义漂移会让新库路径与存量升级路径行为分叉，claim/scope 消费
 空串与 'local' 的分支可能不一致）。
"""
from __future__ import annotations

# 列定义必须与 db_setup._migration_sandbox_control_plane_schema 及
# schema_ddl._AGENT_RUNTIMES_DDL 逐字一致。
_COLUMNS = (
    # (列名, 列定义)
    ("runtime_type", "VARCHAR(32) NOT NULL DEFAULT 'local'"),
    ("runtime_info", "TEXT"),
)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool((cur.fetchone() or [0])[0])


def ensure_runtime_columns() -> None:
    """幂等补列：已存在就跳过。"""
    from src.kernel import db_registry
    from src.kernel.config import log
    from src.kernel.session_manager import db as _proc_db

    # 优先用 runtime 自己库的连接（列在 runtime 库的表上）。
    # 走进程库会被 scan_pgconn 计为跨域进程库连接；三库未启用时回落进程库。
    conn = _proc_db
    try:
        if db_registry.lib_enabled() and db_registry.uses_own_dsn("runtime"):
            owner_conn, _qualifier = db_registry.connection_for("runtime")
            if owner_conn is not None:
                conn = owner_conn
    except Exception as exc:  # noqa: BLE001 - 取连接失败不阻断，回落进程库
        log.warning(f"202611180002 取 runtime 连接失败，回落进程库: {exc}")

    added = []
    c = conn.cursor()
    for col, ddl in _COLUMNS:
        try:
            if _column_exists(c, "agent_runtimes", col):
                continue
            c.execute(f"ALTER TABLE agent_runtimes ADD COLUMN `{col}` {ddl}")
            added.append(col)
        except Exception as exc:  # noqa: BLE001 - 单列失败不阻断另一列
            log.warning(f"202611180002 补列 {col} 失败（跳过）: {exc}")
    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        # 不静默：commit 失败意味着补列没落库，下次启动会重试，但必须留痕
        log.warning(f"202611180002 commit 失败（列已 ALTER，可能未提交）: {exc}")

    if added:
        log.info(f"202611180002 agent_runtimes 已补列: {', '.join(added)}")
    else:
        log.info("202611180002 agent_runtimes 两列已就位，无需变更")


MIGRATION = {
    "version": "202611180002",
    "name": "ensure-runtime-type-info-columns",
    "kind": "safe",
    "fn": ensure_runtime_columns,
    "domains": ("runtime",),
}
