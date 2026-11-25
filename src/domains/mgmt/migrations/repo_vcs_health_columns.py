"""managed_repos 补「VCS 存在性熔断」8 列（迁移 202611250001，Issue #3759）。

上游 VCS 已不存在的孤儿仓库卡（如 `huaweicloud-mate/repository-requests`）当前
没有任何字段承载「仓库不在上游」这一事实：巡检每轮逐 issue 打上游，404 被当作
可重试错误，一小时 76 条 WARNING 且永不收敛。

本迁移给存量库补台账列（新装库由 `src/domains/mgmt/user/db.py::_MANAGED_REPOS_DDL`
直接带列）：

- vcs_status / vcs_missing_since：熔断状态与首次发现时间；
- vcs_last_error_code / vcs_last_error_at：最近一次永久失败（4xx 除 429）；
- vcs_probe_result / vcs_probe_at：仓库级存在性探测结论与时间；
- vcs_next_probe_at / vcs_fail_count：退避放行时间与连续失败计数（5m→15m→1h 封顶）。

归属：managed_repos 归 mgmt 域（configs/table_ownership.json），补列 SQL 住本域
migrations 下满足跨域守卫（R8 只管 kernel 写域表，但 api 层的表名写语句会被
tests/arch/test_foreign_table_refs.py 判成新增跨域引用）——注册由
`src/api/db_setup.py` 的 `register_migration` 做，本模块只导出实现。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

TABLE = "managed_repos"

#: (列名, DDL)。VARCHAR 而非 TEXT：MySQL 8.0.13 之前 TEXT 不支持 DEFAULT，
#: 且本仓实测 `TEXT NOT NULL DEFAULT ''` 落地为 `text NOT NULL DEFAULT NULL`
#: （strict 下任何不给值的 INSERT 直接 1364）。这些列都是短码/时间戳，255 足够。
COLUMNS = (
    ("vcs_status", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_missing_since", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_last_error_code", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_last_error_at", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_probe_result", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_probe_at", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_next_probe_at", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("vcs_fail_count", "INTEGER NOT NULL DEFAULT 0"),
)


def add_repo_vcs_health_columns(conn: Any = None) -> None:
    """幂等补列（缺列才 ALTER；重复执行零改动）。"""
    from src.api import db_setup as ds

    target = conn if conn is not None else ds.db
    with ds.db_lock:
        c = target.cursor()
        try:
            for col, ddl in COLUMNS:
                # strict + raise_on_guard：`_ensure_column` 在被域归属守卫跳过时
                # 只记人工清单并正常返回，而执行器约定「fn 正常返回=盖章」——
                # 列没建成却把版本号盖死，下次启动不再重试（2026-11-18 事故同型）。
                ds._ensure_column(c, TABLE, col, ddl, allow_copy=True,
                                  strict=True, raise_on_guard=True)
            target.commit()
        finally:
            try:
                c.close()
            except Exception:
                pass


def verify_repo_vcs_health_columns(conn: Any = None) -> None:
    """后置校验：8 列在位才盖章；缺任意一列抛错（不盖章，下次启动重试）。"""
    from src.api import db_setup as ds

    target = conn if conn is not None else ds.db
    cols = {r[1] for r in target.cursor().execute(f"PRAGMA table_info({TABLE})").fetchall()}
    missing = [col for col, _ddl in COLUMNS if col not in cols]
    if missing:
        raise RuntimeError(
            f"202611250001 post_check 未收敛：{TABLE} 缺列 {missing}（不盖章，下次启动重试）")
    log.info(f"202611250001 post_check 通过：{TABLE} VCS 熔断台账列在位")


MIGRATION = {
    "version": "202611250001",
    "name": "repo-vcs-health-breaker-columns",
    "kind": "safe",
    "fn": add_repo_vcs_health_columns,
    "domains": ("mgmt",),
    "post_check": verify_repo_vcs_health_columns,
}