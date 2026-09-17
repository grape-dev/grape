"""Agent 路由：详情、配置、类型元数据"""
import json
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user_or_api_key, get_bot_token, require_no_session_token
from src.config import log
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENT_TYPES = ["vod", "issue", "coding", "qa", "sre"]


def _update_scene_engine_ids(repo_key: str, agent_type: str, engine_ids: list[int], user_id: int = 0):
    """更新当前场景 node 的 engine_runtime_ids。

    查找仓库当前应用的场景（managed_repos.dev_scene），更新对应 node 的 engine_runtime_ids。
    方案 B：场景只与仓库关联，无 official/user fork 逻辑。
    """
    from src.session_manager import db, db_lock
    from src.api.pipeline_config import _query_scene_by_id, update_custom_scene
    # 查当前场景
    scene_id = ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                scene_id = row[0]
    except Exception:
        pass
    if not scene_id:
        return
    # 查场景
    scene_info = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not scene_info:
        return
    # 更新 node 的 engine_runtime_ids
    nodes = scene_info.get("nodes", [])
    updated = False
    for node in nodes:
        if (node.get("tag") or node.get("id", "")) == agent_type:
            node["engine_runtime_ids"] = engine_ids
            updated = True
            break
    if updated:
        update_custom_scene(
            scene_id=scene_id, repo_key=repo_key, nodes=nodes)


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
                        "default_backend": row[3] or "", "icon": row[4] or "", "order_num": row[5] or 0,
                        "enable": bool(row[6]) if row[6] is not None else True}
        except Exception:
            pass
    return None


@router.get("/types")
def list_agent_types():
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
        "default_backend": r[3] or "", "icon": r[4] or "",
        "order_num": r[5] or 0,
        "enable": bool(r[6]) if r[6] is not None else True,
        "color": COLORS.get(r[0], "var(--t2)"),
        "bg": BGS.get(r[0], "var(--bg)"),
    } for r in rows]}


@router.get("/{name}/{owner}/{repo}", operation_id="agents_detail", summary="Get agent config", description="Returns the agent session config (runtime/skill/engine bindings) for name/owner/repo.")
def get_agent_detail(name: str, owner: str, repo: str, issue_number: str = "",
                     keyword: str = Query("", description="事件关键词搜索（seq/issue_id/event_type/title/summary/actor）"),
                     page: int = Query(1, ge=1, description="页码，从 1 开始"),
                     page_size: int = Query(10, ge=1, le=100, description="每页条数"),
                     user: dict = Depends(require_no_session_token)):
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    meta = _get_agent_type_meta(name)
    if not meta:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Agent 类型未配置: {name}"})

    from src.repo_key import to_repo_key
    _owner_repo = f"{owner}/{repo}"
    repo_key = to_repo_key(_owner_repo)

    # 权限校验：用户必须有权访问该仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    # 同时查 gitcode: 和 github: 前缀（用户可能切换了 provider）
    _repo_keys = [repo_key]
    if repo_key.startswith("gitcode:"):
        _repo_keys.append(f"github:{_owner_repo}")
    elif repo_key.startswith("github:"):
        _repo_keys.append(f"gitcode:{_owner_repo}")
    _rk_placeholders = ",".join("?" * len(_repo_keys))

    # Issue #108: 归一化 issue_number → #N 格式，供前端高亮匹配
    highlight_issue_id = ""
    if issue_number:
        _inum = str(issue_number).strip().lstrip("#")
        if _inum:
            highlight_issue_id = f"#{_inum}"

    from src.session_manager import db, db_lock
    session_data = {}
    event_stats = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    recent_events = []
    events_total = 0
    vod_bot_connected = False
    vod_conversations = []

    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, runtime_id, skill, skill_id, repo_gitcode_token, runtime_session_id, status, last_active_at, im_app_id, enable, subscribe_events FROM devops_agent_session WHERE repo_key IN ({}) AND agent_type=?".format(_rk_placeholders),
                (*_repo_keys, name)
            )
            row = c.fetchone()
            if row:
                backend_name = ""
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
                     "status": r[3], "last_active_at": normalize_event_time(r[4] or "")}
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
                "SELECT status, COUNT(*) as cnt FROM events WHERE repo_key IN ({}) AND agent_type=? GROUP BY status".format(_rk_placeholders),
                (*_repo_keys, name)
            )
            for r in c.fetchall():
                event_stats[r[0]] = r[1]
        except Exception:
            pass

        # Issue #127: 事件历史支持 keyword 搜索 + 分页（替换硬编码 LIMIT 10）
        _evt_where = [f"repo_key IN ({_rk_placeholders})", "agent_type=?"]
        _evt_params = list(_repo_keys) + [name]
        if keyword:
            _kw = f"%{keyword}%"
            _evt_where.append("(CAST(seq AS TEXT) LIKE ? OR issue_id LIKE ? OR event_type LIKE ? OR title LIKE ? OR summary LIKE ? OR actor LIKE ?)")
            _evt_params += [_kw, _kw, _kw, _kw, _kw, _kw]
        _evt_where_sql = " AND ".join(_evt_where)

        try:
            c.execute(f"SELECT COUNT(*) FROM events WHERE {_evt_where_sql}", _evt_params)
            events_total = c.fetchone()[0]
        except Exception:
            events_total = 0

        try:
            _offset = (page - 1) * page_size
            c.execute(
                f"SELECT seq, event_type, title, actor, summary, status, created_at, runtime, skill, exit_code, duration, started_at, completed_at, issue_id, repo_key FROM events WHERE {_evt_where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?",
                _evt_params + [page_size, _offset]
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "title": r[2] or r[1], "actor": r[3] or "",
                 "summary": r[4] or r[2] or r[1],
                 "status": r[5], "created_at": normalize_event_time(r[6]),
                 "backend": r[7] or "", "skill": r[8] or "",
                 "exit_code": r[9], "duration": r[10],
                 "started_at": normalize_event_time(r[11] or ""), "completed_at": normalize_event_time(r[12] or ""),
                 "issue_id": r[13] or "", "repo_key": r[14] or ""}
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

    # 当前引擎集合（从场景 node 的 engine_runtime_ids 读取，前端回显多选）
    pool = []
    try:
        from src.runtime.instances import instance_name
        from src.api.pipeline_config import get_scene_engine_ids
        from src.session_manager import db, db_lock
        # 查当前场景
        _scene_id = ""
        _uid = user.get("id", 0) if isinstance(user, dict) else 0
        _eids = get_scene_engine_ids(_scene_id, name, repo_key=repo_key, user_id=_uid)
        if _eids:
            from src.runtime.engine_select import _engines_by_ids, load_engine_selection, _norm_repo
            _cfg = load_engine_selection()
            _repo = _norm_repo(repo_key)
            pool = _engines_by_ids(_eids, _repo, _cfg)
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
        "backend": session_data.get("instance_name") or session_data.get("backend", ""),
        "backends": [p.get("instance_name") or p.get("name") for p in pool],  # 引擎集合（多选回显）
        "pool": pool,  # 完整集合信息（含 priority/status 等）
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
        "events_total": events_total,
        "events_page": page,
        "events_page_size": page_size,
        "highlight_issue_id": highlight_issue_id,
        "im": {
            "im_app_id": session_data.get("im_app_id", ""),
            "connected": vod_bot_connected,
            "conversations": vod_conversations,
        } if name == "vod" else None,
    }


