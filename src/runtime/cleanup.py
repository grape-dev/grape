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
    """超 TTL 未活跃的 issue workdir。

    主结构：{owner}/{repo}/issues/{n}/{agent}/（按 Issue 统一隔离，见 workdir-issue-centric-design.md）。
    过渡期兼容旧结构 {owner}/{repo}/{agent}/issues/{n}/，新代码不再写入、仅扫描兜底清理。
    """
    out = []
    now = time.time()

    def _check(p: str):
        if not os.path.isdir(p):
            return
        try:
            mtime = os.path.getmtime(p)
        except Exception:
            return
        if now - mtime > ttl:
            out.append(p)

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
                # 新结构：{repo}/issues/{n}/{agent}/
                issues_root = os.path.join(repo_dir, "issues")
                if os.path.isdir(issues_root):
                    for num in os.listdir(issues_root):
                        issue_dir = os.path.join(issues_root, num)
                        if not os.path.isdir(issue_dir):
                            continue
                        for agent in _GIT_AGENTS:
                            _check(os.path.join(issue_dir, agent))
                # 过渡期旧结构：{repo}/{agent}/issues/{n}/（仅兜底，新代码不写入）
                for agent in _GIT_AGENTS:
                    issues = os.path.join(repo_dir, agent, "issues")
                    if not os.path.isdir(issues):
                        continue
                    for num in os.listdir(issues):
                        _check(os.path.join(issues, num))
    except Exception as e:
        log.warning(f"sweeper zombie scan failed: {e}")
    return out


def sweep_stuck_events_once(no_start_minutes: int = 10, stale_minutes: int = 30) -> int:
    """兜底回收：running 事件超时且无 agent_tasks 活跃任务支撑 → 置 failed（防卡 running 僵尸）。

    事件在 append_event 时即 running，若入队/启动失败或执行中断且未更新终态，
    事件永远 running（Agent 详情页显示"进行中"，但无任务/日志/结果）。两类僵尸：
    - started_at 为空（从未开始）且 created_at 超过 no_start_minutes → 入队/启动从未成功
    - started_at 非空但超过 stale_minutes 且无活跃任务 → 执行中断（任务记录已消失）
    有 queued/dispatched/running 任务支撑的 running 事件一律跳过（真在执行）。
    created_at 为 DB 本地时间（MySQL），started_at 为 UTC ISO 字符串，分开比较。
    """
    from datetime import datetime, timezone, timedelta
    fixed = 0
    try:
        now_local = datetime.now()
        no_start_cut = (now_local - timedelta(minutes=no_start_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        now_utc = datetime.now(timezone.utc)
        stale_cut = (now_utc - timedelta(minutes=stale_minutes)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq FROM events WHERE status='running' AND "
                "(started_at IS NULL OR started_at='') AND created_at < ?",
                (no_start_cut,))
            seqs = [r[0] for r in c.fetchall()]
            c.execute(
                "SELECT seq FROM events WHERE status='running' AND started_at != '' "
                "AND started_at IS NOT NULL AND started_at < ?",
                (stale_cut,))
            seqs += [r[0] for r in c.fetchall()]
        seqs = list(dict.fromkeys(seqs))
        if not seqs:
            return 0
        ph = ",".join("?" * len(seqs))
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT DISTINCT event_seq FROM agent_tasks WHERE event_seq IN ({ph}) "
                "AND status IN ('queued','dispatched','running')",
                seqs)
            live = {r[0] for r in c.fetchall()}
        dead = [s for s in seqs if s not in live]
        if dead:
            ph2 = ",".join("?" * len(dead))
            now_iso = now_utc.isoformat()
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"UPDATE events SET status='failed', exit_code=1, completed_at=?, "
                    f"result='中断：running 超时无任务执行记录（sweeper 兜底回收）' WHERE seq IN ({ph2})",
                    (now_iso, *dead))
                fixed = c.rowcount
                db.commit()
            if fixed:
                log.info(f"sweeper: recovered {fixed} stuck running event(s): {dead}")
            # 取消对应的线程池排队任务（防止 event 已 failed 但子进程还在跑）
            try:
                from src.api.event_pipeline import cancel_direct_task
                for _seq in dead:
                    cancel_direct_task(_seq)
            except Exception as e:
                log.warning(f"sweeper: cancel_direct_task failed: {e}")
    except Exception as e:
        log.warning(f"sweeper event recovery failed: {e}")
    return fixed


