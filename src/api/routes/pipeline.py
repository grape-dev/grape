"""Pipeline 路由：Issue 工作流聚合展示"""
from fastapi import APIRouter, Depends
from src.api.deps import require_no_session_token
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


def _resolve_feishu_name(gitcode_user: str, repo_key: str = "") -> str:
    """按平台用户名查飞书昵称（缺失回退用户名）。

    FR-17.1: 有 repo_key 时按 provider 分查对应列，避免跨平台同名误命中。
    """
    if not gitcode_user:
        return ""
    from src.session_manager import db, db_lock
    # FR-CQ-1: 提取为 um_column_for_repo 工具函数
    from src.repo_key import um_column_for_repo
    _col = um_column_for_repo(repo_key)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT feishu_name FROM unified_user_mapping WHERE {_col}=? AND feishu_name!='' LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            return row[0] if row else gitcode_user
        except Exception:
            return gitcode_user


def _get_issue_pipeline(repo_key: str, issue_id: str, events: list,
                        title_map: dict = None, stage_map: dict = None,
                        pipe_cfg_cache: dict = None, get_pipe_cfg=None,
                        bot_usernames: set = None, feishu_name_cache: dict = None) -> dict:
    """根据 issue_id 下的事件列表构建单个 Pipeline

    参数:
        title_map: 批量查出的 issue 标题映射（消除标题 N+1）
        stage_map: 批量查出的 issue stage/scene/assignee 映射（消除 get_issue_stage N+1）
        pipe_cfg_cache: 场景配置缓存 {scene_id: config}（消除 get_pipeline_config N+1）
        get_pipe_cfg: 获取场景配置的回调函数（带缓存）
        bot_usernames: 仓库的 bot_username 集合（消除 get_all_bot_usernames N+1）
        feishu_name_cache: 飞书用户名缓存 {gitcode_user: feishu_name}（消除 _resolve_feishu_name N+1）
    """
    issue_events = [e for e in events if e.get("issue_id") == issue_id]
    if not issue_events:
        return None

    # PR 订阅伪 issue_id 提前解析（供 title 兜底使用）
    import re as _re
    _pr_match = _re.match(r'#pr_(\d+)_', issue_id)

    # 优先从批量 title_map 取标题（消除 N+1）
    title = ""
    if title_map:
        title = title_map.get(issue_id, "")
    if not title:
        title = issue_events[0].get("title", "") or issue_id
    # 如果 title_map 提供了标题且非占位符，直接使用（跳过 N+1 DB 查询）
    _EVENT_TYPE_NAMES = {
        "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
        "issue_closed", "push", "tag_created", "release_created",
        "pull_request_opened", "pull_request_updated", "pull_request_merged",
        "note_hook_pr", "vod_signal", "a2a_event", "build_start", "build_failure",
        "deploy_start", "deploy_failure", "deploy_finish",
        # Issue #329: cross-pipeline handoff event types
        "assignee_changed", "cross_scene_handoff",
    }
    def _is_placeholder(t):
        """判断 title 是否为占位符（事件类型名或 A2A 占位标题），不是 Issue 真实标题"""
        return not t or t in _EVENT_TYPE_NAMES or t.startswith("A2A:") or t.startswith("A2A (订阅):") or t.startswith("PR 订阅 QA:")

    # title_map 已提供有效标题时跳过 DB 查询；否则回退到 DB 查询（兼容无 title_map 的调用）
    if not title_map or _is_placeholder(title):
        try:
            from src.session_manager import db as _db, db_lock as _dl
            _iss_num = int(str(issue_id).lstrip("#"))
            with _dl:
                _c = _db.cursor()
                _c.execute("SELECT title FROM issue WHERE repo_key=? AND issue_number=?", (repo_key, _iss_num))
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
    # PR 订阅跨 issue 兜底：同一 PR 号的其他事件可能有真实 PR 标题
    if _is_placeholder(title) and _pr_match:
        _pr_num = _pr_match.group(1)
        for _e in events:
            _eid = _e.get("issue_id", "")
            if _eid == issue_id:
                continue
            if _re.match(rf'#pr_{_pr_num}_', _eid) or _eid == f"#pr_{_pr_num}":
                _et = _e.get("title", "") or ""
                if not _is_placeholder(_et):
                    title = _et
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
                        _c2.execute("UPDATE issue SET title=? WHERE repo_key=? AND issue_number=? AND (title='' OR title IS NULL)",
                                    (_api_title, repo_key, _iss_num_api))
                        _db2.commit()
                except Exception:
                    pass
        except Exception:
            pass
    # 统一计算节点状态（与任务详情页共享同一套逻辑）
    from src.api.pipeline_status import compute_node_statuses, STATUS_COMPLETED

    # 优先从批量 stage_map 取（消除 get_issue_stage/get_issue_scene N+1）
    _st = (stage_map or {}).get(issue_id) or {}
    _stage = _st.get("stage", "")
    _scene_id = _st.get("scene_id", "")
    _pipe_cfg = get_pipe_cfg(_scene_id) if _scene_id and get_pipe_cfg else None
    if not _pipe_cfg and _scene_id:
        from src.api.pipeline_config import get_pipeline_config
        _pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
        if pipe_cfg_cache is not None:
            pipe_cfg_cache[_scene_id] = _pipe_cfg
    _scene_nodes = (_pipe_cfg or {}).get("nodes", []) or [{"id": a, "tag": a} for a in AGENT_ORDER]

    _nodes, stage_statuses = compute_node_statuses(_scene_nodes, issue_events, _stage)

    # 转换为原有数据格式（兼容前端 pipeline.js）
    stages = []
    for n in _nodes:
        _le = n.get("latest_event")
        if _le:
            _latest_event = {
                "seq": _le.get("seq", 0),
                "title": _le.get("title", "") or _le.get("event_type", ""),
                "event_type": _le.get("event_type", ""),
                "status": _le.get("status", ""),
                "actor": _le.get("actor", ""),
            }
        else:
            _latest_event = None
        stages.append({"agent": n.get("id", ""), "latest_event": _latest_event})

    all_done = all(s == STATUS_COMPLETED for s in stage_statuses)
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

    involved_agents = [_nodes[i].get("name") or _nodes[i].get("id", "") for i, s in enumerate(stage_statuses) if s > 0]

    # 责任人：优先 Issue assignee（issue_state.assignee，GitCode 真实指派，可为 grape_bot 或用户），
    # 回退从事件 actor 推断（_get_latest_assignee 已因 payload 不含 assignee 而基本失效，保留兜底）。
    current_owner = ""
    # _st 已在上方从 stage_map 获取
    def _resolve_feishu_name_cached(name):
        if not name:
            return ""
        if feishu_name_cache is not None and name in feishu_name_cache:
            return feishu_name_cache[name]
        result = _resolve_feishu_name(name, repo_key)
        if feishu_name_cache is not None:
            feishu_name_cache[name] = result
        return result

    if _st and _st.get("assignee"):
        current_owner = _resolve_feishu_name_cached(_st["assignee"])
    else:
        assignee = _get_latest_assignee(issue_events)
        if assignee:
            current_owner = _resolve_feishu_name_cached(assignee)
        else:
            for i in range(len(stage_statuses) - 1, -1, -1):
                if stage_statuses[i] > 0:
                    agent = _nodes[i].get("id", "") if i < len(_nodes) else ""
                    latest = stages[i].get("latest_event") or {}
                    actor = latest.get("actor", "")
                    if actor:
                        _bots = bot_usernames if bot_usernames is not None else set()
                        if not _bots:
                            from src.user.db import get_all_bot_usernames
                            _bots = get_all_bot_usernames()
                        if actor in _bots:
                            from src.api.event_pipeline import get_agent_display_name
                            current_owner = get_agent_display_name(agent)
                        else:
                            current_owner = actor
                    break

    # 生命周期阶段（issue_state.stage）：vod/issue/coding/qa/sre/done；无记录默认 vod
    stage = (_st.get("stage") if _st else "") or "vod"
    # Issue 级开发场景（从 stage_map 取，不再回退 get_issue_scene）
    # PR 订阅事件不写 issue_state，回退从 events 表 scene_id 列取
    scene_id = (_st.get("scene_id") if _st else "") or _scene_id or ""
    if not scene_id:
        for _e in issue_events:
            _sid = _e.get("scene_id") or ""
            if _sid:
                scene_id = _sid
                break
    # 解析场景名称（内置 + 成员自定义）——使用缓存回调
    scene_name = ""
    issue_pipeline_config = None
    if scene_id:
        _cfg = get_pipe_cfg(scene_id) if get_pipe_cfg else None
        if not _cfg:
            from src.api.pipeline_config import get_pipeline_config
            _cfg = get_pipeline_config(scene_id, repo_key=repo_key)
        if _cfg:
            scene_name = _cfg.get("display_name") or _cfg.get("name", scene_id)
            issue_pipeline_config = _cfg

    # 总执行时长：所有已完成事件的 duration 之和
    total_duration = 0.0
    for e in issue_events:
        d = e.get("duration")
        if d and isinstance(d, (int, float)):
            total_duration += d

    # 父任务 ID（子任务场景，issue_state.parent_issue_id）
    parent_issue_id = (_st.get("parent_issue_id") if _st else "") or ""

    # PR 订阅伪 issue_id（#pr_{num}_{scene_id}）简化显示
    if _pr_match:
        display_issue_id = f"PR#{_pr_match.group(1)}"
    else:
        display_issue_id = issue_id

    return {
        "issue_id": display_issue_id,
        "raw_issue_id": issue_id,
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


def _running_issue_ids(repo_keys) -> set[str]:
    """按 repo 查 agent_tasks 中 queued/dispatched/running 的 issue_id 集合（前端 B 执行态派生）。"""
    if isinstance(repo_keys, str):
        repo_keys = [repo_keys]
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT issue_id FROM agent_tasks WHERE repo_key IN ({}) AND status IN ('queued','dispatched','running') AND issue_id!=''".format(
                    ",".join("?" * len(repo_keys))
                ),
                repo_keys
            )
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()


