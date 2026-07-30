"""Agent 路由：详情、配置、类型元数据"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user, get_bot_token

router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENT_TYPES = ["vod", "issue", "coding", "qa", "sre"]


def _get_agent_type_meta(agent_type: str) -> dict | None:
    """从 agent_type_config 表读取 Agent 元数据"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT agent_type, display_name, description, default_backend, icon, order_num FROM agent_type_config WHERE agent_type=?",
                (agent_type,)
            )
            row = c.fetchone()
            if row:
                return {"agent_type": row[0], "display_name": row[1], "description": row[2],
                        "default_backend": row[3] or "opencode", "icon": row[4] or "", "order_num": row[5] or 0}
        except Exception:
            pass
    return None


@router.get("/types")
async def list_agent_types():
    """返回所有 Agent 类型的元数据（显示名、图标、描述、颜色等）"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT agent_type, display_name, description, default_backend, icon, order_num FROM agent_type_config ORDER BY order_num, agent_type"
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    COLORS = {"vod": "var(--pk)", "issue": "var(--r)", "coding": "var(--a)", "qa": "var(--pu)", "sre": "var(--or)"}
    BGS = {"vod": "#fde8f0", "issue": "#fce5e5", "coding": "#e3edfd", "qa": "#f3e9fe", "sre": "#fef0e4"}

    return {"types": [{
        "agent_type": r[0], "display_name": r[1], "description": r[2] or "",
        "default_backend": r[3] or "opencode", "icon": r[4] or "",
        "order_num": r[5] or 0,
        "color": COLORS.get(r[0], "var(--t2)"),
        "bg": BGS.get(r[0], "var(--bg)"),
    } for r in rows]}


@router.get("/{name}/{owner}/{repo}")
async def get_agent_detail(name: str, owner: str, repo: str, user: dict = Depends(get_current_user)):
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    meta = _get_agent_type_meta(name)
    if not meta:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Agent 类型未配置: {name}"})

    repo_key = f"{owner}/{repo}"

    from src.session_manager import db, db_lock
    session_data = {}
    event_stats = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    recent_events = []

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT backend, skill, repo_gitcode_token, oc_session_id, status, last_active_at, im_provider, im_app_id FROM agent_sessions WHERE repo_key=? AND agent_type=?",
                (repo_key, name)
            )
            row = c.fetchone()
            if row:
                session_data = {"backend": row[0], "skill": row[1] or "", "repo_gitcode_token": row[2] or "", "oc_session_id": row[3], "status": row[4], "last_active_at": row[5],
                               "im_provider": row[6] or "", "im_app_id": row[7] or ""}
        except Exception:
            pass

        try:
            c.execute(
                "SELECT status, COUNT(*) as cnt FROM events WHERE repo_key=? AND agent_type=? GROUP BY status",
                (repo_key, name)
            )
            for r in c.fetchall():
                event_stats[r[0]] = r[1]
        except Exception:
            pass

        try:
            c.execute(
                "SELECT seq, event_type, title, actor, summary, status, created_at, backend, skill, exit_code, duration, started_at, completed_at FROM events WHERE repo_key=? AND agent_type=? ORDER BY seq DESC LIMIT 10",
                (repo_key, name)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "title": r[2] or r[1], "actor": r[3] or "",
                 "summary": r[4] or r[2] or r[1],
                 "status": r[5], "created_at": r[6],
                 "backend": r[7] or "", "skill": r[8] or "",
                 "exit_code": r[9], "duration": r[10],
                 "started_at": r[11] or "", "completed_at": r[12] or ""}
                for r in c.fetchall()
            ]
        except Exception:
            pass

    COLORS = {"vod": "var(--pk)", "issue": "var(--r)", "coding": "var(--a)", "qa": "var(--pu)", "sre": "var(--or)"}
    BGS = {"vod": "#fde8f0", "issue": "#fce5e5", "coding": "#e3edfd", "qa": "#f3e9fe", "sre": "#fef0e4"}

    return {
        "agent_type": name,
        "display_name": meta["display_name"],
        "icon": meta["icon"],
        "description": meta["description"],
        "color": COLORS.get(name, "var(--t2)"),
        "bg": BGS.get(name, "var(--bg)"),
        "backend": session_data.get("backend", meta.get("default_backend", "opencode")),
        "skill": session_data.get("skill", ""),
        "repo_gitcode_token": session_data.get("repo_gitcode_token", ""),
        "status": session_data.get("status", "idle"),
        "last_active": session_data.get("last_active_at", ""),
        "metrics": {
            "total_events": sum(event_stats.values()),
            "event_stats": event_stats,
        },
        "recent_events": recent_events,
        "im": {
            "provider": session_data.get("im_provider", ""),
            "app_id": session_data.get("im_app_id", ""),
            "connected": bool(session_data.get("im_app_id")),
        },
    }


class AgentConfig(BaseModel):
    backend: str = "opencode"
    skill: str = ""
    im_provider: str = ""
    im_app_id: str = ""
    im_app_secret: str = ""


@router.put("/{name}/{owner}/{repo}")
async def update_agent_config(name: str, owner: str, repo: str, body: AgentConfig,
                              user: dict = Depends(get_current_user)):
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    repo_key = f"{owner}/{repo}"
    import uuid
    session_id = str(uuid.uuid4())

    # 获取 repo 绑定的 DevOps Bot Token
    from src.user.db import get_bot_token_by_repo
    repo_token = get_bot_token_by_repo(owner, repo)

    # 密码掩码处理：如果传 "********" 表示不修改
    from src.session_manager import db, db_lock
    # IM 配置仅在 VOD Agent 有效
    im_provider = body.im_provider if name == "vod" else ""
    im_app_id = body.im_app_id if name == "vod" else ""
    im_secret = body.im_app_secret if name == "vod" else ""
    if im_secret == "********":
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT im_app_secret FROM agent_sessions WHERE repo_key=? AND agent_type=?", (repo_key, name))
                row = c.fetchone()
                if row:
                    im_secret = row[0] or ""
        except Exception:
            pass

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT OR REPLACE INTO agent_sessions (id, repo_key, agent_type, backend, skill, repo_gitcode_token, status, last_active_at, im_provider, im_app_id, im_app_secret) VALUES (?,?,?,?,?,?,?,datetime('now'),?,?,?)",
                 (session_id, repo_key, name, body.backend, body.skill, repo_token, "idle",
                  im_provider, im_app_id, im_secret)
            )
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"保存配置失败: {e}"})

    return {"message": "Agent 配置已更新", "session_id": session_id}
