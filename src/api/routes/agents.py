"""Agent 路由：详情、配置、类型元数据"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user, get_bot_token
from src.config import log

router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENT_TYPES = ["vod", "issue", "coding", "qa", "sre"]


def _get_agent_type_meta(agent_type: str) -> dict | None:
    """从 agent_type 表读取 Agent 元数据"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT agent_type, display_name, description, default_backend, icon, order_num FROM agent_type WHERE agent_type=?",
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
                "SELECT agent_type, display_name, description, default_backend, icon, order_num FROM agent_type ORDER BY order_num, agent_type"
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
    vod_bot_connected = False
    vod_conversations = []

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, backend, skill, repo_gitcode_token, oc_session_id, status, last_active_at, im_app_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                (repo_key, name)
            )
            row = c.fetchone()
            if row:
                session_data = {"id": row[0], "backend": row[1], "skill": row[2] or "", "repo_gitcode_token": row[3] or "", "oc_session_id": row[4], "status": row[5], "last_active_at": row[6],
                               "im_app_id": row[7] or ""}
        except Exception:
            pass

        # VOD IM — 查询已绑定的群
        if name == "vod" and session_data.get("id"):
            try:
                c.execute(
                    "SELECT vc.feishu_chat_id, vc.feishu_chat_name, vc.oc_session_id, vc.status, vc.last_active_at FROM vod_conversations vc WHERE vc.agent_session_id=? ORDER BY vc.last_active_at DESC",
                    (session_data["id"],)
                )
                vod_conversations = [
                    {"chat_id": r[0], "chat_name": r[1] or "", "oc_session_id": r[2] or "",
                     "status": r[3], "last_active_at": r[4] or ""}
                    for r in c.fetchall()
                ]
            except Exception:
                pass
            if session_data.get("im_app_id"):
                try:
                    c.execute("SELECT COUNT(*) FROM im_bot_config WHERE bot_type='vod_bot' AND bot_app_id=?", (session_data["im_app_id"],))
                    vod_bot_connected = c.fetchone()[0] > 0
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
            "im_app_id": session_data.get("im_app_id", ""),
            "connected": vod_bot_connected,
            "conversations": vod_conversations,
        } if name == "vod" else None,
    }


class AgentConfig(BaseModel):
    backend: str = "opencode"
    skill: str = ""
    # VOD IM 配置（仅在 name=="vod" 时生效）
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

    app_secret = ""
    from src.user.db import get_bot_token_by_repo
    repo_token = get_bot_token_by_repo(owner, repo)

    from src.session_manager import db, db_lock

    # VOD IM 配置：保存到 im_bot_config + 类型独占校验 + 清理旧 app_id
    old_app_id = ""
    if name == "vod" and body.im_app_id:
        app_secret = body.im_app_secret
        if app_secret == "********":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT bot_app_secret FROM im_bot_config WHERE bot_app_id=?", (body.im_app_id,))
                    row = c.fetchone()
                    if row:
                        app_secret = row[0]
            except Exception:
                pass
        if app_secret:
            with db_lock:
                c = db.cursor()
                # 类型独占校验
                c.execute("SELECT bot_type FROM im_bot_config WHERE bot_app_id=?", (body.im_app_id,))
                row = c.fetchone()
                if row and row[0] == "chat_bot":
                    raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被 Chat Agent 使用，不能同时绑定 VOD Agent 和 Chat Agent"})

                # 如果换了新的 app_id，删除旧的 app_id
                c.execute("SELECT im_app_id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'", (repo_key,))
                old_row = c.fetchone()
                old_app_id = old_row[0] if old_row else ""
                if old_app_id and old_app_id != body.im_app_id:
                    # 检查是否还有其他 agent_session 引用旧 app_id
                    c.execute("SELECT COUNT(*) FROM devops_agent_session WHERE im_app_id=? AND repo_key!=?", (old_app_id, repo_key))
                    other_refs = c.fetchone()[0]
                    if other_refs == 0:
                        c.execute("DELETE FROM im_bot_config WHERE bot_app_id=?", (old_app_id,))
                        log.info(f"VOD IM: removed old app_id={old_app_id[:30]}")
                    # 清理旧 conversations
                    c.execute("DELETE FROM vod_conversations WHERE agent_session_id IN (SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod')", (repo_key,))
                    log.info(f"VOD IM: cleaned conversations for {repo_key}")

                c.execute(
                    "INSERT OR REPLACE INTO im_bot_config (bot_app_id, bot_app_secret, bot_type) VALUES (?,?,?)",
                    (body.im_app_id, app_secret, "vod_bot")
                )
                db.commit()

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, backend, skill, repo_gitcode_token, status, last_active_at, im_app_id, im_app_secret, im_provider) VALUES (?,?,?,?,?,?,?,datetime('now'),?,?,?)",
                (session_id, repo_key, name, body.backend, body.skill, repo_token, "idle",
                 body.im_app_id if name == "vod" else "",
                 app_secret if name == "vod" and app_secret else "",
                 "feishu" if name == "vod" and body.im_app_id else "")
            )
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"保存配置失败: {e}"})

    # VOD IM：通过 BotManager 动态增删 Bot
    if name == "vod" and body.im_app_id and app_secret:
        try:
            from src.api.main import bot_manager
            if bot_manager and bot_manager.api:
                if old_app_id and old_app_id != body.im_app_id:
                    await bot_manager.remove_bot(old_app_id)
                    log.info(f"VOD IM: removed old bot {old_app_id[:30]} from BotManager")
                await bot_manager.add_bot(body.im_app_id, app_secret, bot_type="vod_bot")
                log.info(f"VOD IM: added bot {body.im_app_id[:30]} to BotManager")
        except Exception as e:
            log.warning(f"VOD BotManager update failed: {e}")

    return {"message": "Agent 配置已更新", "session_id": session_id}
