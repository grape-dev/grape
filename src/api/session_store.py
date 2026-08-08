"""Agent Session 管理 — per (repo_key, agent_type) 独立会话"""
import uuid, json
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log


def get_agent_type_enable(agent_type: str) -> int:
    """读 agent_type.enable（全局默认，sre 默认关）。无记录返回 1（默认开）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT enable FROM agent_type WHERE agent_type=?", (agent_type,))
            row = c.fetchone()
            if row and row[0] is not None:
                return int(row[0])
    except Exception:
        pass
    return 1


def is_agent_enabled(repo_key: str, agent_type: str) -> bool:
    """仓库级 Agent 是否启用：优先 devops_agent_session.enable，无会话回退 agent_type.enable。

    见 agent-enable-toggle-design.md §五 拦截语义。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_type))
            row = c.fetchone()
            if row and row[0] is not None:
                return bool(int(row[0]))
    except Exception:
        pass
    return bool(get_agent_type_enable(agent_type))


def get_or_create_agent_session(repo_key: str, agent_type: str,
                                 backend: str = "opencode") -> dict:
    """获取或创建 Agent 会话，每个 (repo_key, agent_type) 独立"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type)
            )
            row = c.fetchone()
        except Exception:
            row = None

        if row:
            return {
                "id": row[0],
                "runtime_session_id": row[1] or "",
                "status": row[2] or "idle",
                "last_active_at": row[3] or "",
                "enable": bool(row[4]) if row[4] is not None else True,
                "new": False,
                "repo_key": repo_key,
                "agent_type": agent_type,
            }

        sid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        runtime_id = 0
        if backend:
            from src.user.db import ensure_default_backend, get_runtime_id_by_name
            runtime_id = get_runtime_id_by_name(backend) or ensure_default_backend(backend) or 0
        _enable = get_agent_type_enable(agent_type)
        try:
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, last_active_at, enable) VALUES (?,?,?,?,?,?,?)",
                (sid, repo_key, agent_type, runtime_id, "idle", now, _enable)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to create agent session: {e}")
            return {"id": sid, "runtime_session_id": "", "status": "idle",
                    "last_active_at": now, "enable": bool(_enable), "new": True,
                    "repo_key": repo_key, "agent_type": agent_type}

        return {"id": sid, "runtime_session_id": "", "status": "idle",
                "last_active_at": now, "enable": bool(_enable), "new": True,
                "repo_key": repo_key, "agent_type": agent_type}


def update_agent_session(session_id: str, status: str = "idle",
                         oc_session_id: str = None):
    """更新 Agent 会话状态"""
    with db_lock:
        c = db.cursor()
        try:
            if oc_session_id:
                c.execute(
                    "UPDATE devops_agent_session SET status=?, runtime_session_id=?, last_active_at=? WHERE id=?",
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
                "SELECT id, agent_type, runtime_id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? ORDER BY agent_type",
                (repo_key,)
            )
            result = []
            for r in c.fetchall():
                backend_name = ""
                if r[2]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (r[2],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend_name = brow[0]
                result.append({"id": r[0], "agent_type": r[1], "backend": backend_name,
                               "runtime_session_id": r[3] or "", "status": r[4],
                               "last_active_at": r[5] or "",
                               "enable": bool(r[6]) if r[6] is not None else True})
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

    from src.repo_key import split_owner_repo
    _owner, _rname = split_owner_repo(repo_key)
    from src.user.db import get_bot_token_by_repo
    _token = get_bot_token_by_repo(_owner, _rname)

    # VOD IM 配置：不自动注入。VOD 助手无全局 bot 概念，同一 vod bot 由用户在多个 repo 中显式配置。
    _im_app_id = ""
    _im_app_secret = ""

    with db_lock:
        c = db.cursor()
        from src.user.db import ensure_default_backend
        _default_runtime_id = ensure_default_backend("opencode") or 0
        for _at, _sk in _mapping.items():
            _sid = str(uuid.uuid4())
            try:
                from src.user.db import resolve_skill_id
                _skill_id = resolve_skill_id(_sk)
                _enable = get_agent_type_enable(_at)
                c.execute(
                    "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token, status, last_active_at, im_app_id, im_app_secret, im_provider, enable) VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?)",
                    (_sid, repo_key, _at, _default_runtime_id, _sk, _skill_id, _token, "idle",
                     _im_app_id if _at == "vod" else "", _im_app_secret if _at == "vod" else "",
                     "feishu", _enable)
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
