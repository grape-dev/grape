"""Agent 路由：详情、配置、类型元数据"""
import json
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
                "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, enable FROM agent_type WHERE agent_type=?",
                (agent_type,)
            )
            row = c.fetchone()
            if row:
                return {"agent_type": row[0], "display_name": row[1], "description": row[2],
                        "default_backend": row[3] or "opencode", "icon": row[4] or "", "order_num": row[5] or 0,
                        "enable": bool(row[6]) if row[6] is not None else True}
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
                "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, enable FROM agent_type ORDER BY order_num, agent_type"
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
        "enable": bool(r[6]) if r[6] is not None else True,
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

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

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
                "SELECT id, runtime_id, skill, skill_id, repo_gitcode_token, runtime_session_id, status, last_active_at, im_app_id, enable, subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                (repo_key, name)
            )
            row = c.fetchone()
            if row:
                backend_name = "opencode"
                instance_name_val = ""
                if row[1]:
                    c.execute("SELECT name, credential_id FROM agent_runtimes WHERE id=?", (row[1],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend_name = brow[0]
                        _dev = ""
                        if brow[1]:
                            c.execute("SELECT device_name FROM runtime_credentials WHERE id=?", (brow[1],))
                            _d = c.fetchone()
                            _dev = _d[0] if _d else ""
                        from src.runtime.instances import instance_name
                        instance_name_val = instance_name(brow[0], _dev or "", brow[1] or 0)
                _sub_evts = []
                try:
                    _sub_evts = json.loads(row[10] or "[]") if row[10] else []
                except Exception:
                    _sub_evts = []
                session_data = {"id": row[0], "runtime": backend_name, "skill": row[2] or "", "skill_id": row[3] or 0, "repo_gitcode_token": row[4] or "", "runtime_session_id": row[5], "status": row[6], "last_active_at": row[7],
                               "im_app_id": row[8] or "",
                               "instance_name": instance_name_val,
                               "enable": bool(row[9]) if row[9] is not None else True,
                               "subscribe_events": _sub_evts}
        except Exception:
            pass

        # VOD IM — 查询已绑定的群
        if name == "vod" and session_data.get("id"):
            try:
                c.execute(
                    "SELECT vc.im_chat_id, vc.im_channel_name, vc.runtime_session_id, vc.status, vc.last_active_at FROM vod_conversations vc WHERE vc.agent_session_id=? ORDER BY vc.last_active_at DESC",
                    (session_data["id"],)
                )
                vod_conversations = [
                    {"chat_id": r[0], "chat_name": r[1] or "", "runtime_session_id": r[2] or "",
                     "status": r[3], "last_active_at": r[4] or ""}
                    for r in c.fetchall()
                ]
            except Exception:
                pass
            if session_data.get("im_app_id"):
                try:
                    c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_type='vod_bot' AND bot_app_id=?", (session_data["im_app_id"],))
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
                "SELECT seq, event_type, title, actor, summary, status, created_at, runtime, skill, exit_code, duration, started_at, completed_at, issue_id FROM events WHERE repo_key=? AND agent_type=? ORDER BY seq DESC LIMIT 10",
                (repo_key, name)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "title": r[2] or r[1], "actor": r[3] or "",
                 "summary": r[4] or r[2] or r[1],
                 "status": r[5], "created_at": r[6],
                 "backend": r[7] or "", "skill": r[8] or "",
                 "exit_code": r[9], "duration": r[10],
                 "started_at": r[11] or "", "completed_at": r[12] or "",
                 "issue_id": r[13] or ""}
                for r in c.fetchall()
            ]
        except Exception:
            pass

    COLORS = {"vod": "var(--pk)", "issue": "var(--r)", "coding": "var(--a)", "qa": "var(--pu)", "sre": "var(--or)"}
    BGS = {"vod": "#fde8f0", "issue": "#fce5e5", "coding": "#e3edfd", "qa": "#f3e9fe", "sre": "#fef0e4"}

    # 当前配置技能的 scope（用于前端下拉回显同名技能）
    skill_scope = ""
    if session_data.get("skill_id"):
        try:
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT scope FROM skill WHERE id=? AND is_active=1", (session_data["skill_id"],)).fetchone()
                skill_scope = row[0] if row else ""
        except Exception:
            pass

    # 当前引擎池绑定（agent-cluster-design.md §4.1，前端回显多选）
    pool = []
    try:
        from src.runtime.service import get_agent_pool
        from src.runtime.instances import instance_name
        pool = get_agent_pool(name)
        for _p in pool:
            _p["instance_name"] = instance_name(_p.get("name") or "engine", _p.get("device_name") or "", _p.get("credential_id") or 0)
    except Exception:
        pass

    return {
        "agent_type": name,
        "display_name": meta["display_name"],
        "icon": meta["icon"],
        "description": meta["description"],
        "color": COLORS.get(name, "var(--t2)"),
        "bg": BGS.get(name, "var(--bg)"),
        "backend": session_data.get("instance_name") or session_data.get("backend", meta.get("default_backend", "opencode")),
        "backends": [p.get("instance_name") or p.get("name") for p in pool],  # 引擎池（多选回显）
        "pool": pool,  # 完整池信息（含 busy_slots/status 等）
        "skill": session_data.get("skill", ""),
        "skill_id": session_data.get("skill_id", 0),
        "skill_scope": skill_scope,
        "repo_gitcode_token": session_data.get("repo_gitcode_token", ""),
        "subscribe_events": session_data.get("subscribe_events", []),
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
    backends: list[str] | None = None  # 引擎池（1..N 引擎实例名，agent-cluster-design.md §4.1）
    # None=未传（单 backend 兼容）；[]=显式清空池；非空=替换池绑定
    skill: str = ""
    skill_scope: str = ""  # official / repo（前端下拉回显同名技能时精确匹配）
    enable: bool | None = None  # Agent 启用开关（Dashboard 切换；None=不修改）
    subscribe_events: list | None = None  # 仓库级事件订阅 [{"event_type","branch"}]；None=不修改
    # VOD IM 配置（仅在 name=="vod" 时生效）
    im_app_id: str = ""
    im_app_secret: str = ""


@router.post("/{name}/{owner}/{repo}/clean")
async def clean_agent_session(name: str, owner: str, repo: str,
                              user: dict = Depends(get_current_user)):
    """清理 Agent 会话：将 devops_agent_session.runtime_session_id 置空。

    clean 之后清除 runtime_session_id，下一次事件到来时会创建新的 runtime_session_id
    （Agent 重新开始全新上下文，不延续历史对话）。
    """
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_agent_session SET runtime_session_id='' WHERE repo_key=? AND agent_type=?",
                  (repo_key, name))
        updated = c.rowcount
        db.commit()
    log.info(f"Agent session cleaned: repo={repo_key}, agent={name}, rows={updated}")
    return {"message": "Agent 会话已清理（runtime_session_id 已置空）", "cleaned": updated > 0, "agent_type": name}


