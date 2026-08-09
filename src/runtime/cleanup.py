"""sweeper：Issue `done` 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）。

- 仅清理 `issue_state.stage='done'`（终态）的 Issue workdir，进行中绝不清理；
- 保留 event.json 档案（独立路径）与 issue_agent_session DB 行（审计）；
- 跳过有 dispatched/running 任务的会话；
- 对超 TTL 未活跃的僵尸 workdir 兜底清理。
"""
import asyncio, os, shutil, time
from src import config
from src.config import log
from src.session_manager import db, db_lock

_SWEEP_INTERVAL = int(os.environ.get("SWEEPER_INTERVAL_SECONDS", "900"))  # 15min
_ZOMBIE_TTL_DAYS = int(os.environ.get("SWEEPER_ZOMBIE_TTL_DAYS", "7"))

_GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")


def _done_issues() -> list[tuple[str, str]]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_id FROM issue_state WHERE stage='done'")
        return c.fetchall()


def _active_issue_sessions() -> set[tuple[str, str, str]]:
    """有 dispatched/running 任务的 (repo_key, agent_type, issue_num) 集合（不清理）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT DISTINCT issue_session_id FROM agent_tasks WHERE status IN ('dispatched','running') AND issue_session_id!=''")
        ids = [r[0] for r in c.fetchall()]
    if not ids:
        return set()
    ph = ",".join("?" for _ in ids)
    active = set()
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT repo_key, agent_type, issue_id FROM issue_agent_session WHERE id IN ({ph})", ids)
        for r in c.fetchall():
            active.add((r[0], r[1], str(r[2]).lstrip("#")))
    return active


def _zombie_issue_dirs(ttl: float) -> list[str]:
    """超 TTL 未活跃的 issue workdir（仅限 {owner}/{repo}/{agent}/{issues}/{n} 结构）。"""
    out = []
    now = time.time()
    try:
        owners = os.listdir(config.WORKSPACE_DIR) if os.path.isdir(config.WORKSPACE_DIR) else []
        for owner in owners:
            owner_dir = os.path.join(config.WORKSPACE_DIR, owner)
            if not os.path.isdir(owner_dir):
                continue
            for rname in os.listdir(owner_dir):
                repo_dir = os.path.join(owner_dir, rname)
                if not os.path.isdir(repo_dir):
                    continue
                for agent in _GIT_AGENTS:
                    issues = os.path.join(repo_dir, agent, "issues")
                    if not os.path.isdir(issues):
                        continue
                    for num in os.listdir(issues):
                        p = os.path.join(issues, num)
                        if not os.path.isdir(p):
                            continue
                        try:
                            mtime = os.path.getmtime(p)
                        except Exception:
                            continue
                        if now - mtime > ttl:
                            out.append(p)
    except Exception as e:
        log.warning(f"sweeper zombie scan failed: {e}")
    return out


def sweep_once() -> int:
    cleaned = 0
    try:
        from src.repo_key import split_owner_repo
        from src.runtime import repogit
        active = _active_issue_sessions()
        for repo_key, issue_id in _done_issues():
            num = str(issue_id or "").lstrip("#")
            if not num or num == "0":
                continue
            for agent_type in _GIT_AGENTS:
                if (repo_key, agent_type, num) in active:
                    continue
                issue_dir = repogit.issue_dir_for(repo_key, agent_type, num)
                if not os.path.isdir(issue_dir):
                    continue
                try:
                    repogit.cleanup_issue_worktree(repo_key, agent_type, num)
                    cleaned += 1
                except Exception as e:
                    log.warning(f"sweeper cleanup failed {issue_dir}: {e}")
        # 僵尸兜底
        ttl = _ZOMBIE_TTL_DAYS * 86400
        for p in _zombie_issue_dirs(ttl):
            try:
                shutil.rmtree(p, ignore_errors=True)
                cleaned += 1
            except Exception as e:
                log.warning(f"sweeper zombie cleanup failed {p}: {e}")
        if cleaned:
            log.info(f"sweeper cleaned {cleaned} issue workdir(s)")
        return cleaned
    except Exception as e:
        log.warning(f"sweep_once failed: {e}")
        return 0


async def sweep_loop():
    """服务端后台周期清理（main.py lifespan 挂载）。"""
    while True:
        try:
            sweep_once()
        except Exception as e:
            log.warning(f"sweeper loop error: {e}")
        await asyncio.sleep(_SWEEP_INTERVAL)
