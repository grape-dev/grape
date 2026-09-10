"""Pipeline 路由：Issue 工作流聚合展示"""
from fastapi import APIRouter, Depends
from src.api.deps import get_current_user
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

AGENT_ORDER = ["vod", "issue", "coding", "qa", "sre"]
AGENT_LABELS = ["VOD", "Issue", "Coding", "QA", "SRE"]

STAGE_STATUS_MAP = {
    "pending": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}


def _get_latest_assignee(issue_events: list) -> str:
    """从 issue 事件（最新在前倒序）payload 解析 assignee（GitCode 用户名）

    优先 object_attributes.assignee / issue.assignee，assignee 可能为 str 或 dict{username}。
    """
    import json as _json
    for e in reversed(issue_events):
        payload = e.get("payload") or ""
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        obj = payload.get("issue") or payload.get("object_attributes") or {}
        if not isinstance(obj, dict):
            continue
        asg = obj.get("assignee", "")
        if isinstance(asg, dict):
            asg = asg.get("username", "") or asg.get("login", "")
        if asg:
            return asg
    return ""


def _resolve_feishu_name(gitcode_user: str) -> str:
    """按 GitCode 用户名查飞书昵称（缺失回退用户名）"""
    if not gitcode_user:
        return ""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT feishu_name FROM unified_user_mapping WHERE gitcode_user=? AND feishu_name!='' LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            return row[0] if row else gitcode_user
        except Exception:
            return gitcode_user


def _get_issue_pipeline(repo_key: str, issue_id: str, events: list) -> dict:
    """根据 issue_id 下的事件列表构建单个 Pipeline"""
    issue_events = [e for e in events if e.get("issue_id") == issue_id]
    if not issue_events:
        return None

    title = issue_events[0].get("title", "") or issue_id
    # 优先从 tasks 表获取 Issue 真实标题（event title 常为 issue_opened 等类型名或 A2A 占位标题）
    _EVENT_TYPE_NAMES = {
        "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
        "issue_closed", "push", "tag_created", "release_created",
        "pull_request_opened", "pull_request_updated", "pull_request_merged",
        "note_hook_pr", "vod_signal", "a2a_event", "build_start", "build_failure",
        "deploy_start", "deploy_failure", "deploy_finish",
    }
    def _is_placeholder(t):
        """判断 title 是否为占位符（事件类型名或 A2A 占位标题），不是 Issue 真实标题"""
        return not t or t in _EVENT_TYPE_NAMES or t.startswith("A2A:") or t.startswith("A2A (订阅):")
    try:
        from src.session_manager import db as _db, db_lock as _dl
        _iss_num = int(str(issue_id).lstrip("#"))
        with _dl:
            _c = _db.cursor()
            _c.execute("SELECT title FROM tasks WHERE repo_key=? AND issue_number=?", (repo_key, _iss_num))
            _t = _c.fetchone()
            if _t and _t[0] and not _is_placeholder(_t[0]):
                title = _t[0]
    except Exception:
        pass
    # events 表中找一条非占位符标题做兜底
    if _is_placeholder(title):
        for _e in issue_events:
            _et = _e.get("title", "") or ""
            if not _is_placeholder(_et):
                title = _et
                break
    # payload 兜底：events title 都是占位符 → 从 issue_opened 事件的 payload 中提取真实标题
    if _is_placeholder(title):
        import json as _json
        for _e in issue_events:
            if _e.get("event_type") != "issue_opened":
                continue
            _pl = _e.get("payload", "")
            if isinstance(_pl, str):
                try:
                    _pl = _json.loads(_pl)
                except Exception:
                    _pl = {}
            if not isinstance(_pl, dict):
                continue
            _obj = _pl.get("object_attributes", {})
            _iss = _pl.get("issue", {})
            _pt = ""
            if isinstance(_obj, dict):
                _pt = _obj.get("title", "") or ""
            if not _pt and isinstance(_iss, dict):
                _pt = _iss.get("title", "") or ""
            if _pt and not _is_placeholder(_pt):
                title = _pt
                break
    # GitCode API 兜底：tasks/events/payload 都没真实标题 → 调 API 获取并回写 tasks 表
    if _is_placeholder(title):
        try:
            _iss_num_api = int(str(issue_id).lstrip("#"))
            from src.api.event_pipeline import _fetch_issue_title_via_api
            _api_title = _fetch_issue_title_via_api(repo_key, issue_id)
            if _api_title:
                title = _api_title
                # 回写 tasks 表（后续不再调 API）
                try:
                    from src.session_manager import db as _db2, db_lock as _dl2
                    with _dl2:
                        _c2 = _db2.cursor()
                        _c2.execute("UPDATE tasks SET title=? WHERE repo_key=? AND issue_number=? AND (title='' OR title IS NULL)",
                                    (_api_title, repo_key, _iss_num_api))
                        _db2.commit()
                except Exception:
                    pass
        except Exception:
            pass
    stages = []
    stage_statuses = []

    for agent in AGENT_ORDER:
        agent_events = [e for e in issue_events if e.get("agent_type") == agent]
        if not agent_events:
            stages.append({"agent": agent, "latest_event": None})
            stage_statuses.append(0)
            continue

        latest = agent_events[-1]
        status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
        # 最新一条 event 明细（卡片展示用）
        latest_event = {
            "seq": latest.get("seq", 0),
            "title": latest.get("title", "") or latest.get("event_type", ""),
            "event_type": latest.get("event_type", ""),
            "status": latest.get("status", ""),
            "actor": latest.get("actor", ""),
        }
        stages.append({"agent": agent, "latest_event": latest_event})
        stage_statuses.append(status)

    all_done = all(s == 2 for s in stage_statuses)
    any_failed = any(s == 3 for s in stage_statuses)
    any_running = any(s == 1 for s in stage_statuses)

    if all_done:
        result = "Released"
    elif any_failed:
        result = "Failed"
    elif any_running:
        result = "Processing"
    else:
        result = "Pending"

    involved_agents = [AGENT_LABELS[i] for i, s in enumerate(stage_statuses) if s > 0]

    # 责任人：优先 Issue assignee（issue_state.assignee，GitCode 真实指派，可为 grape_bot 或用户），
    # 回退从事件 actor 推断（_get_latest_assignee 已因 payload 不含 assignee 而基本失效，保留兜底）。
    current_owner = ""
    from src.api.session_store import get_issue_stage
    _st = get_issue_stage(repo_key, issue_id)
    if _st and _st.get("assignee"):
        current_owner = _resolve_feishu_name(_st["assignee"])
    else:
        assignee = _get_latest_assignee(issue_events)
        if assignee:
            current_owner = _resolve_feishu_name(assignee)
        else:
            for i in range(len(stage_statuses) - 1, -1, -1):
                if stage_statuses[i] > 0:
                    agent = AGENT_ORDER[i]
                    latest = stages[i].get("latest_event") or {}
                    actor = latest.get("actor", "")
                    if actor:
                        from src.user.db import get_all_bot_usernames
                        if actor in get_all_bot_usernames():
                            from src.api.event_pipeline import get_agent_display_name
                            current_owner = get_agent_display_name(agent)
                        else:
                            current_owner = actor
                    break

    # 生命周期阶段（issue_state.stage）：vod/issue/coding/qa/sre/done；无记录默认 vod
    stage = (_st.get("stage") if _st else "") or "vod"
    # Issue 级开发场景（VOD 判定，空=仓库默认）
    scene_id = (_st.get("scene_id") if _st else "") or ""
    # 解析场景名称（内置 + 成员自定义）
    scene_name = ""
    issue_pipeline_config = None
    if scene_id:
        from src.api.pipeline_config import get_pipeline_config
        _cfg = get_pipeline_config(scene_id, repo_key=repo_key)
        if _cfg:
            scene_name = _cfg.get("name", scene_id)
            issue_pipeline_config = _cfg

    # 总执行时长：所有已完成事件的 duration 之和
    total_duration = 0.0
    for e in issue_events:
        d = e.get("duration")
        if d and isinstance(d, (int, float)):
            total_duration += d

    # 父任务 ID（子任务场景，issue_state.parent_issue_id）
    parent_issue_id = (_st.get("parent_issue_id") if _st else "") or ""

    return {
        "issue_id": issue_id,
        "title": title,
        "stages": stages,
        "statuses": stage_statuses,
        "result": result,
        "stage": stage,
        "scene_id": scene_id,
        "scene_name": scene_name,
        "pipeline_config": issue_pipeline_config,
        "agents": involved_agents,
        "current_owner": current_owner,
        "event_count": len(issue_events),
        "duration": round(total_duration, 1) if total_duration > 0 else None,
        "parent_issue_id": parent_issue_id,
    }


