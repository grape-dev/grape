"""仓库迁移（Issue #540）：repo_key 原子改写。

当仓库在 VCS 侧从 A 组织转移到 B 组织后，Grape 平台内以 repo_key 为索引的全部
关联记录需原子改写为新 repo_key，无需「解除托管 → 重新托管」两步操作。

设计（docs/issue-540/spec.md）：
- DB 改写封装为单事务（与 repo_cleanup 对称），失败 rollback + 抛 RepoMigrateError；
- managed_repos 主键 repo_key 直接 UPDATE（SQLite 不能改主键值，需 DELETE+INSERT）；
- user_repos 复合主键 (user_id, repo_key)，UNIQUE 冲突时删旧留新（保留迁移行）；
- 其余 _REPO_KEY_TABLES：repo_key 列 UPDATE（UNIQUE(repo_key) 冲突时删旧留新）；
- skill(scope='repo') owner_id 改写；
- meta：onboarded:* / cleanup_pending:* 改写；
- 文件系统迁移（workspace 知识基线目录）在 DB 提交后执行；
- 全部动作写 audit_logs。

不重建 webhook / 不重新邀请 bot：webhook_id / bot_token / webhook_token / bot_username
等托管字段随 managed_repos 行整体迁移保留。
"""
from __future__ import annotations

import os
import shutil

from src.config import log


class RepoMigrateError(Exception):
    """迁移失败（DB 事务已回滚，可安全重试）。"""


# 以 repo_key 为索引的关联表（与 repo_cleanup._REPO_KEY_TABLES 对齐，不含 managed_repos/user_repos）
_REPO_KEY_TABLES = (
    "issue",
    "issue_state",
    "issue_agent_session",
    "events",
    "event_skipped",
    "event_thread",
    "event_route_config",
    "agent_tasks",
    "agent_shared_knowledge",
    "devops_pipeline_scene",
    "chat_conversations",
    "chat_agent_session",
    "devops_agent_session",
    "repo_im_group_binding",
    "pipeline_instances",
)

# 子表（经父表 id 关联，无 repo_key 列）——迁移不改其内容，父表 repo_key 改写后自动跟随


def _normalize_keys(repo_key: str) -> list[str]:
    """repo_key 带/不带 provider 前缀双写兼容（与 repo_cleanup 对称）。"""
    if not repo_key:
        return []
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    base = f"{owner}/{repo}" if owner and repo else repo_key
    keys = [repo_key, f"{provider}:{base}", base]
    return list(dict.fromkeys(k for k in keys if k))


def _existing_tables(c) -> set[str]:
    """探测当前库中实际存在的表。"""
    try:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    except Exception:
        try:
            rows = c.execute("SHOW TABLES").fetchall()
            return {list(r)[-1] for r in rows}
        except Exception:
            return set()


def _table_has_repo_key(c, table: str) -> bool:
    try:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        return "repo_key" in cols
    except Exception:
        return False


def _unique_on_repo_key(c, table: str) -> bool:
    """探测表是否有 UNIQUE(repo_key) 或 repo_key 为主键。"""
    try:
        for ci in c.execute(f"PRAGMA table_info({table})").fetchall():
            if ci[5] == 1 and ci[1] == "repo_key":  # pk == repo_key
                return True
        for idx in c.execute(f"PRAGMA index_list({table})").fetchall():
            if idx[2] == 1:  # unique index
                cols = [x[2] for x in c.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
                if cols and cols[0] == "repo_key":
                    return True
    except Exception:
        pass
    return False


def _update_managed_repos(c, old: str, new: str, new_owner: str, new_repo: str) -> int:
    """managed_repos：repo_key 是主键，不能直接 UPDATE 主键值 → DELETE 旧行 + INSERT 新行。

    保留全部托管字段（webhook_id / bot_token / webhook_token / bot_username /
    dev_scene / org_id / host_user_id / webhook_secret）。
    """
    c.execute(
        "SELECT repo_owner, repo_name, full_name, webhook_id, dev_scene, host_status, "
        "bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, "
        "bot_github_token, org_id, host_user_id, webhook_secret, created_at "
        "FROM managed_repos WHERE repo_key=?",
        (old,))
    row = c.fetchone()
    if not row:
        return 0
    (repo_owner, repo_name, full_name, webhook_id, dev_scene, host_status,
     bot_gc, wh_gc, bot_user, wh_gh, bot_gh, org_id, host_uid, wh_secret, created_at) = row
    new_full = f"{new_owner}/{new_repo}"
    # 先删旧行（避免主键冲突）
    c.execute("DELETE FROM managed_repos WHERE repo_key=?", (old,))
    # 插入新行（保留全部字段，仅改 repo_key/owner/name/full_name）
    try:
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, host_status, bot_gitcode_token, gitcode_webhook_token, "
            "bot_username, github_webhook_token, bot_github_token, org_id, host_user_id, "
            "webhook_secret, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new, new_owner, new_repo, new_full, webhook_id, dev_scene or "", host_status or "",
             bot_gc or "", wh_gc or "", bot_user or "", wh_gh or "", bot_gh or "",
             org_id or 0, host_uid or 0, wh_secret or "", created_at))
    except Exception:
        # 列不全时降级（兼容未迁移的库）
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, "
            "org_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new, new_owner, new_repo, new_full, webhook_id, dev_scene or "",
             bot_gc or "", wh_gc or "", bot_user or "", org_id or 0))
    return 1


