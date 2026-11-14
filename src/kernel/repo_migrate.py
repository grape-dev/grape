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

from src.kernel.config import log

class RepoMigrateError(Exception):
    """迁移失败（DB 事务已回滚，可安全重试）。"""

# 表清单与迁移语句已按域下沉：runtime 段在 runtime public（11 张 repo_key 表 + skill + meta），
# mgmt 段在 mgmt public（managed_repos / user_repos + 4 张 repo_key 表）。
# 本模块只做编排：归一化键 → mgmt 段事务 → runtime 段事务 → 文件目录 → 审计。

def _normalize_keys(repo_key: str) -> list[str]:
    """repo_key 带/不带 provider 前缀双写兼容（与 repo_cleanup 对称）。"""
    if not repo_key:
        return []
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    base = f"{owner}/{repo}" if owner and repo else repo_key
    keys = [repo_key, f"{provider}:{base}", base]
    return list(dict.fromkeys(k for k in keys if k))

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
    from src.kernel.repo_key import parse_repo_key, to_repo_key
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

    from src.domains.mgmt import public as _mgmt_mig
    from src.domains.runtime import public as _rt_mig

    # 跨库事务不可得：按域拆成两段，各自在本域一个事务内完成。
    # 两段都是「删旧留新」语义 → 重复执行幂等；第二段失败时第一段已提交，调用方重试即收敛。
    try:
        _mg_stats = _mgmt_mig.migrate_repo_key_mgmt(repo_key, old_keys, new, new_owner, new_repo)
    except Exception as e:
        log.warning(f"Migrate repo_key mgmt segment failed (rolled back) {repo_key}->{new}: {e}")
        _audit(repo_key, new, operator_id, {"error": str(e), "rolled_back": True, "segment": "mgmt"})
        raise RepoMigrateError(f"migrate repo_key failed at mgmt segment: {e}") from e
    try:
        _rt_stats = _rt_mig.migrate_repo_key_runtime(old_keys, new)
    except Exception as e:
        # 第一段（mgmt）已提交 —— 如实记录，不做静默重试
        log.warning(f"Migrate repo_key runtime segment failed after mgmt committed "
                    f"{repo_key}->{new}: {e}")
        _audit(repo_key, new, operator_id,
               {"error": str(e), "mgmt_committed": True, "segment": "runtime"})
        raise RepoMigrateError(f"migrate repo_key failed at runtime segment: {e}") from e
    stats["tables"] = sum(_mg_stats.values()) + sum(_rt_stats.values())
    stats["by_table"] = {**_mg_stats, **_rt_stats}

    # 文件系统迁移（DB 提交后）
    stats["dirs_moved"] = _migrate_workspace_dir(repo_key, new)

    log.info(f"Migrate repo_key {repo_key}->{new} done: {stats}")
    _audit(repo_key, new, operator_id, stats)
    return stats

def _migrate_workspace_dir(old: str, new: str) -> int:
    """工作区知识基线目录物理迁移：old → new。"""
    from src.kernel.repo_key import workspace_repo_dir
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
        from src.domains.mgmt.auth import audit as auth_audit
        auth_audit.log_audit(
            "repo_migrate",
            user_id=operator_id,
            detail={"old_repo_key": old, "new_repo_key": new, **detail},
        )
    except Exception as e:
        log.warning(f"Migrate audit failed: {e}")
