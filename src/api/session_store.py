"""Agent Session 管理 — per (repo_key, agent_type) 独立会话"""
import uuid, json
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log


def get_or_create_agent_session(repo_key: str, agent_type: str,
                                 backend: str = "opencode") -> dict:
    """获取或创建 Agent 会话，每个 (repo_key, agent_type) 独立"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, oc_session_id, status, last_active_at FROM agent_sessions WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type)
            )
            row = c.fetchone()
        except Exception:
            row = None

        if row:
            return {
                "id": row[0],
                "oc_session_id": row[1] or "",
                "status": row[2] or "idle",
                "last_active_at": row[3] or "",
                "new": False,
                "repo_key": repo_key,
                "agent_type": agent_type,
            }

        sid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        try:
            c.execute(
                "INSERT INTO agent_sessions (id, repo_key, agent_type, backend, status, last_active_at) VALUES (?,?,?,?,?,?)",
                (sid, repo_key, agent_type, backend, "idle", now)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to create agent session: {e}")
            return {"id": sid, "oc_session_id": "", "status": "idle",
                    "last_active_at": now, "new": True,
                    "repo_key": repo_key, "agent_type": agent_type}

        return {"id": sid, "oc_session_id": "", "status": "idle",
                "last_active_at": now, "new": True,
                "repo_key": repo_key, "agent_type": agent_type}


def update_agent_session(session_id: str, status: str = "idle",
                         oc_session_id: str = None):
    """更新 Agent 会话状态"""
    with db_lock:
        c = db.cursor()
        try:
            if oc_session_id:
                c.execute(
                    "UPDATE agent_sessions SET status=?, oc_session_id=?, last_active_at=? WHERE id=?",
                    (status, oc_session_id, datetime.now().isoformat(), session_id)
                )
            else:
                c.execute(
                    "UPDATE agent_sessions SET status=?, last_active_at=? WHERE id=?",
                    (status, datetime.now().isoformat(), session_id)
                )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update session {session_id}: {e}")


def list_agent_sessions(repo_key: str) -> list[dict]:
    """列出仓库的所有 Agent 会话"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, agent_type, backend, oc_session_id, status, last_active_at FROM agent_sessions WHERE repo_key=? ORDER BY agent_type",
                (repo_key,)
            )
            return [
                {"id": r[0], "agent_type": r[1], "backend": r[2],
                 "oc_session_id": r[3] or "", "status": r[4],
                 "last_active_at": r[5] or ""}
                for r in c.fetchall()
            ]
        except Exception:
            return []


def delete_agent_session(repo_key: str, agent_type: str) -> bool:
    """删除 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM agent_sessions WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            db.commit()
            return c.rowcount > 0
        except Exception:
            return False