def _update_user_repos(c, old: str, new: str, new_owner: str, new_repo: str) -> int:
    """user_repos：复合主键 (user_id, repo_key)，UNIQUE 冲突时删旧留新。

    迁移行保留源行的 is_join/is_admin/role 等用户级状态。
    """
    c.execute("SELECT user_id, owner, name, description, role, is_admin, is_join, "
              "created_at, synced_at FROM user_repos WHERE repo_key=?", (old,))
    rows = c.fetchall()
    if not rows:
        return 0
    cnt = 0
    new_full = f"{new_owner}/{new_repo}"
    for (uid, owner, name, desc, role, is_admin, is_join, created_at, synced_at) in rows:
        # 目标行已存在（同 user_id + new repo_key）→ 删旧行（保留迁移行）
        c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (uid, new))
        try:
            c.execute(
                "INSERT INTO user_repos (user_id, repo_key, owner, name, description, "
                "role, is_admin, is_join, created_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, new, new_owner, new_repo, desc or "", role or "",
                 is_admin or 0, is_join or 0, created_at or "", synced_at or ""))
        except Exception:
            # 列不全降级
            c.execute(
                "INSERT INTO user_repos (user_id, repo_key, owner, name, role, is_admin) "
                "VALUES (?,?,?,?,?,?)",
                (uid, new, new_owner, new_repo, role or "", is_admin or 0))
        cnt += 1
    # 删除所有源 repo_key 行
    c.execute("DELETE FROM user_repos WHERE repo_key=?", (old,))
    return cnt


def _update_repo_key_tables(c, old_keys: list[str], new: str) -> int:
    """其余 _REPO_KEY_TABLES：UPDATE repo_key（UNIQUE 冲突时删旧留新）。"""
    placeholders = ",".join("?" for _ in old_keys)
    cnt = 0
    existing = _existing_tables(c)
    for tbl in _REPO_KEY_TABLES:
        if tbl not in existing or not _table_has_repo_key(c, tbl):
            continue
        is_unique = _unique_on_repo_key(c, tbl)
        if is_unique:
            # UNIQUE(repo_key)：逐行处理冲突（目标 repo_key 已有行 → 删源行）
            c.execute(
                f"SELECT repo_key FROM {tbl} WHERE repo_key IN ({placeholders})",
                old_keys)
            for (rk,) in c.fetchall():
                conflict = c.execute(
                    f"SELECT 1 FROM {tbl} WHERE repo_key=? LIMIT 1", (new,)).fetchone()
                if conflict:
                    # 目标已有行 → 删源行（保留目标行，避免 UNIQUE 冲突）
                    c.execute(f"DELETE FROM {tbl} WHERE repo_key=?", (rk,))
                    cnt += c.rowcount
                else:
                    c.execute(f"UPDATE {tbl} SET repo_key=? WHERE repo_key=?", (new, rk))
                    cnt += c.rowcount
        else:
            # 无 UNIQUE 约束：批量 UPDATE
            c.execute(
                f"UPDATE {tbl} SET repo_key=? WHERE repo_key IN ({placeholders})",
                [new] + old_keys)
            cnt += c.rowcount
    return cnt


def _update_skill(c, old_keys: list[str], new: str) -> int:
    """skill(scope='repo')：owner_id 存 repo_key，UNIQUE(skill_name, scope, owner_id)。"""
    existing = _existing_tables(c)
    if "skill" not in existing:
        return 0
    placeholders = ",".join("?" for _ in old_keys)
    # skill 表用 skill_name（非 name）；冲突处理：目标 owner_id 已有同名 skill → 删旧（保留迁移行）
    c.execute(
        f"SELECT id, skill_name FROM skill WHERE scope='repo' AND owner_id IN ({placeholders})",
        old_keys)
    rows = c.fetchall()
    cnt = 0
    for (sid, skill_name) in rows:
        conflict = c.execute(
            "SELECT 1 FROM skill WHERE scope='repo' AND owner_id=? AND skill_name=? LIMIT 1",
            (new, skill_name)).fetchone()
        if conflict:
            c.execute("DELETE FROM skill WHERE id=?", (sid,))
            cnt += c.rowcount
        else:
            c.execute("UPDATE skill SET owner_id=? WHERE id=?", (new, sid))
            cnt += c.rowcount
    return cnt


