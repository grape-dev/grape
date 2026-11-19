"""沙箱 DB schema 管理（runtime 侧只负责本域表）。

独立模块——bootstrap.py 调用 ensure_sandbox_tables() 作为第一步，
但建表逻辑不放在 bootstrap 里，避免职责混淆。

⚠ 表归属（configs/table_ownership.json 是唯一权威）
------------------------------------------------------
沙箱相关共 7 张表，只有 1 张归 runtime：

  grape_t_cloud_sandboxes            → runtime   ← 本模块建
  grape_t_engine_sandbox_paths       → mgmt
  grape_t_engine_sandbox_config      → mgmt
  grape_t_engine_sandbox_whitelist   → mgmt
  grape_t_task_proxy_tokens          → mgmt
  grape_t_user_llm_credentials       → mgmt
  grape_t_sandbox_templates          → mgmt

2026-11-19 修正：这 6 张 mgmt 表的 DDL 与 seed 原先写在本模块里，而
ensure_sandbox_tables() 被 main.py 和 runtime/agent.py 都调用 —— 四个进程启动时
各自把表建进**自己的库**。三库拓扑下这既是越权建表，也在 runtime 库里留下空的
同名空壳表。DDL 与 seed 已迁至 src/domains/mgmt/schema_ddl.py
（SANDBOX_CONFIG_DDL / OFFICIAL_SANDBOX_DOMAINS），由 mgmt 进程的启动路径建表；
本模块只建 grape_t_cloud_sandboxes。runtime 读白名单/路径/模板一律走
mgmt.public 的具名原语（engine_sandbox_whitelist_domains 等），不直查表。

幂等：CREATE TABLE IF NOT EXISTS。
区分两类失败：
- TransientError: DB 连接断开、超时（可重试）
- SchemaError: 表结构冲突、权限不足（不可重试，fail-fast）
"""

from __future__ import annotations

from src.kernel.config import log


class SchemaError(Exception):
    """schema 冲突——不可重试，应 fail-fast。"""


class TransientError(Exception):
    """DB 临时不可用——可重试。"""


# 归 runtime 的沙箱表（唯一一张）。其余 6 张在 mgmt。
_CLOUD_SANDBOXES_DDL = """CREATE TABLE IF NOT EXISTS grape_t_cloud_sandboxes (
    id TEXT PRIMARY KEY,
    issue_id VARCHAR(255) NOT NULL,
    repo_key VARCHAR(255) NOT NULL,
    sandbox_id VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'creating',
    endpoint TEXT,
    created_at VARCHAR(32) NOT NULL DEFAULT '',
    updated_at VARCHAR(32) NOT NULL DEFAULT '',
    released_at VARCHAR(32) DEFAULT NULL,
    meta_json TEXT
)"""


def ensure_sandbox_tables() -> None:
    """确保 runtime 自己的沙箱表存在（幂等）。

    只建 grape_t_cloud_sandboxes —— 沙箱配置面的 6 张表归 mgmt，由 mgmt 进程的
    启动路径建（见模块 docstring）。这里不建、不 seed、不写别域表。

    schema 冲突时 raise SchemaError（不可重试）。
    DB 不可用时 raise TransientError（可重试）。
    调用方（bootstrap.py）决定重试/降级/fail-fast。
    """
    from src.kernel.session_manager import db, db_lock

    try:
        from src.kernel import table_ownership as _own

        # 防御：万一归属清单把这张表改判给别域，本域就不该再建它。
        if _own.should_skip("grape_t_cloud_sandboxes"):
            log.info("[DDL_SKIP] `grape_t_cloud_sandboxes`: 归属已变更，非本域跳过建表")
            return
    except Exception as _e:  # 归属清单不可用时按本域表处理（本文件即为 runtime）
        log.warning(f"table_ownership 不可用，按本域处理建表: {_e}")

    try:
        with db_lock:
            db.executescript(_CLOUD_SANDBOXES_DDL + ";")
            db.commit()
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("conflict", "permission", "denied", "readonly", "syntax")):
            raise SchemaError(f"cloud_sandboxes DDL failed: {e}") from e
        raise TransientError(f"cloud_sandboxes DDL transient: {e}") from e


def verify_sandbox_tables() -> list[str]:
    """返回缺失的 runtime 沙箱表名（供启动自检；空 list 表示 OK）。"""
    from src.kernel.session_manager import db, db_lock

    missing: list[str] = []
    with db_lock:
        try:
            cur = db.cursor()
            cur.execute("SELECT 1 FROM grape_t_cloud_sandboxes LIMIT 1")
            cur.fetchall()
        except Exception:
            missing.append("grape_t_cloud_sandboxes")
    return missing