@router.put("/{name}/{owner}/{repo}")
async def update_agent_config(name: str, owner: str, repo: str, body: AgentConfig,
                              user: dict = Depends(get_current_user)):
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    app_secret = ""
    from src.user.db import get_bot_token_by_repo
    repo_token = get_bot_token_by_repo(owner, repo)

    from src.session_manager import db, db_lock

    # VOD IM 配置：保存到 global_im_bot_config + 类型独占校验 + 清理旧 app_id
    old_app_id = ""
    if name == "vod" and body.im_app_id:
        app_secret = body.im_app_secret
        if app_secret == "********":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT im_app_secret FROM devops_agent_session WHERE im_app_id=? AND im_app_secret!='' LIMIT 1", (body.im_app_id,))
                    row = c.fetchone()
                    if row:
                        app_secret = row[0]
            except Exception:
                pass
        if app_secret:
            with db_lock:
                c = db.cursor()
                # 类型独占校验
                c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=?", (body.im_app_id,))
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
                        c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (old_app_id,))
                        log.info(f"VOD IM: removed old app_id={old_app_id[:30]}")
                    # 清理旧 conversations
                    c.execute("DELETE FROM vod_conversations WHERE agent_session_id IN (SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod')", (repo_key,))
                    log.info(f"VOD IM: cleaned conversations for {repo_key}")

                c.execute(
                    "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?,?)",
                    (body.im_app_id, "vod_bot")
                )
                db.commit()

    # ---- 引擎池（多引擎选择，agent-cluster-design.md §4.1）----
    # backends 显式提供（含空数组=清空池）→ 替换该 Agent 的引擎池绑定，runtime_id 置 0（池调度/兜底链）。
    # 解析在 db_lock 外执行（set_agent_pool 内部自行加锁，避免嵌套死锁）。
    pool_mode = False
    if body.backends is not None:
        from src.runtime.instances import resolve_instance_id, scope_of_runtime
        from src.user.db import get_runtime_id_by_name
        from src.runtime import service as _rt_service
        rids = []
        for bn in body.backends:
            _rid = resolve_instance_id(bn)
            if _rid is None:
                _rid = get_runtime_id_by_name(bn)
            if not _rid:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"未知引擎: {bn}"})
            _scope = scope_of_runtime(_rid)
            if _scope == "user":
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"DevOps Agent 只能绑定仓库/全局引擎，所选为个人引擎: {bn}"})
            rids.append(_rid)
        _rt_service.set_agent_pool(name, rids)
        pool_mode = True
        log.info(f"Agent pool configured: {name} -> {rids}")

    with db_lock:
        c = db.cursor()
        try:
            from src.user.db import ensure_default_backend, get_runtime_id_by_name
            from src.runtime.instances import resolve_instance_id, scope_of_runtime
            # 池模式：runtime_id=0（入队时按 pool_agent_type 调度）；否则按单引擎解析（兼容旧行为）
            if pool_mode:
                runtime_id = 0
            else:
                # 优先按唯一引擎名（engine name / instance_name）解析；否则按类型名（S2 健康选择）兜底
                runtime_id = resolve_instance_id(body.backend)
                if runtime_id is not None:
                    _scope = scope_of_runtime(runtime_id)
                    # DevOps Agent 只能绑定 仓库/全局 引擎（个人引擎仅 Chat 可用，见 runtime-admin-two-view-design §5.1）
                    if _scope == "user":
                        raise HTTPException(status_code=400, detail={"code": 400, "message": f"DevOps Agent 只能绑定仓库/全局引擎，所选为个人引擎: {body.backend}"})
                else:
                    runtime_id = get_runtime_id_by_name(body.backend) or ensure_default_backend(body.backend or "opencode") or 0
            # 精确按 scope(+owner) 解析 skill_id，避免同名时官方优先误命中
            skill_id = 0
            if body.skill:
                from src.session_manager import db as _db, db_lock as _db_lock
                def _find_skill(scope, owner_id=""):
                    with _db_lock:
                        _c = _db.cursor()
                        if owner_id:
                            row = _c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1 LIMIT 1",
                                             (body.skill, scope, owner_id)).fetchone()
                        else:
                            row = _c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1",
                                             (body.skill, scope)).fetchone()
                        return row[0] if row else 0

                if getattr(body, "skill_scope", "") == "repo":
                    skill_id = _find_skill("repo", repo_key)
                elif getattr(body, "skill_scope", "") == "official":
                    skill_id = _find_skill("official")
                else:
                    skill_id = _find_skill("repo", repo_key)
                    if not skill_id:
                        skill_id = _find_skill("official")
            # enable：body 显式传则用；否则继承 agent_type 默认（sre 默认关）
            if body.enable is not None:
                _enable = 1 if body.enable else 0
            else:
                from src.api.session_store import get_agent_type_enable
                _enable = get_agent_type_enable(name)
            # 保留既有主键 id（防 devops_agent_session.id 换新导致 vod_conversations 悬空，
            # 见 session_store.upsert_agent_session 背景注释）；im 三字段仅 VOD 显式传值时写
            from src.api.session_store import upsert_agent_session
            session_id = upsert_agent_session(
                repo_key=repo_key, agent_type=name,
                runtime_id=runtime_id, skill=body.skill, skill_id=skill_id,
                repo_gitcode_token=repo_token, status="idle",
                enable=_enable,
                subscribe_events=json.dumps(body.subscribe_events, ensure_ascii=False) if body.subscribe_events is not None else "[]",
                im_app_id=body.im_app_id if name == "vod" and body.im_app_id else None,
                im_app_secret=app_secret if name == "vod" and body.im_app_id and app_secret else None,
                im_provider="feishu" if name == "vod" and body.im_app_id else None,
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