def sweep_once() -> int:
    cleaned = 0
    try:
        # 兜底回收卡 running 僵尸事件（入队/执行失败未更新终态的历史遗留）
        try:
            cleaned += sweep_stuck_events_once()
        except Exception as e:
            log.warning(f"sweeper stuck-event sweep failed: {e}")
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


_POLL_INTERVAL = int(os.environ.get("GRAPE_POLL_INTERVAL_SECONDS", "60"))


def _poll_gitcode_issues_once() -> int:
    """Poll GitCode for new issues on managed repos (webhook fallback).

    For each managed repo with a bot token, fetch recent open issues via GitCode API.
    If an issue has no corresponding event in DB (no issue_opened event),
    inject a synthetic webhook to trigger the pipeline.
    """
    import json as _json
    import urllib.request as _urllib
    try:
        from src.session_manager import db, db_lock
        from src.user.db import get_gitcode_token
        from src.repo_key import to_repo_key
    except Exception:
        return 0

    triggered = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, gitcode_webhook_token FROM managed_repos WHERE bot_gitcode_token != '' OR gitcode_webhook_token != ''")
            repos = c.fetchall()
        if not repos:
            return 0

        for repo_key, webhook_token in repos:
            try:
                owner, repo_name = repo_key.split(":")[-1].split("/")
                tok = get_gitcode_token(repo_key=repo_key) or ""
                if not tok:
                    continue
                # Fetch recent open issues (last 20)
                req = _urllib.Request(
                    f"https://api.gitcode.com/api/v5/repos/{owner}/{repo_name}/issues?state=open&per_page=20&direction=desc",
                    headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
                resp = _urllib.urlopen(req, timeout=15)
                issues = _json.loads(resp.read())
                if not issues:
                    continue

                for issue in issues:
                    num = issue.get("number")
                    if not num:
                        continue
                    issue_id = f"#{num}"

                    # Check if this issue already has an event
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT 1 FROM events WHERE repo_key=? AND issue_id=? LIMIT 1", (repo_key, issue_id))
                        if c.fetchone():
                            continue  # already has events

                    # New issue without events — inject synthetic webhook
                    title = issue.get("title", "")
                    body = issue.get("body", "")
                    log.info(f"poll: new issue {issue_id} on {repo_key}: {title[:60]}, injecting webhook")

                    # Call webhook endpoint internally
                    payload = _json.dumps({
                        "action": "open",
                        "issue": {"number": num, "title": title, "body": body[:2000]},
                        "repository": {"full_name": f"{owner}/{repo_name}", "name": repo_name}
                    }).encode()

                    webhook_req = _urllib.Request(
                        f"http://127.0.0.1:38080/webhook/gitcode",
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitCode-Token": webhook_token or "poll",
                            "X-Gitcode-Event": "issue_opened",
                        },
                        method="POST")
                    try:
                        wh_resp = _urllib.urlopen(webhook_req, timeout=15)
                        result = _json.loads(wh_resp.read())
                        if result.get("seq"):
                            triggered += 1
                            log.info(f"poll: injected issue {issue_id} -> seq={result.get('seq')}")
                    except Exception as e:
                        log.warning(f"poll: inject failed for {issue_id}: {e}")

            except Exception as e:
                log.warning(f"poll: error checking {repo_key}: {e}")

        if triggered:
            log.info(f"poll: triggered {triggered} new issue(s)")
    except Exception as e:
        log.warning(f"poll_gitcode_issues failed: {e}")
    return triggered


async def sweep_loop():
    """服务端后台周期清理 + GitCode issue 轮询（main.py lifespan 挂载）。"""
    while True:
        try:
            sweep_once()
        except Exception as e:
            log.warning(f"sweeper loop error: {e}")
        try:
            _poll_gitcode_issues_once()
        except Exception as e:
            log.warning(f"poll loop error: {e}")
        await asyncio.sleep(_SWEEP_INTERVAL)