@router.get("/{owner}/{repo}", operation_id="pipeline_get", summary="Repository pipeline roadmap", description="Repository pipeline roadmap (stage/nodes/events) for the current user.")
def get_pipeline(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    from src.repo_key import to_repo_key
    _owner_repo = f"{owner}/{repo}"
    repo_key = to_repo_key(_owner_repo)

    # 权限校验：用户必须有权访问该仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    # 同时查 gitcode: 和 github: 前缀的事件（用户可能切换了 provider）
    _repo_keys = [repo_key]
    if repo_key.startswith("gitcode:"):
        _repo_keys.append(f"github:{_owner_repo}")
    elif repo_key.startswith("github:"):
        _repo_keys.append(f"gitcode:{_owner_repo}")

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, "
                "created_at, actor, duration, repo_key, scene_id "
                "FROM events WHERE repo_key IN ({}) AND issue_id != '' ORDER BY seq ASC".format(
                    ",".join("?" * len(_repo_keys))
                ),
                _repo_keys
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4],
         "status": r[6], "created_at": normalize_event_time(r[7]), "actor": r[8] or "", "payload": "",
         "duration": r[9], "repo_key": r[10] if len(r) > 10 else repo_key,
         "scene_id": r[11] if len(r) > 11 else ""}
        for r in rows
    ]

    issue_ids = list(dict.fromkeys(e["issue_id"] for e in events if e["issue_id"]))

    # 批量查标题（消除 N+1：182 次 DB 查询 → 1 次）
    _title_map = {}
    # 批量查 issue stage/scene/assignee（消除 _get_issue_pipeline 内部 get_issue_stage/get_issue_scene N+1）
    _stage_map = {}
    if issue_ids:
        _iss_nums = []
        for iid in issue_ids:
            _n = str(iid).lstrip("#")
            if _n.isdigit():
                _iss_nums.append(int(_n))
        if _iss_nums:
            try:
                with db_lock:
                    c = db.cursor()
                    # 一次查询同时获取 title + stage + scene_id + assignee 等
                    c.execute(
                        "SELECT issue_number, title, stage, owner_agent, assignee, "
                        "scene_id, parent_issue_id, is_subtask_mode "
                        "FROM issue WHERE repo_key IN ({}) AND issue_number IN ({})".format(
                            ",".join("?" * len(_repo_keys)),
                            ",".join("?" * len(_iss_nums))
                        ),
                        (*_repo_keys, *_iss_nums)
                    )
                    for r in c.fetchall():
                        _iid = f"#{r[0]}"
                        _title_map[_iid] = r[1] or ""
                        _stage_map[_iid] = {
                            "stage": r[2] or "", "owner_agent": r[3] or "",
                            "assignee": r[4] or "", "scene_id": r[5] or "",
                            "parent_issue_id": r[6] or "", "is_subtask_mode": r[7] or 0,
                        }
            except Exception:
                pass

    # 场景配置缓存（同仓库内 scene_id 去重，消除 get_pipeline_config N+1）
    _pipe_cfg_cache = {}
    from src.api.pipeline_config import get_pipeline_config
    def _get_pipe_cfg(scene_id):
        if scene_id not in _pipe_cfg_cache:
            _pipe_cfg_cache[scene_id] = get_pipeline_config(scene_id, repo_key=repo_key) if scene_id else None
        return _pipe_cfg_cache[scene_id]

    # 一次性查 bot_usernames（消除 get_all_bot_usernames N+1）
    _bot_usernames = set()
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_username FROM managed_repos WHERE repo_key IN ({}) AND bot_username!=''".format(
                ",".join("?" * len(_repo_keys))
            ), _repo_keys)
            _bot_usernames = {r[0] for r in c.fetchall() if r[0]}
    except Exception:
        pass

    # 飞书用户名缓存（请求级，消除 _resolve_feishu_name N+1）
    _feishu_name_cache = {}

    # 预建 issue_id → events 列表索引（避免 _get_issue_pipeline 内部反复遍历全量 events）
    _events_by_issue = {}
    for e in events:
        _events_by_issue.setdefault(e["issue_id"], []).append(e)
    pipelines = [_get_issue_pipeline(
        repo_key, iid, events,
        _title_map, _stage_map, _pipe_cfg_cache, _get_pipe_cfg,
        _bot_usernames, _feishu_name_cache
    ) for iid in issue_ids]
    pipelines = [p for p in pipelines if p is not None]

    # Issue #89: 检测 tasks 有记录但 events 无记录的 Issue（数据一致性差异）
    # 这些 Issue 在任务中心有任务，但流水线未生成事件 → 前端展示提示
    tasks_without_events = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT issue_number, title FROM issue "
                "WHERE repo_key=? AND status IN ('active','processing') AND issue_number > 0",
                (repo_key,)
            )
            for r in c.fetchall():
                _iid = f"#{r[0]}"
                if _iid not in issue_ids:
                    tasks_without_events.append({"issue_id": _iid, "title": r[1] or ""})
    except Exception:
        pass

    # 按 Issue 最新活动倒序：用预建索引中的最大 seq 排序（O(N)，避免反复遍历全量 events）
    _max_seq_cache = {iid: max((e["seq"] for e in evts), default=0) for iid, evts in _events_by_issue.items()}
    pipelines.sort(key=lambda p: _max_seq_cache.get(p["issue_id"], 0), reverse=True)

    # 执行态派生（前端 B，见 multi-issue-isolation-design.md §4.4）：由 agent_tasks 实时派生，
    # 不维护 issue_agent_session.status 双份记账，避免状态漂移
    running_issues = _running_issue_ids(_repo_keys)
    for p in pipelines:
        p["executing"] = (p.get("raw_issue_id") or p["issue_id"]) in running_issues

    active = pipelines[0] if pipelines else None
    history = pipelines[1:] if len(pipelines) > 1 else []

    # Pipeline config (event-dispatch-redesign-v2.md Phase 2)
    from src.api.pipeline_config import get_pipeline_config
    scene_id = ""
    pipeline_config = get_pipeline_config(scene_id) if scene_id else None

    # 返回实际有事件的 repo_key（带 provider 前缀），供前端生成 VCS 链接
    _actual_repo_key = repo_key
    if events:
        _actual_repo_key = events[0].get("repo_key", repo_key) or repo_key

    # 返回 repo（不带 provider 前缀，前端用 _vcsUrl() 按 repo_key 判断 provider 拼链接）
    from src.repo_key import from_repo_key
    return {
        "repo": from_repo_key(_actual_repo_key),
        "repo_key": _actual_repo_key,
        "active": active,
        "history": history,
        "active_count": 1 if active and active["result"] in ("Processing", "Pending") else 0,
        "today_count": len(pipelines),
        "failed_count": len([p for p in pipelines if p["result"] == "Failed"]),
        "scene_id": scene_id,
        "pipeline_config": pipeline_config,
        "tasks_without_events": tasks_without_events,
        "has_tasks_without_events": len(tasks_without_events) > 0,
    }