def _update_meta(c, old_keys: list[str], new: str) -> int:
    """meta：onboarded:* / cleanup_pending:* 改写。"""
    existing = _existing_tables(c)
    if "meta" not in existing:
        return 0
    cnt = 0
    for prefix in ("onboarded:", "cleanup_pending:"):
        for old in old_keys:
            old_k = f"{prefix}{old}"
            new_k = f"{prefix}{new}"
            # 目标 key 已存在 → 删旧
            conflict = c.execute(
                "SELECT 1 FROM meta WHERE `key`=? LIMIT 1", (new_k,)).fetchone()
            if conflict:
                c.execute("DELETE FROM meta WHERE `key`=?", (old_k,))
                cnt += c.rowcount
            else:
                c.execute("UPDATE meta SET `key`=? WHERE `key`=?", (new_k, old_k))
                cnt += c.rowcount
    return cnt


def migrate_repo_key(repo_key: str, new_owner: str, new_repo: str,
                     operator_id: int = 0) -> dict:
    """原子改写 repo_key 及全部关联数据（Issue #540）。

    Args:
        repo_key: 源 repo_key（带 provider 前缀，如 gitcode:ownerA/repo）
        new_owner/new_repo: 目标组织/仓库名（VCS 侧已转移后的新地址）
        operator_id: 触发用户 ID（审计用）

    Returns:
        {"tables": 改写行数, "dirs_moved": 0/1, "repo_key": new}

    Raises:
        RepoMigrateError: DB 事务失败（已回滚，可重试）
    """
    from src.repo_key import parse_repo_key, to_repo_key
    provider, old_owner, old_repo = parse_repo_key(repo_key)
    repo_key = to_repo_key(repo_key)
    new = f"{provider}:{new_owner}/{new_repo}" if provider else f"{new_owner}/{new_repo}"
    stats = {"tables": 0, "dirs_moved": 0, "repo_key": new}

    if not repo_key or not new_owner or not new_repo:
        return stats
    if new == repo_key:
        # 目标与源相同：不改写
        return stats

    old_keys = _normalize_keys(repo_key)

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            # managed_repos（主键改写：DELETE + INSERT）
            stats["tables"] += _update_managed_repos(c, repo_key, new, new_owner, new_repo)
            # user_repos（复合主键，冲突删旧留新）
            stats["tables"] += _update_user_repos(c, repo_key, new, new_owner, new_repo)
            # 其余 _REPO_KEY_TABLES
            stats["tables"] += _update_repo_key_tables(c, old_keys, new)
            # skill(scope='repo')
            stats["tables"] += _update_skill(c, old_keys, new)
            # meta
            stats["tables"] += _update_meta(c, old_keys, new)
            db.commit()
        except Exception as e:
            db.rollback()
            log.warning(f"Migrate repo_key DB transaction failed (rolled back) {repo_key}->{new}: {e}")
            _audit(repo_key, new, operator_id, {"error": str(e), "rolled_back": True})
            raise RepoMigrateError(f"migrate repo_key failed: {e}") from e

    # 文件系统迁移（DB 提交后）
    stats["dirs_moved"] = _migrate_workspace_dir(repo_key, new)

    log.info(f"Migrate repo_key {repo_key}->{new} done: {stats}")
    _audit(repo_key, new, operator_id, stats)
    return stats


def _migrate_workspace_dir(old: str, new: str) -> int:
    """工作区知识基线目录物理迁移：old → new。"""
    from src.repo_key import workspace_repo_dir
    old_dir = workspace_repo_dir(old)
    new_dir = workspace_repo_dir(new)
    if not os.path.isdir(old_dir):
        return 0
    # 目标已存在 → 合并（目标目录内容优先，源目录补充）
    if os.path.isdir(new_dir):
        try:
            for name in os.listdir(old_dir):
                src_path = os.path.join(old_dir, name)
                dst_path = os.path.join(new_dir, name)
                if os.path.exists(dst_path):
                    continue
                shutil.move(src_path, dst_path)
            shutil.rmtree(old_dir, ignore_errors=True)
            return 1
        except Exception as e:
            log.warning(f"Migrate workspace dir merge failed ({old_dir}->{new_dir}): {e}")
            return 0
    # 目标不存在 → 直接 move 整个目录
    try:
        os.makedirs(os.path.dirname(new_dir), exist_ok=True)
        shutil.move(old_dir, new_dir)
        return 1
    except Exception as e:
        log.warning(f"Migrate workspace dir move failed ({old_dir}->{new_dir}): {e}")
        return 0


def _audit(old: str, new: str, operator_id: int, detail: dict) -> None:
    try:
        from src.auth import audit as auth_audit
        auth_audit.log_audit(
            "repo_migrate",
            user_id=operator_id,
            detail={"old_repo_key": old, "new_repo_key": new, **detail},
        )
    except Exception as e:
        log.warning(f"Migrate audit failed: {e}")
