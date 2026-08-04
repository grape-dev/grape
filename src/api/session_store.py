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
                "SELECT id, backend_session_id, status, last_active_at FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type)
            )
            row = c.fetchone()
        except Exception:
            row = None

        if row:
            return {
                "id": row[0],
                "backend_session_id": row[1] or "",
                "status": row[2] or "idle",
                "last_active_at": row[3] or "",
                "new": False,
                "repo_key": repo_key,
                "agent_type": agent_type,
            }

        sid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        backend_id = 0
        if backend:
            from src.user.db import ensure_default_backend, get_backend_id_by_name
            backend_id = get_backend_id_by_name(backend) or ensure_default_backend(backend) or 0
        try:
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, backend_id, status, last_active_at) VALUES (?,?,?,?,?,?)",
                (sid, repo_key, agent_type, backend_id, "idle", now)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to create agent session: {e}")
            return {"id": sid, "backend_session_id": "", "status": "idle",
                    "last_active_at": now, "new": True,
                    "repo_key": repo_key, "agent_type": agent_type}

        return {"id": sid, "backend_session_id": "", "status": "idle",
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
                    "UPDATE devops_agent_session SET status=?, backend_session_id=?, last_active_at=? WHERE id=?",
                    (status, oc_session_id, datetime.now().isoformat(), session_id)
                )
            else:
                c.execute(
                    "UPDATE devops_agent_session SET status=?, last_active_at=? WHERE id=?",
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
                "SELECT id, agent_type, backend_id, backend_session_id, status, last_active_at FROM devops_agent_session WHERE repo_key=? ORDER BY agent_type",
                (repo_key,)
            )
            result = []
            for r in c.fetchall():
                backend_name = ""
                if r[2]:
                    c.execute("SELECT name FROM backend_agents WHERE id=?", (r[2],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend_name = brow[0]
                result.append({"id": r[0], "agent_type": r[1], "backend": backend_name,
                               "backend_session_id": r[3] or "", "status": r[4],
                               "last_active_at": r[5] or ""})
            return result
        except Exception:
            return []


def delete_agent_session(repo_key: str, agent_type: str) -> bool:
    """删除单个 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
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

    _im_app_id = ""
    _im_app_secret = ""
    try:
        with db_lock:
            c2 = db.cursor()
            c2.execute("SELECT bot_app_id FROM global_im_bot_config WHERE bot_type='vod_bot' LIMIT 1")
            _row = c2.fetchone()
            if _row:
                _im_app_id = _row[0]
                # secret 从已存在的 devops_agent_session 取
                c2.execute("SELECT im_app_secret FROM devops_agent_session WHERE im_app_id=? AND im_app_secret!='' LIMIT 1", (_im_app_id,))
                _srow = c2.fetchone()
                if _srow and _srow[0]:
                    _im_app_secret = _srow[0]
    except Exception:
        pass

    with db_lock:
        c = db.cursor()
        from src.user.db import ensure_default_backend
        _default_backend_id = ensure_default_backend("opencode") or 0
        for _at, _sk in _mapping.items():
            _sid = str(uuid.uuid4())
            try:
                from src.user.db import resolve_skill_id
                _skill_id = resolve_skill_id(_sk)
                c.execute(
                    "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, backend_id, skill, skill_id, repo_gitcode_token, status, last_active_at, im_app_id, im_app_secret, im_provider) VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,'feishu')",
                    (_sid, repo_key, _at, _default_backend_id, _sk, _skill_id, _token, "idle",
                     _im_app_id if _at == "vod" else "", _im_app_secret if _at == "vod" else "")
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
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
            db.commit()
            _cnt = c.rowcount
            if _cnt:
                log.info(f"Removed {_cnt} agent session(s) for repo={repo_key}")
            return _cnt
        except Exception as e:
            log.warning(f"Failed to remove sessions for {repo_key}: {e}")
            return 0
