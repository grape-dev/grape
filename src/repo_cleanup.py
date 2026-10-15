"""仓库级联清理（Issue #340）。

解除托管（平台侧手动解除 / 代码仓侧删除仓库）时，以 repo_key 为索引，
事务化清空该仓库在平台内的全部关联数据：

- 需求中心：issue / issue_state / issue_agent_session
- 事件流水线：events / event_skipped / event_thread / event_route_config
  / agent_tasks / task_messages
- 会话：chat_conversations / chat_messages / chat_agent_session
  / devops_agent_session / vod_conversations
- 知识：agent_shared_knowledge / scene_knowledge_ref + workspace 知识基线文件
- 场景关联：devops_pipeline_scene
- 技能：skill(repo scope) + skills/repo/{repo_key} 目录
- IM 绑定：repo_im_group_binding
- onboarding 幂等标记：meta

设计（docs/issue-340/plan.md）：
- DB 清理封装为单事务，失败 rollback + 抛 RepoCleanupError（可重试，无半清理）；
- 文件系统清理在 DB 提交后执行，meta 表 pending 标记 + retry_pending_file_cleanups 补偿；
- 全部清理动作写 audit_logs 审计日志（哈希链）。
"""
from __future__ import annotations

import os
import re
import shutil

from src.config import log


class RepoCleanupError(Exception):
    """级联清理失败（DB 事务已回滚，可安全重试）。"""


# 以 repo_key 直删的表（依赖序：先删被引用方的子表，见 _DB_DELETE_STEPS）
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


def _normalize_keys(repo_key: str) -> list[str]:
    """repo_key 带/不带 gitcode: 前缀双写兼容。"""
    if not repo_key:
        return []
    keys = [repo_key]
    if not repo_key.startswith("gitcode:"):
        keys.append(f"gitcode:{repo_key}")
    else:
        keys.append(repo_key[len("gitcode:"):])
    return list(dict.fromkeys(k for k in keys if k))


def _existing_tables(c) -> set[str]:
    """探测当前库中实际存在的表（老库可能缺新表，跳过不中断事务）。"""
    try:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    except Exception:
        # MySQL 兼容层
        try:
            rows = c.execute("SHOW TABLES").fetchall()
            return {list(r)[-1] for r in rows}
        except Exception:
            return set()