class AgentConfig(BaseModel):
    backend: str = ""  # 无字面默认（engine-selection-design.md v2）：单引擎由 backends 集合派发解析
    backends: list[str] | None = None  # 引擎集合（1..N 引擎实例名）
    skill: str = ""  # 兼容旧前端（按名称保存）；新前端传 skill_id 直传
    skill_id: int = 0  # 新：直接传 skill 主键 id，避免同名 skill 误命中
    skill_scope: str = ""  # 兼容旧前端（official / repo / user）
    enable: bool | None = None
    subscribe_events: list | None = None
    im_app_id: str = ""
    im_app_secret: str = ""


@router.post("/{name}/{owner}/{repo}/clean", operation_id="agents_clean", summary="Clean agent session", description="Cleans an agent session by clearing runtime_session_id; a new session is created on the next event.")
def clean_agent_session(name: str, owner: str, repo: str,
                              user: dict = Depends(require_no_session_token)):
    """清理 Agent 会话：将 devops_agent_session.runtime_session_id 置空。

    clean 之后清除 runtime_session_id，下一次事件到来时会创建新的 runtime_session_id
    （Agent 重新开始全新上下文，不延续历史对话）。
    """
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_agent_session SET runtime_session_id='' WHERE repo_key=? AND agent_type=?",
                  (repo_key, name))
        updated = c.rowcount
        db.commit()
    log.info(f"Agent session cleaned: repo={repo_key}, agent={name}, rows={updated}")
    return {"message": "Agent 会话已清理（runtime_session_id 已置空）", "cleaned": updated > 0, "agent_type": name}


