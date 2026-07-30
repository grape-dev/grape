"""Event Store — 事件记录与查询（shared events table）"""
import json, threading
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log

# ====== Per-repo 锁（从旧 event_processor 迁移）======
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def acquire_repo_lock(repo_key: str) -> threading.Lock:
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def append_event(repo_key: str, event_type: str, title: str = "",
                 issue_id: str = "", agent_type: str = "", source: str = "gitcode",
                 actor: str = "", summary: str = "",
                 payload: dict = None, status: str = "pending") -> int:
    """写入事件到 events 表，返回 seq"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (repo_key, issue_id, event_type, source, agent_type, title, actor, summary,
                 json.dumps(payload or {}), status)
            )
            db.commit()
            seq = c.lastrowid
            payload_keys = list((payload or {}).keys())
            log.info(f"Event stored: seq={seq}, repo={repo_key}, type={event_type}, "
                     f"agent={agent_type}, issue={issue_id}, actor={actor}, source={source}, "
                     f"status={status}, summary={summary[:60]}, payload_keys={payload_keys}")
            return seq
        except Exception as e:
            log.warning(f"Event store FAILED: seq=0, repo={repo_key}, type={event_type}, "
                        f"agent={agent_type}, error={e}")
            return 0


def update_event_status(seq: int, status: str):
    """更新事件状态"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET status=? WHERE seq=?", (status, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event {seq}: {e}")


def update_event_config(seq: int, backend: str, skill: str, started_at: str):
    """写入执行配置（backend, skill, started_at）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET backend=?, skill=?, started_at=? WHERE seq=?",
                      (backend, skill, started_at, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event config {seq}: {e}")


def update_event_complete(seq: int, status: str, exit_code: int = None,
                          duration: float = None, agent_logs: list = None,
                          completed_at: str = ""):
    """写入执行结果（status, exit_code, duration, agent_logs, completed_at）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute(
                "UPDATE events SET status=?, exit_code=?, duration=?, agent_logs=?, completed_at=? WHERE seq=?",
                (status, exit_code, duration, json.dumps(agent_logs or []), completed_at, seq)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event complete {seq}: {e}")


def get_events(repo_key: str, agent_type: str = "", issue_id: str = "",
               status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    """查询事件列表，支持按 agent/issue/status 过滤"""
    clauses = ["repo_key=?"]
    params = [repo_key]

    if agent_type:
        clauses.append("agent_type=?")
        params.append(agent_type)
    if issue_id:
        clauses.append("issue_id=?")
        params.append(issue_id)
    if status:
        clauses.append("status=?")
        params.append(status)

    where = " AND ".join(clauses)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, backend, skill, exit_code, duration, started_at, completed_at FROM events WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            return [
                {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "",
                 "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
                 "status": r[7], "created_at": r[8],
                 "backend": r[9] or "", "skill": r[10] or "",
                 "exit_code": r[11], "duration": r[12],
                 "started_at": r[13] or "", "completed_at": r[14] or ""}
                for r in c.fetchall()
            ]
        except Exception:
            return []


def get_event_count(repo_key: str, status: str = "") -> int:
    """统计事件数量"""
    clauses = ["repo_key=?"]
    params = [repo_key]
    if status:
        clauses.append("status=?")
        params.append(status)
    with db_lock:
        try:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM events WHERE {' AND '.join(clauses)}", params)
            return c.fetchone()[0]
        except Exception:
            return 0


def get_events_grouped_by_issue(repo_key: str) -> dict[str, list[dict]]:
    """按 issue_id 聚类事件，返回 {issue_id: [events]}"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
            )
            rows = c.fetchall()
        except Exception:
            return {}

    grouped = {}
    for r in rows:
        iid = r[1]
        if iid not in grouped:
            grouped[iid] = []
        grouped[iid].append({
            "seq": r[0], "event_type": r[2], "agent_type": r[3] or "",
            "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
            "status": r[7], "created_at": r[8],
        })
    return grouped


def get_issue_summary(repo_key: str, max_issues: int = 10) -> str:
    """按 Issue 聚类生成事件摘要文本，用于 Prompt 注入"""
    grouped = get_events_grouped_by_issue(repo_key)
    if not grouped:
        return ""

    lines = []
    sorted_issues = sorted(grouped.keys(), key=lambda iid: grouped[iid][-1]["created_at"], reverse=True)[:max_issues]

    for iid in sorted_issues:
        events = grouped[iid]
        agent_flow = []
        for ev in events:
            agent = ev["agent_type"] or "?"
            action = ev["event_type"]
            status_icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(ev["status"], "?")
            agent_flow.append(f"{agent}:{action}{status_icon}")

        title = events[0]["title"] or iid
        lines.append(f"  {iid} {title} — {' → '.join(agent_flow)}")

    return "Issue Pipeline:\n" + "\n".join(lines)
