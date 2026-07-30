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
    """删除单个 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM agent_sessions WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            db.commit()
            return c.rowcount > 0
        except Exception:
            return False


def ensure_default_agent_sessions(repo_key: str):
    """基于 agent-skill-mapping.json 的 default 场景，为仓库创建默认 Agent 会话"""
    import os
    _path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
    if not os.path.exists(_path):
        log.warning(f"ensure_default_agent_sessions: {_path} not found, skip")
        return

    with open(_path, "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
    _mapping = _cfg.get("scenes", {}).get("default", {}).get("mapping", {})
    if not _mapping:
        return

    _parts = repo_key.split("/")
    _owner, _rname = _parts[0], _parts[-1]
    from src.user.db import get_bot_token_by_repo
    _token = get_bot_token_by_repo(_owner, _rname)

    with db_lock:
        c = db.cursor()
        for _at, _sk in _mapping.items():
            _sid = str(uuid.uuid4())
            try:
                c.execute(
                    "INSERT OR REPLACE INTO agent_sessions (id, repo_key, agent_type, backend, skill, repo_gitcode_token, status, last_active_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (_sid, repo_key, _at, "opencode", _sk, _token, "idle")
                )
            except Exception as e:
                log.warning(f"Failed to create session for {_at}: {e}")
        db.commit()

    log.info(f"Auto-created agent sessions for repo={repo_key} from default scene")


def remove_agent_sessions(repo_key: str) -> int:
    """删除仓库的所有 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM agent_sessions WHERE repo_key=?", (repo_key,))
            db.commit()
            _cnt = c.rowcount
            if _cnt:
                log.info(f"Removed {_cnt} agent session(s) for repo={repo_key}")
            return _cnt
        except Exception as e:
            log.warning(f"Failed to remove sessions for {repo_key}: {e}")
            return 0
