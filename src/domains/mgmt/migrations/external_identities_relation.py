"""external_identities 身份基表迁移（202611230005）。

Issue #3897 B 组：生产 `grape_prod_runtime.external_identities` 不存在（MySQL 1146），
带 token 的身份解析整体失败。两层原因：

1. **非 owner 库缺跨域视图**：identity 表 owner=mgmt，runtime/insight 侧靠跨库视图读；
   该表长期不在 `scripts/view_create_missing_allowlist.txt`，`domain_view_freshness.py
   --repair` 不会补建 → 视图缺失即 1146。本组把表登记进补建清单（部署脚本负责）。
2. **存量库缺基表**：`external_identities` 只在老轨 `_create_tables()`（`_NEW_TABLES`）里建，
   而老轨对已盖帽库永久跳过 —— 早于该表加入清单的库永远不会被补建。本迁移负责
   owner 库的幂等补表 + 补列（status / unbound_at / token_expires_at）。

归属：`external_identities` 归 mgmt（configs/table_ownership.json），故 domains=("mgmt",)。
跨域视图**不在本迁移里建**：视图的唯一构造实现是 `scripts/view_ddl_builder.py`，
非 owner 进程内 `CREATE VIEW ... owner_schema.table` 会破坏「谁拥有谁写」边界，
并由部署流程（迁移 → `domain_view_freshness.py --repair` → 重启）统一保证。

失败语义：补列走 `_ensure_column(strict=True)`——DDL 所有降级分支失败直接抛出
（执行器约定「抛错=不盖章，下次启动重试」，与 2026-11-06 issue.vcs_exists 事故教训对齐）；
post_check 断言当前库存在同名关系（基表或视图），基表形态还需三列齐全才盖章。
"""
from __future__ import annotations

TABLE = "external_identities"

#: 早期建表可能缺失的列（与 `src/domains/mgmt/schema_ddl.py::_EXTERNAL_IDENTITIES_DDL` 同口径）。
COLUMNS = (
    ("status", "INTEGER DEFAULT 1"),
    ("unbound_at", "TEXT DEFAULT NULL"),
    ("token_expires_at", "TEXT DEFAULT ''"),
)


def ensure_identity_relation(conn=None) -> None:
    """幂等补建 external_identities 基表 + 缺列（仅 owner 库；幂等可重跑）。"""
    from src.api import db_setup as ds
    from src.domains.mgmt.schema_ddl import _EXTERNAL_IDENTITIES_DDL
    from src.domains.runtime.schema_probe import relation_kind
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        kind = relation_kind(c, TABLE)
        if kind is None:
            # 不带 PRIMARY KEY/TEXT 索引的 CREATE TABLE 在 MySQL 侧可重复执行
            # （IF NOT EXISTS），sqlite 侧由下方 commit 落盘；不要再发 PRAGMA
            # （MySQL 方言下未翻译会 1064）。
            c.execute(_EXTERNAL_IDENTITIES_DDL)
        if kind != "view":
            for col, ddl in COLUMNS:
                # strict=True（QA R2 Nit）：所有降级分支失败时 raise 而非只记
                # _DDL_MANUAL_REVIEW 后正常返回——执行器约定「fn 正常返回=盖章」，
                # 静默返回会把「列没建成」盖死成已完成（issue.vcs_exists 事故同态）。
                ds._ensure_column(c, TABLE, col, ddl, strict=True)
        conn.commit()


def verify_identity_relation(conn=None) -> None:
    """后置校验：当前库必须存在 external_identities（基表或跨域视图），否则不盖章。

    QA R2 Nit：基表形态还需 status/unbound_at/token_expires_at 三列齐全——
    「表在、列缺」线上同样表现为查询该列 1054 静默失败，不能盖章；
    视图形态的列是建视图时的快照、归部署流程（view_ddl_builder）管，
    仅断言关系存在。
    """
    from src.domains.runtime.schema_probe import column_exists, relation_kind
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    kind = relation_kind(c, TABLE)
    if kind is None:
        raise RuntimeError(
            f"202611230005 post_check 未收敛：{TABLE} 在当前库不存在"
            f"（owner 库补表失败；非 owner 库需 scripts/domain_view_freshness.py --repair）")
    if kind != "view":
        missing = [col for col, _ in COLUMNS if not column_exists(c, TABLE, col)]
        if missing:
            raise RuntimeError(
                f"202611230005 post_check 未收敛：{TABLE} 缺列 {missing}"
                f"（补列失败未盖章，下次启动重试；MySQL 侧查 _DDL_MANUAL_REVIEW 清单）")
    log.info(f"202611230005 post_check 通过：{TABLE} 形态={kind}")


MIGRATION = {
    "version": "202611230005",
    "name": "external-identities-relation",
    "kind": "safe",
    "fn": ensure_identity_relation,
    "domains": ("mgmt",),
    "post_check": verify_identity_relation,
}