def cascade_cleanup_repo_data(repo_key: str, owner: str, repo: str,
                              trigger: str = "platform",
                              operator_id: int = 0,
                              dry_run: bool = False) -> dict:
    """级联清空仓库全部关联数据（Issue #340）。

    Args:
        repo_key: owner/repo（可带 provider 前缀，内部双写兼容）
        owner/repo: 用于文件系统路径（含路径安全校验）
        trigger: platform | webhook | polling | legacy（写入审计日志）
        operator_id: 触发用户 ID（审计用）
        dry_run: 仅统计不删除

    Returns:
        {"tables": 删除行数, "skill_dirs": 0, "workspace_dirs": 0,
         "dry_run": bool, "trigger": str}

    Raises:
        RepoCleanupError: DB 事务失败（已回滚，可重试）
    """
    keys = _normalize_keys(repo_key)
    stats = {"tables": 0, "skill_dirs": 0, "workspace_dirs": 0,
             "dry_run": bool(dry_run), "trigger": trigger}
    if not keys:
        return stats

    # 路径安全校验：owner/repo 仅允许字母数字、点、下划线、连字符（防路径穿越）
    if owner and repo and not (re.match(r'^[\w.\-]+$', owner) and re.match(r'^[\w.\-]+$', repo)):
        log.warning(f"Cleanup skipped: invalid owner/repo {owner}/{repo}")
        return stats

    from src.session_manager import db, db_lock

    placeholders = ",".join("?" for _ in keys)
    with db_lock:
        c = db.cursor()
        existing = _existing_tables(c)
        try:
            if dry_run:
                for tbl in _REPO_KEY_TABLES:
                    if tbl in existing:
                        c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE repo_key IN ({placeholders})", keys)
                        stats["tables"] += c.fetchone()[0]
                db.commit()
                log.info(f"Cascade cleanup dry-run {repo_key} (trigger={trigger}): {stats}")
                return stats

            # ---- 依赖子表先删（无 repo_key 列，经父表 id 关联）----
            # task_messages ← agent_tasks.id
            if "agent_tasks" in existing and "task_messages" in existing:
                c.execute(f"SELECT id FROM agent_tasks WHERE repo_key IN ({placeholders})", keys)
                task_ids = [r[0] for r in c.fetchall()]
                if task_ids:
                    _ph = ",".join("?" for _ in task_ids)
                    c.execute(f"DELETE FROM task_messages WHERE task_id IN ({_ph})", task_ids)
                    stats["tables"] += c.rowcount
            # scene_knowledge_ref ← agent_shared_knowledge.id
            if "agent_shared_knowledge" in existing and "scene_knowledge_ref" in existing:
                c.execute(f"SELECT id FROM agent_shared_knowledge WHERE repo_key IN ({placeholders})", keys)
                kn_ids = [r[0] for r in c.fetchall()]
                if kn_ids:
                    _ph = ",".join("?" for _ in kn_ids)
                    c.execute(f"DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ({_ph})", kn_ids)
                    stats["tables"] += c.rowcount
            # vod_conversations ← devops_agent_session.id
            if "devops_agent_session" in existing and "vod_conversations" in existing:
                c.execute(f"DELETE FROM vod_conversations WHERE agent_session_id IN "
                          f"(SELECT id FROM devops_agent_session WHERE repo_key IN ({placeholders}))", keys)
                stats["tables"] += c.rowcount
            # chat_messages ← chat_conversations.id
            if "chat_conversations" in existing and "chat_messages" in existing:
                c.execute(f"SELECT id FROM chat_conversations WHERE repo_key IN ({placeholders})", keys)
                conv_ids = [r[0] for r in c.fetchall()]
                if conv_ids:
                    _ph = ",".join("?" for _ in conv_ids)
                    c.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({_ph})", conv_ids)
                    stats["tables"] += c.rowcount

            # ---- 按 repo_key 直删 ----
            for tbl in _REPO_KEY_TABLES:
                if tbl not in existing:
                    continue
                c.execute(f"DELETE FROM {tbl} WHERE repo_key IN ({placeholders})", keys)
                stats["tables"] += c.rowcount

            # 仓库 skill 记录（scope='repo'，owner_id 存 repo_key）
            if "skill" in existing:
                c.execute(f"DELETE FROM skill WHERE scope='repo' AND owner_id IN ({placeholders})", keys)
                stats["tables"] += c.rowcount
            # onboarding 幂等标记（重建同名仓库应重新引导）
            if "meta" in existing:
                _ob_keys = [f"onboarded:{k}" for k in keys]
                c.execute(f"DELETE FROM meta WHERE `key` IN ({','.join('?' for _ in _ob_keys)})", _ob_keys)
                stats["tables"] += c.rowcount

            db.commit()
        except Exception as e:
            db.rollback()
            log.warning(f"Cascade cleanup DB transaction failed (rolled back) {repo_key}: {e}")
            _audit(repo_key, trigger, operator_id, {"error": str(e), "rolled_back": True})
            raise RepoCleanupError(f"cascade cleanup failed: {e}") from e

    # ---- 文件系统清理（DB 提交后，补偿机制）----
    if not dry_run:
        _cleanup_files(keys, owner, repo, stats)

    log.info(f"Cascade cleanup {repo_key} (trigger={trigger}): {stats}")
    _audit(repo_key, trigger, operator_id, stats)
    return stats


def _audit(repo_key: str, trigger: str, operator_id: int, detail: dict) -> None:
    try:
        from src.auth import audit as auth_audit
        auth_audit.log_audit(
            "repo_cascade_cleanup",
            user_id=operator_id,
            detail={"repo_key": repo_key, "trigger": trigger, **detail},
        )
    except Exception as e:
        log.warning(f"Cascade cleanup audit failed: {e}")