def _running_issue_ids(repo_key: str) -> set[str]:
    """按 repo 查 agent_tasks 中 queued/dispatched/running 的 issue_id 集合（前端 B 执行态派生）。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT issue_id FROM agent_tasks WHERE repo_key=? AND status IN ('queued','dispatched','running') AND issue_id!=''",
                (repo_key,)
            )
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()


@router.get("/{owner}/{repo}")
def get_pipeline(owner: str, repo: str, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at, actor, payload, duration FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4],
         "status": r[6], "created_at": normalize_event_time(r[7]), "actor": r[8] or "", "payload": r[9] or "",
         "duration": r[10]}
        for r in rows
    ]

    issue_ids = list(dict.fromkeys(e["issue_id"] for e in events if e["issue_id"]))
    # 预建 issue_id → events 列表索引（避免 _get_issue_pipeline 内部反复遍历全量 events）
    _events_by_issue = {}
    for e in events:
        _events_by_issue.setdefault(e["issue_id"], []).append(e)
    pipelines = [_get_issue_pipeline(repo_key, iid, _events_by_issue.get(iid, [])) for iid in issue_ids]
    pipelines = [p for p in pipelines if p is not None]

    # 按 Issue 最新活动倒序：用预建索引中的最大 seq 排序（O(N)，避免反复遍历全量 events）
    _max_seq_cache = {iid: max((e["seq"] for e in evts), default=0) for iid, evts in _events_by_issue.items()}
    pipelines.sort(key=lambda p: _max_seq_cache.get(p["issue_id"], 0), reverse=True)

    # 执行态派生（前端 B，见 multi-issue-isolation-design.md §4.4）：由 agent_tasks 实时派生，
    # 不维护 issue_agent_session.status 双份记账，避免状态漂移
    running_issues = _running_issue_ids(repo_key)
    for p in pipelines:
        p["executing"] = p["issue_id"] in running_issues

    active = pipelines[0] if pipelines else None
    history = pipelines[1:] if len(pipelines) > 1 else []

    # Pipeline config (event-dispatch-redesign-v2.md Phase 2)
    from src.api.pipeline_config import get_pipeline_config
    scene_id = ""
    pipeline_config = get_pipeline_config(scene_id) if scene_id else None

    return {
        "repo": f"{owner}/{repo}",
        "active": active,
        "history": history,
        "active_count": 1 if active and active["result"] in ("Processing", "Pending") else 0,
        "today_count": len(pipelines),
        "scene_id": scene_id,
        "pipeline_config": pipeline_config,
    }
