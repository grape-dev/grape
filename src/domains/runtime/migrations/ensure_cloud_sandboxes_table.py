"""把 grape_t_cloud_sandboxes 建到 runtime 域库里（迁移 202611180001）。

背景：这张表归 runtime（沙箱实例数据就是运行域的资产），但它是在归属调整为 runtime 之前
建在 mgmt 库里的历史产物 —— mgmt 库那份是空表（0 行），runtime 库反而没有。
DDL 与 src/domains/runtime/agents/sandbox/schema.py::_TABLES 中的定义逐字一致，
沙箱初始化那条路也仍会执行（IF NOT EXISTS，幂等）。

domains=("runtime",)：只有 runtime 角色的进程会跑这条。
"""
from __future__ import annotations

_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_cloud_sandboxes (
    id TEXT PRIMARY KEY,
    issue_id VARCHAR(255) NOT NULL,
    repo_key VARCHAR(255) NOT NULL,
    sandbox_id VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    provider_id TEXT DEFAULT '',
    state VARCHAR(32) DEFAULT 'creating',
    sandbox_url VARCHAR(512) DEFAULT '',
    created_at TEXT,
    last_refresh_at TEXT,
    agent_installed INTEGER DEFAULT 0,
    kill_attempts INTEGER DEFAULT 0,
    task_id VARCHAR(255) DEFAULT '',
    stage VARCHAR(64) DEFAULT '',
    expire_at VARCHAR(32) DEFAULT '',
    attempt INTEGER DEFAULT 0,
    created_by INTEGER DEFAULT 0,
    template_id VARCHAR(255) DEFAULT '',
    template_name VARCHAR(255) DEFAULT '',
    engine_type VARCHAR(64) DEFAULT '',
    last_error TEXT DEFAULT '',
    UNIQUE(sandbox_id)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_cloud_sandboxes_repo ON grape_t_cloud_sandboxes(repo_key)",
    "CREATE INDEX IF NOT EXISTS idx_cloud_sandboxes_state ON grape_t_cloud_sandboxes(state)",
    "CREATE INDEX IF NOT EXISTS idx_cloud_sandboxes_task ON grape_t_cloud_sandboxes(task_id)",
)


def ensure_cloud_sandboxes_table() -> None:
    """在 owner 域（runtime）库建表 + 索引；幂等。

    Issue #3527：函数体补所有权守卫。迁移执行器在「多 DSN 已配但
    GRAPE_MIGRATION_OWNER 为空」时不筛域、会跑全量登记表，mgmt/insight 进程
    曾因此把本表建进自己的库（同名空表遮蔽沙箱列表）。守卫按
    configs/table_ownership.json 判定，非 runtime 域直接跳过。
    """
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    try:
        from src.kernel import table_ownership as _own
        if _own.should_skip("grape_t_cloud_sandboxes"):
            log.info("[DDL_SKIP] `grape_t_cloud_sandboxes`: 归属 runtime，非本域跳过建表")
            return
    except Exception as _e:  # noqa: BLE001 - 归属清单不可用时按本域表处理（本模块即 runtime）
        log.warning(f"table_ownership 不可用，按本域处理建表: {_e}")

    with db_lock:
        c = db.cursor()
        c.execute(_DDL)
        for idx in _INDEXES:
            try:
                c.execute(idx)
            except Exception as exc:  # noqa: BLE001 - 索引名冲突等不影响建表
                log.warning(f"202611180001 建索引跳过: {exc}")
        db.commit()
    log.info("202611180001 grape_t_cloud_sandboxes 已在 owner 域（runtime）就位")


MIGRATION = {
    "version": "202611180001",
    "name": "ensure-cloud-sandboxes-in-runtime",
    "kind": "safe",
    "fn": ensure_cloud_sandboxes_table,
    "domains": ("runtime",),
}