def _cleanup_files(keys: list[str], owner: str, repo: str, stats: dict) -> None:
    """skill 目录 + workspace 知识基线目录清理（带 meta pending 补偿标记）。"""
    from src.session_manager import db, db_lock

    def _mark_pending(k: str, on: bool):
        try:
            with db_lock:
                c = db.cursor()
                if on:
                    c.execute("INSERT OR REPLACE INTO meta (`key`, value) VALUES (?, ?)",
                              (f"cleanup_pending:{k}", "1"))
                else:
                    c.execute("DELETE FROM meta WHERE `key`=?", (f"cleanup_pending:{k}",))
                db.commit()
        except Exception as e:
            log.warning(f"Cleanup pending mark failed ({k}): {e}")

    # 1. 仓库 skill 目录（skills/repo/{repo_key}/）
    try:
        from src.user.db import SKILL_DIR
        _repo_skills_base = os.path.join(os.path.dirname(SKILL_DIR), "repo")
        for k in keys:
            _dir = os.path.join(_repo_skills_base, k)
            if os.path.isdir(_dir):
                _mark_pending(k, True)
                try:
                    shutil.rmtree(_dir)
                    stats["skill_dirs"] += 1
                    _mark_pending(k, False)
                except Exception as e:
                    log.warning(f"Cleanup skill dir failed ({k}): {e}")
    except Exception as e:
        log.warning(f"Cleanup skill dir failed: {e}")

    if not (owner and repo):
        return

    # 2. workspace 知识基线目录（data/workspaces/...，新旧路径兼容）
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import parse_repo_key, workspace_repo_dir
        _ws_base = WORKSPACE_DIR
        _provider, _, _ = parse_repo_key(keys[0]) if keys else ("gitcode", "", "")
        _provider = _provider or "gitcode"
        candidates = [
            workspace_repo_dir(keys[0]) if keys else "",
            os.path.join(_ws_base, owner, repo),
            os.path.join(_ws_base, repo),
            os.path.join(_ws_base, "feishu", f"{owner}_{repo}"),
            os.path.join(_ws_base, "vod", f"{owner}_{repo}"),
            os.path.join(_ws_base, "vod", _provider, f"{owner}_{repo}"),
            os.path.join(_ws_base, "history", owner, repo),
            os.path.join(_ws_base, "history", f"gitcode:{owner}", repo),
            os.path.join(_ws_base, "history", owner, _provider, f"{owner}_{repo}"),
        ]
        _pending_key = keys[0]
        _pending_on = False
        for _dir in candidates:
            _abs = os.path.normpath(_dir)
            if os.path.isdir(_abs) and _abs.startswith(os.path.normpath(_ws_base) + os.sep):
                if not _pending_on:
                    _mark_pending(_pending_key, True)
                    _pending_on = True
                try:
                    shutil.rmtree(_abs)
                    stats["workspace_dirs"] += 1
                except Exception as e:
                    log.warning(f"Cleanup workspace dir failed ({_abs}): {e}")
        if _pending_on:
            # 候选路径全部删除完成 → 清除 pending 标记；
            # 仍有目录残留（删除失败）→ 保留 pending 标记供 retry_pending_file_cleanups 重试。
            _still = any(os.path.isdir(os.path.normpath(d)) for d in candidates
                         if os.path.normpath(d).startswith(os.path.normpath(_ws_base) + os.sep))
            _mark_pending(_pending_key, bool(_still))
    except Exception as e:
        log.warning(f"Cleanup workspace dir failed: {e}")


def retry_pending_file_cleanups() -> int:
    """重试历史失败的文件清理（meta 表 cleanup_pending:* 标记）。

    Returns:
        仍存在的 pending 标记数（0 = 全部清理完成）
    """
    from src.session_manager import db, db_lock
    pending = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT `key` FROM meta WHERE `key` LIKE 'cleanup_pending:%'")
            pending = [r[0] for r in c.fetchall()]
    except Exception as e:
        log.warning(f"Retry pending cleanups: read meta failed: {e}")
        return -1

    from src.user.db import SKILL_DIR
    from src.config import WORKSPACE_DIR
    from src.repo_key import parse_repo_key, split_owner_repo, workspace_repo_dir
    _repo_skills_base = os.path.join(os.path.dirname(SKILL_DIR), "repo")

    remaining = 0
    for key in pending:
        rk = key[len("cleanup_pending:"):]
        provider, owner, repo_name = parse_repo_key(rk)
        if not (owner and repo_name):
            owner, repo_name = split_owner_repo(rk)
        done = True
        # skill 目录
        _dir = os.path.join(_repo_skills_base, rk)
        if os.path.isdir(_dir):
            try:
                shutil.rmtree(_dir)
            except Exception:
                done = False
        # workspace 目录
        if owner and repo_name:
            for cand in (workspace_repo_dir(rk),
                         os.path.join(WORKSPACE_DIR, owner, repo_name),
                         os.path.join(WORKSPACE_DIR, repo_name)):
                _abs = os.path.normpath(cand)
                if os.path.isdir(_abs) and _abs.startswith(os.path.normpath(WORKSPACE_DIR) + os.sep):
                    try:
                        shutil.rmtree(_abs)
                    except Exception:
                        done = False
        if done:
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("DELETE FROM meta WHERE `key`=?", (key,))
                    db.commit()
            except Exception:
                pass
        else:
            remaining += 1
    return remaining
