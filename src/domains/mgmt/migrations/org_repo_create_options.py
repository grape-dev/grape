"""组织仓库创建选项列：幂等修复 + 后置校验（迁移 202611230003）。

背景（2026-11-23 测试环境实测）：`POST /api/orgs` 500，日志
`(1054, "Unknown column 'org_repo_provider' in 'field list'")`。
`organization` 缺 5 列，但 202611183011 在 `grape_t_schema_migrations` 里已盖章。

为什么会出现「盖章但没建列」：
- 202611183011 的 `_ensure_column` 没传 `strict` / `raise_on_guard`。域守卫跳过
  或 ALTER 降级失败都会正常返回，执行器据此盖章；版本号一旦盖死，下次启动不再
  重试（与 issue.vcs_exists / cat_l1_id / takeover 同族）。
- 也可能是当时迁移跑在另一个库、台账随后被带到目标库 —— 无论哪种，已盖章的
  库都不会自愈。

修复口径：
- 用**新版本号**重跑补列（改老迁移的 strict 对已盖章库无效，只改 checksum）；
- `strict=True + raise_on_guard=True`：建不出来就抛错不盖章，下次启动重试；
- `allow_copy=True`：保证 MySQL 5.7（INSTANT/INPLACE 不支持 TEXT 补列）也能建；
- 带 `post_check`：列不全即使 fn 正常返回也不盖章（Issue #3648 的口径）。

SQL 住属主域 migrations 下（`organization` owner=mgmt，见
configs/table_ownership.json）；注册由 src/api/db_setup.py 引导器做。
"""
from __future__ import annotations

#: 列名与 DDL 的单一真源（schema_ddl.py 的建表口径是同名 5 列；TEXT 不带默认值
#: 与 MySQL 上 `TEXT DEFAULT ''` 被静默丢弃后的实际形态等价，应用层本就 `or ''`）。
ORG_REPO_COLUMN_DDL = (
    ("org_repo_provider", "TEXT"),
    ("org_repo_owner", "TEXT"),
    ("org_repo_name", "TEXT"),
    ("org_repo_path", "TEXT"),
    ("org_repo_private", "INTEGER DEFAULT 1"),
)


def ensure_org_repo_create_columns(conn=None) -> None:
    """幂等补齐 organization 的 5 个仓库创建选项列；失败抛错不盖章。"""
    from contextlib import nullcontext

    from src.api.db_setup import _ensure_column
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        for name, ddl in ORG_REPO_COLUMN_DDL:
            _ensure_column(c, "organization", name, ddl,
                           allow_copy=True, strict=True, raise_on_guard=True)
        conn.commit()


def verify_org_repo_create_columns(conn=None) -> list:
    """后置校验：organization 表与 5 列必须全部在位，否则抛错（执行器不盖章）。

    整表缺失不能静默通过（Issue #3648 口径）：这里与 mgmt 分类字典校验一致，
    统一报「表/列缺失」，既让启动日志可检索，也避免临时库/半初始化库被当成
    已收敛。仅在没有 organization 表的单测临时库才会触发，生产库恒存在。
    """
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    present = {str(r[1]) for r in c.execute("PRAGMA table_info(organization)").fetchall()}
    if not present:
        raise RuntimeError(
            "organization 表/列缺失（仓库创建选项列后置校验失败）：表不存在")
    missing = [name for name, _ in ORG_REPO_COLUMN_DDL if name not in present]
    if missing:
        raise RuntimeError(
            f"organization 表/列缺失（仓库创建选项列后置校验失败）：缺列 {missing}")
    return []
