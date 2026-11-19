"""沙箱配置面的建表与 seed（mgmt 侧，owner 域职责）。

沙箱相关 7 张表里 6 张归 mgmt（配置面）：

    grape_t_engine_sandbox_paths
    grape_t_engine_sandbox_config
    grape_t_engine_sandbox_whitelist
    grape_t_task_proxy_tokens
    grape_t_user_llm_credentials
    grape_t_sandbox_templates

第 7 张 grape_t_cloud_sandboxes 是运行实例面，归 runtime，由
runtime/agents/sandbox/schema.py 建。

2026-11-19 修正：这 6 张表的 DDL 与官方域名 seed 原先写在 runtime 的
ensure_sandbox_tables() 里，而该函数被四个进程的启动路径都调用 —— 三库拓扑下
每个进程把表建进自己的库，既越权又在 runtime 库里留下空壳表。DDL 与 seed 数据
移到 mgmt/schema_ddl.py（SANDBOX_CONFIG_DDL / OFFICIAL_SANDBOX_DOMAINS），
建表与 seed 由本模块在 mgmt 进程启动时执行。

幂等：CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE + 表空才 seed。
"""

from __future__ import annotations

from src.kernel.config import log
from src.kernel.session_manager import db, db_lock

from src.domains.mgmt.schema_ddl import OFFICIAL_SANDBOX_DOMAINS, SANDBOX_CONFIG_DDL

# 用「首表」代表整族做归属判断：同一批迁移建出来的配置面表归属一致。
_GUARD_TABLE = "grape_t_engine_sandbox_whitelist"


def ensure_sandbox_config_tables(*, seed: bool = True) -> bool:
    """建/补齐沙箱配置面 6 表，并按需 seed 官方域名。

    Returns:
        True 表示本进程是 owner（mgmt）并已执行；False 表示按归属跳过。
    """
    from src.kernel import table_ownership as _own

    if _own.should_skip(_GUARD_TABLE):
        log.info("[DDL_SKIP] 沙箱配置面 6 表：owner 非本进程，跳过")
        return False

    try:
        with db_lock:
            for name, ddl in SANDBOX_CONFIG_DDL:
                db.executescript(ddl.strip() + ";")
            db.commit()
        log.info("沙箱配置面 6 表已确保存在")
    except Exception as e:
        # 建表失败不应打断启动：真实故障会在首次读写时暴露，且这里多是方言差异。
        log.warning(f"沙箱配置面建表失败（将在首次读写时暴露）: {e}")
        return False

    if seed:
        try:
            _seed_official_domains()
        except Exception as e:
            log.warning(f"沙箱官方域名 seed 失败: {e}")
    return True


def _seed_official_domains() -> None:
    """表空时 seed 官方域名（幂等：只在该表 0 行时写入）。"""
    from src.kernel.sandbox_uid import new_row_uid

    with db_lock:
        cur = db.cursor()
        cur.execute("SELECT COUNT(1) FROM grape_t_engine_sandbox_whitelist")
        row = cur.fetchone()
        count = (row[0] if row else 0) or 0
        if count:
            log.info(
                "grape_t_engine_sandbox_whitelist 已有 %s 行，跳过 seed（不覆盖运营改动）", count
            )
            return
        for domain, method, path_prefix, scheme, port in OFFICIAL_SANDBOX_DOMAINS:
            db.execute(
                "INSERT OR IGNORE INTO grape_t_engine_sandbox_whitelist "
                "(id, backend_name, domain, method, path_prefix, scheme, port) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_row_uid(), "*", domain, method, path_prefix, scheme, port),
            )
        db.commit()
    log.info("Seeded grape_t_engine_sandbox_whitelist: %s official domains", len(OFFICIAL_SANDBOX_DOMAINS))