@router.put("/{name}/{owner}/{repo}", operation_id="agents_update", summary="Update agent config", description="Updates the agent session config (engine/skill/IM bindings).")
async def update_agent_config(name: str, owner: str, repo: str, body: AgentConfig,
                              user: dict = Depends(require_no_session_token)):
    if name not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"未知 Agent: {name}"})

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

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
                    "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type, bot_name) VALUES (?, ?, '')",
                    (body.im_app_id, "vod_bot")
                )
                db.commit()

    # ---- 引擎配置：写入场景 node 的 engine_runtime_ids ----
    # backends 显式提供 → 解析为 runtime_id 列表，更新当前场景 node；
    # 无场景则为空（全广播/兜底）。
    if body.backends is not None:
        from src.runtime.instances import resolve_instance_id, scope_of_runtime
        from src.user.db import get_runtime_id_by_name
        rids = []
        for bn in body.backends:
            # 优先按数字 ID 解析（前端传 runtime_id）；非数字才按名字解析
            _rid = None
            try:
                _rid = int(bn)
                # 校验 ID 存在
                with db_lock:
                    c2 = db.cursor()
                    c2.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (_rid,))
                    if not c2.fetchone():
                        _rid = None
            except (ValueError, TypeError):
                pass
            if _rid is None:
                _rid = resolve_instance_id(bn)
            if _rid is None:
                _rid = get_runtime_id_by_name(bn)
            if not _rid:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"未知引擎: {bn}"})
            rids.append(_rid)
        # 更新场景 node 的 engine_runtime_ids
        try:
            _update_scene_engine_ids(repo_key, name, rids, user.get("id", 0))
        except Exception as e:
            log.warning(f"Agent engine config: update scene node failed: {e}")
        log.info(f"Agent engine configured: {repo_key}:{name} -> {rids}")

    with db_lock:
        c = db.cursor()
        try:
            from src.user.db import ensure_default_backend, get_runtime_id_by_name
            from src.runtime.instances import resolve_instance_id, scope_of_runtime
            # runtime_id：优先按 backends 集合首项解析；其次 body.backend；否则按类型名兜底
            _backend_for_rid = body.backend
            if not _backend_for_rid and body.backends:
                _backend_for_rid = body.backends[0]
            runtime_id = resolve_instance_id(_backend_for_rid) if _backend_for_rid else None
            if runtime_id is not None:
                _scope = scope_of_runtime(runtime_id)
                if _scope == "user":
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"DevOps Agent 只能绑定仓库/全局引擎，所选为个人引擎: {_backend_for_rid}"})
            else:
                # 优先按唯一引擎名（engine name / instance_name）解析；否则按类型名（S2 健康选择）兜底
                runtime_id = resolve_instance_id(_backend_for_rid)
                if runtime_id is not None:
                    pass  # 个人引擎（scope=user）也允许绑定 DevOps Agent
                else:
                    runtime_id = get_runtime_id_by_name(_backend_for_rid) or ensure_default_backend(_backend_for_rid or "opencode") or 0
            # 精确解析 skill_id：
            # 新方式：body.skill_id 有值 → 直接用，同步查出 skill_name 写入 skill 字段（兜底）
            # 旧方式兼容：body.skill 有值且无 skill_id → 按 skill_scope 查 id
            # skill_id=0 且 skill='' → 清空配置（用默认映射）
            skill_id = 0
            skill_name_for_save = ""
            if body.skill_id:
                from src.user.db import get_skill_by_id
                _sk = get_skill_by_id(body.skill_id)
                if _sk:
                    skill_id = body.skill_id
                    skill_name_for_save = _sk.get("skill_name") or ""
                else:
                    log.warning(f"Agent config: skill_id={body.skill_id} not found or inactive, ignoring")
            elif body.skill:
                # 旧方式兼容：按名称+scope 查 id
                from src.session_manager import db as _db, db_lock as _db_lock
                def _find_skill(scope, owner_id=""):
                    with _db_lock:
                        _c = _db.cursor()
                        if owner_id:
                            from src.repo_key import from_repo_key
                            _owners = list(dict.fromkeys([owner_id, from_repo_key(owner_id)]))
                            _q = ",".join("?" * len(_owners))
                            row = _c.execute(
                                f"SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id IN ({_q}) AND is_active=1 LIMIT 1",
                                (body.skill, scope, *_owners),
                            ).fetchone()
                        else:
                            row = _c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1",
                                             (body.skill, scope)).fetchone()
                        return row[0] if row else 0

                if getattr(body, "skill_scope", "") == "repo":
                    skill_id = _find_skill("repo", repo_key)
                elif getattr(body, "skill_scope", "") == "official":
                    skill_id = _find_skill("official")
                elif getattr(body, "skill_scope", "") == "user":
                    skill_id = _find_skill("user", str(user["id"]))
                else:
                    skill_id = _find_skill("repo", repo_key)
                    if not skill_id:
                        skill_id = _find_skill("official")
                skill_name_for_save = body.skill
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
                runtime_id=runtime_id, skill=skill_name_for_save, skill_id=skill_id,
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
