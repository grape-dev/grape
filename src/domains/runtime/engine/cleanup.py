"""sweeper：Issue `done` 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）。

- 仅清理 `issue_state.stage='done'`（终态）的 Issue workdir，进行中绝不清理；
- 保留 event.json 档案（独立路径）与 issue_agent_session DB 行（审计）；
- 跳过有 dispatched/running 任务的会话；
- 对超 TTL 未活跃的僵尸 workdir 兜底清理；
- 对 chat / feishu / vod / history 目录做 TTL 清理（无活跃会话时删除）。
"""

import asyncio, os, shutil, time

from src.kernel import config

from src.kernel.config import log, CHAT_CWD_BASE

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import repogit

_SWEEP_INTERVAL = int(os.environ.get("SWEEPER_INTERVAL_SECONDS", "900"))  # 15min

_ZOMBIE_TTL_DAYS = int(os.environ.get("SWEEPER_ZOMBIE_TTL_DAYS", "3"))  # 非 done issue workdir 3 天不活跃清理

# Chat / feishu / vod 目录 TTL（默认 3 天未活跃清理，比 issue 的 7 天更短——这些是临时会话目录）
_SESSION_DIR_TTL_DAYS = int(os.environ.get("SWEEPER_SESSION_DIR_TTL_DAYS", "3"))

# 悬空 agent_tasks 回收 TTL（sweep_stuck_agent_tasks_once，单位分钟）：
# queued 超时无人认领 / running·dispatched 所属 runtime 已离线或不存在且 started_at 超时 /
# running·dispatched 超长未收敛（worker 侧任务超时 RUNTIME_TASK_TIMEOUT=1h，6h 仍运行必为孤儿）
_QUEUED_STALE_MINUTES = int(os.environ.get("SWEEPER_QUEUED_STALE_MINUTES", "180"))

_DEAD_RUNTIME_MINUTES = int(os.environ.get("SWEEPER_DEAD_RUNTIME_MINUTES", "30"))

def _long_running_minutes() -> int:
    """running·dispatched 孤儿回收阈值（分钟）。

    Issue #470：默认值随统一超时配置派生（timeout 分钟 × 6——worker 超时 1h 时
    6h 仍运行必为孤儿），不再写死 360；SWEEPER_LONG_RUNNING_MINUTES env 兜底
    优先级保留（运维显式调参 > 配置派生）。
    """
    _env = os.environ.get("SWEEPER_LONG_RUNNING_MINUTES", "").strip()
    if _env:
        try:
            v = int(float(_env))
            if v > 0:
                return v
        except Exception:
            pass
    from src.kernel.config import get_orphan_threshold_minutes
    return get_orphan_threshold_minutes()

_QUEUED_STALE_TXT = "中断：queued 超时无 worker 认领（sweeper 兜底回收）"

_DEAD_RUNTIME_TXT = "中断：running/dispatched 悬空，所属 runtime 已离线或不存在（sweeper 兜底回收）"

_LONG_RUNNING_TXT = "中断：running/dispatched 超长未收敛（sweeper 兜底回收，worker 任务超时阈值 1h）"

_DUAL_SIGNAL_TXT = "中断：双信号已齐（stream_ended+self_reported）但终态未收敛（sweeper 兜底回收）"

_GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")

def _done_issues() -> list[tuple[str, str]]:
    """返回到达终态（stage='done'）的 (repo_key, issue_id) 列表。Phase C3：读 issue 单表。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT repo_key, issue_number FROM issue WHERE stage='done'").fetchall()
        return [(r[0], f"#{r[1]}") for r in rows]

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


def _resolve_repo_git_token(repo_key: str) -> str:
    """sweeper 侧的仓库执行 token（Issue #2041）。

    未推送判定的远端事实复核（worktree_guard._remote_branch_head → `ls-remote`）
    需要鉴权；sweeper 不在任务上下文内，只能自行解析。解析失败/无 token → 空串，
    判定自动回退本地 ref 链（保守，不改变「宁可残留，不可丢改动」口径）。
    """
    if not repo_key:
        return ""
    try:
        from src.kernel.repo import Repo
        return Repo(repo_key).bot_token or ""
    except Exception as e:
        log.warning(f"sweeper git token resolve failed {repo_key}: {e}")
        return ""


d