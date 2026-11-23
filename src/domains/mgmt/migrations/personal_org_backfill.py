"""个人组织与 org_id 存量回填（mgmt 域）—— 2026-11-22 恢复。

背景：原 `db_setup._migrate_create_personal_orgs()` / `_migrate_data_to_personal_orgs()`
第一段随废表 `user_accounts` 清退被整段删除，但它们里面“给存量账号补个人组织、
给 org_id 为空的 managed_repos 回填”并不依赖废表——只是当时按整函数处理删过头了。
本迁移用 `grape_accounts` 作为账号来源把这段能力恢复，行为与旧实现等价且幂等：

  1. 每个 status=1 且没有个人组织的账号 → 建个人组织 + owner 成员
     （slug 冲突时退 `personal-<id>-<username[:8]>`，与登录期补建口径一致）
  2. `managed_repos.org_id` 为空的行 → 按 `user_repos.is_admin` 找到管理员 → 回填其个人组织

不在本迁移内：runtime 归属表的回填（runtime_credentials / agent_runtimes / skill /
events / agent_tasks / devops_agent_session / issue）由 runtime 域自己的迁移完成，
避免跨域直写别人的表。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class PersonalOrgBackfillMigration:
    """幂等：补个人组织 + 回填 managed_repos.org_id（账号源=grape_accounts）。"""

    NAME = "personal-orgs-from-accounts"

    def __call__(self, conn: Any = None) -> dict:
        from src.api import db_setup as ds

        target = ds.db if conn is None else conn
        c = target.cursor()
        if not ds._table_present(c, "grape_accounts"):
            raise ds._NonOwnerObject(
                "grape_accounts 不在本库（非 mgmt owner 域），跳过个人组织回填")

        from src.kernel.id_gen import uuid7_str

        stats = {"orgs_created": 0, "managed_repos": 0}

        # 1) 个人组织
        c.execute("SELECT id, username FROM grape_accounts WHERE status=1")
        for uid, uname in list(c.fetchall() or []):
            uid_s = str(uid)
            c.execute("SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid_s,))
            if c.fetchone():
                continue
            slug = f"personal-{uid_s}"
            c.execute("SELECT id FROM organization WHERE slug=?", (slug,))
            if c.fetchone():
                slug = f"personal-{uid_s}-{str(uname or '')[:8]}"
            org_id = uuid7_str()
            name = f"{uname or uid_s}的个人空间"
            c.execute(
                "INSERT INTO organization (id, name, slug, description, owner_id, is_personal) "
                "VALUES (?, ?, ?, '', ?, 1)",
                (org_id, name, slug, uid_s),
            )
            c.execute(
                "INSERT OR IGNORE INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, uid_s),
            )
            stats["orgs_created"] += 1

        # 2) managed_repos.org_id 回填（只处理空值；管理员 → 其个人组织）
        if ds._table_present(c, "managed_repos"):
            c.execute("SELECT repo_key FROM managed_repos WHERE org_id='' OR org_id IS NULL")
            for (repo_key,) in list(c.fetchall() or []):
                c.execute(
                    "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 LIMIT 1",
                    (repo_key,),
                )
                row = c.fetchone()
                if not row:
                    continue
                c.execute(
                    "SELECT id FROM organization WHERE owner_id=? AND is_personal=1",
                    (str(row[0]),),
                )
                org = c.fetchone()
                if not org:
                    continue
                cur = c.execute(
                    "UPDATE managed_repos SET org_id=? WHERE repo_key=?"
                    " AND (org_id='' OR org_id IS NULL)",
                    (org[0], repo_key),
                )
                stats["managed_repos"] += int(getattr(cur, "rowcount", 0) or 0)

        target.commit()
        log.info(f"personal org backfill: {stats}")
        return stats


def migrate_personal_orgs_from_accounts(conn: Any = None) -> dict:
    """注册给 `register_migration(fn=...)` 的薄函数。"""
    return PersonalOrgBackfillMigration()(conn)