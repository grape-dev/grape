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

    # 总执行时长：所有已完成事件的 duration 之和
    total_duration = 0.0
    for e in issue_events:
        d = e.get("duration")
        if d and isinstance(d, (int, float)):
            total_duration += d

    return {
        "issue_id": issue_id,
        "title": title,
        "stages": stages,
        "statuses": stage_statuses,
        "result": result,
        "stage": stage,
        "agents": involved_agents,
        "current_owner": current_owner,
        "event_count": len(issue_events),
        "duration": round(total_duration, 1) if total_duration > 0 else None,
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
async def get_pipeline(owner: str, repo: str, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at, actor, payload FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4],
         "status": r[6], "created_at": normalize_event_time(r[7]), "actor": r[8] or "", "payload": r[9] or ""}
        for r in rows
    ]

    issue_ids = list(dict.fromkeys(e["issue_id"] for e in events if e["issue_id"]))
    pipelines = [_get_issue_pipeline(repo_key, iid, events) for iid in issue_ids]
    pipelines = [p for p in pipelines if p is not None]

    # 按 Issue 最新活动倒序：最新的 issue 排最前（原为 issue 首次出现顺序，最早 issue 恒在前）
    # 用每个 issue 的最新 event seq 作为排序键（seq 单调递增，最新 seq = 最新活动）
    def _latest_seq(p):
        return max((e["seq"] for e in events if e["issue_id"] == p["issue_id"]), default=0)
    pipelines.sort(key=_latest_seq, reverse=True)

    # 执行态派生（前端 B，见 multi-issue-isolation-design.md §4.4）：由 agent_tasks 实时派生，
    # 不维护 issue_agent_session.status 双份记账，避免状态漂移
    running_issues = _running_issue_ids(repo_key)
    for p in pipelines:
        p["executing"] = p["issue_id"] in running_issues

    active = pipelines[0] if pipelines else None
    history = pipelines[1:] if len(pipelines) > 1 else []

    return {
        "repo": f"{owner}/{repo}",
        "active": active,
        "history": history,
        "active_count": 1 if active and active["result"] in ("Processing", "Pending") else 0,
        "today_count": len(pipelines),
    }
