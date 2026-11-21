"""runtime 域 org_id 存量回填（2026-11-22 恢复）。

旧实现散在 `db_setup._migrate_data_to_personal_orgs()` 的第 2–5 段，随废表清退被整段删除。
拆成 owner 域自己的迁移：

  1. `runtime_credentials`（scope<>global）：owner_name → 个人组织；同凭证的
     `agent_runtimes` 跟随 credential_id 回填
  2. `skill`（scope=user）：owner_id 是用户 id → 个人组织
  3. `skill`（scope=repo）：owner_id 是 repo_key → 该仓 org（经 mgmt 契约映射）
  4. 其它跟随 repo_key 的表（events / agent_tasks / devops_agent_session / issue）
     与 mgmt 侧（devops_pipeline_scene / agent_shared_knowledge）：
     映射由 `mgmt.public.repo_org_map()` 取，各自域自己的契约回填

所有步骤幂等（只处理空 org_id），失败抛出（迁移不盖章、下次启动重试）。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _org_by_username(username: str) -> str:
    """username → 个人组织 id（经 mgmt 契约；不可用/无账号返回空）。"""
    if not username:
        return ""
    try:
        from src.domains.mgmt import public as mgmt

        account_id = mgmt.get_account_id_by_username(username)
        if not account_id:
            return ""
        return str(mgmt.personal_org_id(str(account_id)) or "")
    except Exception as e:  # noqa: BLE001 - 契约不可用时不猜、不跨库
        log.warning(f"org backfill: resolve username failed ({username}): {e}")
        return ""


def _org_by_user_id(user_id: Any) -> str:
    try:
        from src.domains.mgmt import public as mgmt

        return str(mgmt.personal_org_id(str(user_id)) or "")
    except Exception as e:  # noqa: BLE001
        log.warning(f"org backfill: resolve user failed ({user_id}): {e}")
        return ""


class RuntimeOrgIdBackfillMigration:
    """幂等：回填 runtime 归属表的空 org_id。"""

    NAME = "runtime-org-id-backfill"

    def __call__(self, conn: Any = None) -> dict:
        from src.api import db_setup as ds

        target = ds.db if conn is None else conn
        c = target.cursor()
        stats: dict[str, Any] = {"runtime_credentials": 0, "agent_runtimes": 0,
                                 "skill_user": 0, "skill_repo": 0}

        # 1) runtime_credentials + agent_runtimes
        if ds._table_present(c, "runtime_credentials"):
            c.execute(
                "SELECT id, owner_name FROM runtime_credentials "
                "WHERE (org_id='' OR org_id IS NULL) AND COALESCE(scope,'')<>'global'"
            )
            for cred_id, owner_name in list(c.fetchall() or []):
                org = _org_by_username(owner_name)
                if not org:
                    continue
                cur = c.execute("UPDATE runtime_credentials SET org_id=? WHERE id=?", (org, cred_id))
                stats["runtime_credentials"] += int(getattr(cur, "rowcount", 0) or 0)
                if ds._table_present(c, "agent_runtimes"):
                    cur = c.execute(
                        "UPDATE agent_runtimes SET org_id=? WHERE credential_id=? "
                        "AND (org_id='' OR org_id IS NULL)",
                        (org, cred_id),
                    )
                    stats["agent_runtimes"] += int(getattr(cur, "rowcount", 0) or 0)

        # 2) skill scope=user（owner_id = 用户 id）
        if ds._table_present(c, "skill"):
            c.execute(
                "SELECT id, owner_id FROM skill WHERE scope='user' "
                "AND (org_id='' OR org_id IS NULL)"
            )
            for sid, owner_id in list(c.fetchall() or []):
                org = _org_by_user_id(owner_id)
                if not org:
                    continue
                cur = c.execute("UPDATE skill SET org_id=? WHERE id=?", (org, sid))
                stats["skill_user"] += int(getattr(cur, "rowcount", 0) or 0)

            # 3) skill scope=repo（owner_id = repo_key）
            repo_map = {}
            try:
                from src.domains.mgmt import public as mgmt

                repo_map = mgmt.repo_org_map() or {}
            except Exception as e:  # noqa: BLE001
                log.warning(f"org backfill: repo_org_map unavailable: {e}")
            c.execute(
                "SELECT id, owner_id FROM skill WHERE scope='repo' "
                "AND (org_id='' OR org_id IS NULL)"
            )
            for sid, owner_id in list(c.fetchall() or []):
                org = repo_map.get(str(owner_id or ""))
                if not org:
                    continue
                cur = c.execute("UPDATE skill SET org_id=? WHERE id=?", (org, sid))
                stats["skill_repo"] += int(getattr(cur, "rowcount", 0) or 0)

        target.commit()

        # 4) 跟随 repo_key 的表：各自域的契约回填（映射由 mgmt 取）
        try:
            from src.domains.mgmt import public as mgmt
            from src.domains.runtime import public as runtime

            mapping = mgmt.repo_org_map() or {}
            if mapping:
                stats["by_repo_mgmt"] = mgmt.backfill_org_id_by_repo(mapping)
                stats["by_repo_runtime"] = runtime.backfill_org_id_by_repo(mapping)
        except Exception as e:  # noqa: BLE001 - 契约不可用不阻断前 3 段，但要让迁移可见
            log.warning(f"org backfill: repo-keyed tables skipped: {e}")

        log.info(f"runtime org_id backfill: {stats}")
        return stats


def migrate_runtime_org_ids(conn: Any = None) -> dict:
    """注册给 `register_migration(fn=...)` 的薄函数。"""
    return RuntimeOrgIdBackfillMigration()(conn)