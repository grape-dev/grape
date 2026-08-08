"""Events 路由：事件列表、详情"""
from fastapi import APIRouter, HTTPException, Depends, Query
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/detail/{seq}")
async def get_event_detail(seq: int, user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, created_at, runtime, skill, exit_code, duration, agent_logs, started_at, completed_at, result FROM events WHERE seq=?",
                (seq,)
            )
            row = c.fetchone()
        except Exception:
            row = None

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到"})

    import json
    from src.repo_key import from_repo_key
    payload = {}
    try:
        payload = json.loads(row[9]) if row[9] else {}
    except Exception:
        pass

    agent_logs = []
    try:
        agent_logs = json.loads(row[16]) if row[16] else []
    except Exception:
        pass

    result = {
        "seq": row[0],
        "repo_key": from_repo_key(row[1] or ""),
        "issue_id": row[2],
        "event_type": row[3],
        "source": row[4],
        "agent_type": row[5] or "",
        "title": row[6] or row[3],
        "actor": row[7] or "",
        "summary": row[8] or row[6] or row[3],
        "payload": payload,
        "status": row[10],
        "created_at": row[11],
        "runtime": row[12] or "",
        "skill": row[13] or "",
        "exit_code": row[14],
        "duration": row[15],
        "agent_logs": agent_logs,
        "started_at": row[17] or "",
        "completed_at": row[18] or "",
        "result": row[19] or "",
    }
    # M4：附 event.json archive 对应 pipeline 段（结构化产出展示，见 §11.4）
    try:
        import re as _re
        num = ""
        if row[2]:
            m = _re.search(r"(\d+)", str(row[2]))
            if m:
                num = m.group(1)
        if num:
            from src.api.event_archive import load_event_json
            arch = load_event_json(row[1] or "", num)
            if arch:
                for seg in arch.get("pipeline", []):
                    if seg.get("seq") == row[0]:
                        result["archive"] = seg
                        break
                if "archive" not in result:
                    result["archive"] = arch
    except Exception:
        pass
    return result


@router.get("/{owner}/{repo}")
async def list_events(owner: str, repo: str,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(20, ge=1, le=100),
                      status: str = Query("", pattern="^(pending|running|completed|failed)?$"),
                      user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.session_manager import db, db_lock

    where_clauses = ["repo_key=?"]
    params = [repo_key]
    if status:
        where_clauses.append("status=?")
        params.append(status)

    where_sql = " AND ".join(where_clauses)

    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params)
            total = c.fetchone()[0]

            offset = (page - 1) * page_size
            c.execute(
                f"SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE {where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [page_size, offset]
            )
            events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": r[6]}
                for r in c.fetchall()
            ]
        except Exception:
            return {"events": [], "total": 0, "page": page, "page_size": page_size}

    return {"events": events, "total": total, "page": page, "page_size": page_size}
