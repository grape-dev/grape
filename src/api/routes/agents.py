"""Agent 路由：详情、配置"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENT_META = {
    "vod": {"display_name": "VOD Agent", "icon": "🔍", "description": "Value-Operational-Data Agent — 事件信号监控与 Issue 触发"},
    "issue": {"display_name": "Issue Agent", "icon": "📋", "description": "Issue 分类、标签、指派、A2A 触发"},
    "coding": {"display_name": "Coding Agent", "icon": "💻", "description": "代码实现、PR 创建"},
    "qa": {"display_name": "QA Agent", "icon": "🧪", "description": "代码审查、质量检测"},
    "sre": {"display_name": "SRE Agent", "icon": "⚙️", "description": "变更日志、部署状态"},
}


@router.get("/{name}/{owner}/{repo}")
async def get_agent_detail(name: str, owner: str, repo: str, user: dict = Depends(get_current_user)):
    if name not in AGENT_META:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    repo_key = f"{owner}/{repo}"
    meta = AGENT_META[name]

    from src.session_manager import db, db_lock
    session_data = {}
    event_stats = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    recent_events = []

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT backend, oc_session_id, status, last_active_at FROM agent_sessions WHERE repo_key=? AND agent_type=?",
                (repo_key, name)
            )
            row = c.fetchone()
            if row:
                session_data = {"backend": row[0], "oc_session_id": row[1], "status": row[2], "last_active_at": row[3]}
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
                "SELECT seq, event_type, title, status, created_at FROM events WHERE repo_key=? AND agent_type=? ORDER BY seq DESC LIMIT 10",
                (repo_key, name)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "title": r[2] or r[1], "status": r[3], "created_at": r[4]}
                for r in c.fetchall()
            ]
        except Exception:
            pass

    from src.api.db_setup import _SEED_FILES
    from src.api.deps import get_bot_token

    bot_token = get_bot_token("default")

    skills_list = []
    from src.config import get_skill_capabilities
    all_skills = get_skill_capabilities()
    for skill_name, skill_info in all_skills.items():
        skills_list.append({
            "name": skill_name,
            "description": skill_info.get("description", "")[:100],
        })

    config = {}
    from src.api.db_setup import _CONFIG_DIR
    try:
        import json, os
        cfg_path = os.path.join(_CONFIG_DIR, "agent-routing.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r") as f:
                routing = json.load(f)
            backends = routing.get("skill_backends", {})
            config = {"backend": backends.get(name, routing.get("default_backend", "opencode"))}
    except Exception:
        config = {"backend": "opencode"}

    return {
        "name": name,
        "display_name": meta["display_name"],
        "icon": meta["icon"],
        "description": meta["description"],
        "backend": session_data.get("backend", config.get("backend", "opencode")),
        "status": session_data.get("status", "idle"),
        "oc_session_id": session_data.get("oc_session_id", ""),
        "last_active": session_data.get("last_active_at", ""),
        "bot_token_configured": bool(bot_token),
        "metrics": {
            "total_events": sum(event_stats.values()),
            "event_stats": event_stats,
        },
        "recent_events": recent_events,
        "skills": skills_list,
    }


class AgentConfig(BaseModel):
    backend: str = "opencode"


@router.put("/{name}/{owner}/{repo}")
async def update_agent_config(name: str, owner: str, repo: str, body: AgentConfig,
                              user: dict = Depends(get_current_user)):
    if name not in AGENT_META:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    repo_key = f"{owner}/{repo}"
    import uuid
    session_id = str(uuid.uuid4())

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT OR REPLACE INTO agent_sessions (id, repo_key, agent_type, backend, status, last_active_at) VALUES (?,?,?,?,?,datetime('now'))",
                (session_id, repo_key, name, body.backend, "idle")
            )
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"保存配置失败: {e}"})

    return {"message": "Agent 配置已更新", "session_id": session_id}
