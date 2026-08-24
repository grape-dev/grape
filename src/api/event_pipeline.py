"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid, threading
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from src.config import log, WORKSPACE_DIR
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import NON_ISSUE_EVENT_ROUTE, PR_DEFAULT_AGENT, ISSUE_EVENT_TYPES, PR_EVENT_TYPES
from src.api.event_store import get_issue_summary
from src.session_manager import db, db_lock

# 本机 direct 执行的有界并发池（替代无界 threading.Thread，见 engine-selection-design.md §9.3）
_DIRECT_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20")),
    thread_name_prefix="ag-direct",
)
# {event_seq: Future} — 供 sweeper 取消排队中的 direct 任务
_DIRECT_FUTURES: dict[int, Future] = {}
_DIRECT_FUTURES_LOCK = threading.Lock()


def start_direct_agent(fn, args, name=""):
    """提交一个本机 direct 执行任务到有界池（webhook / run_pipeline 共用）。"""
    future = _DIRECT_POOL.submit(fn, *args)
    # 从 args 中提取 seq（约定第一个参数是 repo_key，第二个是 seq）
    seq = args[1] if len(args) > 1 else 0
    if seq:
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES[seq] = future
    return future


def _create_direct_task(repo_key, seq, agent_type, backend, skill_name,
                        skill_path, engine_id, evt, issue_id="", skill_id=0):
    """direct 执行前创建 agent_tasks + 设 started_at，防 sweeper 误杀。"""
    from src.runtime.service import _TASK_COLUMNS_LIST
    from src.api.session_store import get_or_create_agent_session
    from src.api.event_store import update_event_config
    from src.runtime.instances import runtime_instance_name
    from datetime import datetime, timezone

    inst_name = runtime_instance_name(engine_id) or backend or ""
    now = datetime.now(timezone.utc).isoformat()

    # 先设 started_at/runtime/skill，让 sweeper 的 no_start 检查跳过
    update_event_config(seq, inst_name, skill_name, now)

    # 查 agent_session_id
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"create_direct_task: no agent_session_id for {repo_key}/{agent_type}")
        return ""

    task_id = "t_" + uuid.uuid4().hex[:12]
    # issue_id 优先用传参，兜底从 evt 解析
    if not issue_id:
        issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""
    from src.repo_key import from_repo_key

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
                f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})",
                (task_id, agent_session_id, engine_id, seq, issue_id, repo_key or "",
                 agent_type or "", skill_name or "", skill_id or 0,
                 "", "", "", "", "", 0, "", "{}",
                 "{}", "", "", "{}", "{}", "{}", 0, "", 0,
                 "queued", None, "", "", "", "{}", now, None, now)
            )
            db.commit()
        log.info(f"Direct task created: {task_id} seq={seq} engine={engine_id}({inst_name}) skill={skill_name}")
    except Exception as e:
        log.warning(f"create_direct_task failed for seq={seq}: {e}")
        return ""
    return task_id


def _update_direct_task(seq, status, exit_code=0, result_text=""):
    """direct 执行完成后更新 agent_tasks 状态（通过 event_seq 反查）。"""
    try:
        from datetime import datetime, timezone
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, completed_at=? "
                "WHERE event_seq=? AND status='queued'",
                (status, exit_code, result_text or "", datetime.now(timezone.utc).isoformat(), seq)
            )
            db.commit()
    except Exception as e:
        log.warning(f"update_direct_task failed for seq={seq}: {e}")


def cancel_direct_task(seq: int) -> bool:
    """取消排队/运行中的 direct 任务。Future 在队列中 → cancel；
    已在运行 → 杀掉对应的 opencode/hermes/codearts 子进程。"""
    cancelled = False
    with _DIRECT_FUTURES_LOCK:
        future = _DIRECT_FUTURES.pop(seq, None)
    if future is not None:
        if not future.done():
            cancelled = future.cancel()
            if cancelled:
                log.info(f"cancel_direct_task: seq={seq} removed from thread pool queue")
            else:
                # Future 已在运行中 → 杀掉对应子进程（按 seq 查 task 的 cwd + engine）
                log.info(f"cancel_direct_task: seq={seq} running, killing engine subprocess")
                try:
                    from src.session_manager import db, db_lock
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT id, runtime_id, cwd, agent_type FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (seq,))
                        tr = c.fetchone()
                    if tr:
                        _task = {"id": tr[0], "runtime_id": tr[1], "cwd": tr[2], "agent_type": tr[3]}
                        # 推断 backend
                        _backend = "opencode"
                        try:
                            with db_lock:
                                c2 = db.cursor()
                                c2.execute("SELECT name FROM agent_runtimes WHERE id=?", (tr[1],))
                                rn = c2.fetchone()
                            if rn and "hermes" in (rn[0] or "").lower():
                                _backend = "hermes"
                            elif rn and "codearts" in (rn[0] or "").lower():
                                _backend = "codearts"
                        except Exception:
                            pass
                        from src.runtime.agent import _kill_stale_engine
                        _kill_stale_engine(_task, _backend)
                        cancelled = True
                except Exception as e:
                    log.warning(f"cancel_direct_task: kill process failed for seq={seq}: {e}")
        else:
            # Future 已完成，清理
            cancelled = True
    return cancelled


def get_agent_display_name(agent_type: str) -> str:
    """从 configs/agent-description.json 读取 Agent 显示名（Issue Agent / QA Agent / Coding Agent 等）。

    注入给 agent 的环境变量 AGENT_NAME 使用（skill 中 [$AGENT_NAME]）。
    未配置时返回 agent_type 本身。
    """
    try:
        _path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-description.json")
        if os.path.exists(_path):
            with open(_path, "r", encoding="utf-8") as _f:
                _data = json.load(_f)
            return (_data.get(agent_type) or {}).get("name", "") or agent_type
    except Exception as e:
        log.warning(f"get_agent_display_name failed for {agent_type}: {e}")
    return agent_type


def _is_pr_payload(payload: dict) -> bool:
    """判断是否为 PR 事件 payload（PR webhook 带 issues 关联列表 / pull_request 键）。"""
    return ("issues" in payload) or bool(payload.get("pull_request")) or bool(payload.get("merge_request"))


def resolve_issue_id(payload: dict) -> str:
    """从事件 payload 提取 Issue 编号。

    注意：PR 事件不返回 PR 号（其 object_attributes.iid 是 PR 号，会误当 issue_id）；
    PR 事件的真实关联 Issue 用 resolve_pr_issue_id（title/分支/issues 列表）。
    """
    if not isinstance(payload, dict):
        return ""
    # A2A payload 用 issue_number 字段
    num = payload.get("issue_number", "")
    if num:
        return f"#{num}"
    # Issue 类事件（issue_comment 等）顶层 issue 对象
    obj = payload.get("issue", {})
    if isinstance(obj, dict):
        num = obj.get("number", obj.get("iid", ""))
        if num:
            return f"#{num}"
    # PR payload 中显式关联的 issues 列表（GitCode pull_request_*）
    issues = payload.get("issues")
    if isinstance(issues, list):
        for it in issues:
            if isinstance(it, dict):
                num = it.get("number", it.get("iid", ""))
                if num:
                    return f"#{num}"
    # Issue 类 webhook 的 object_attributes.iid（PR 事件的该字段是 PR 号，跳过）
    obj = payload.get("object_attributes", {})
    if isinstance(obj, dict) and not _is_pr_payload(payload):
        num = obj.get("number", obj.get("iid", obj.get("noteable_iid", "")))
        if num:
            return f"#{num}"
    return ""


def resolve_pr_issue_id(payload: dict, api_token: str = "") -> str:
    """从 PR 事件 payload 解析关联的 Issue 编号。
    优先级：payload 标题 #N → 分支名 issue-N → 同步调 PR 详情 API。
    """
    import re, json, urllib.request
    from src.api.event_store import _extract_issue_id_from_text

    # 1. 从 object_attributes / pull_request 的 title 提取 #N
    obj = payload.get("object_attributes", {}) or {}
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    title = obj.get("title", "") or pr_obj.get("title", "")
    issue = _extract_issue_id_from_text(title)
    if issue:
        return issue

    # 1.5 从 payload.issues 关联列表提取（GitCode pull_request_* 直接给出关联 Issue）
    issues = payload.get("issues")
    if isinstance(issues, list):
        for it in issues:
            if isinstance(it, dict):
                _num = it.get("number", it.get("iid", ""))
                if _num and str(_num) != "0":
                    return f"#{_num}"

    # 2. 从分支名提取 issue-N
    branch = obj.get("source_branch", "") or obj.get("ref", "") or pr_obj.get("head", {}).get("ref", "") if isinstance(pr_obj.get("head"), dict) else (obj.get("source_branch", "") or obj.get("ref", ""))
    m = re.search(r'issue[-_](\d+)', str(branch))
    if m and m.group(1) != "0":
        return f"#{m.group(1)}"

    # 3. 同步调 PR 详情 API
    pr_number = obj.get("iid", obj.get("number", "")) or pr_obj.get("number", "")
    if pr_number and api_token:
        repo_path = payload.get("project", {}).get("path_with_namespace", "") if isinstance(payload.get("project"), dict) else ""
        if not repo_path:
            repo = payload.get("repository", {})
            if isinstance(repo, dict):
                repo_path = repo.get("full_name", "")
        if repo_path and "/" in repo_path:
            try:
                url = f"https://api.gitcode.com/api/v5/repos/{repo_path}/pulls/{pr_number}"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_token}"})
                data = json.loads(urllib.request.urlopen(req, timeout=10).read())
                # title / head.ref
                issue = _extract_issue_id_from_text(str(data.get("title", "")))
                if issue:
                    return issue
                head = data.get("head", {}) or {}
                m = re.search(r'issue[-_](\d+)', str(head.get("ref", "")))
                if m and m.group(1) != "0":
                    return f"#{m.group(1)}"
            except Exception:
                return ""
    return ""


def _resolve_issue_stage_agent(repo_key: str, event_type: str, payload: dict,
                               default_agent: str) -> str:
    """阶段矩阵路由（D9，见 vod-skill-upgrade-plan.md §3.0.3）。

    关联 issue_id 的事件按 issue_state 阶段判定归属：
    - issue_opened 恒给 vod（D8）
    - Issue 类事件（issue_updated/comment/...）：vod→VOD；issue 及之后→Issue Agent
    - PR 类事件：vod/issue→Issue Agent 审视；coding→Coding；qa→QA；sre→SRE；done→不分发
    无 issue_state 记录 / 非关联事件 → 返回 default_agent（现状 get_domain_agent 结果）。
    """
    from src.api.session_store import get_issue_stage, get_issue_stage_name

    if event_type == "issue_opened":
        return "vod"

    if event_type not in ISSUE_EVENT_TYPES and event_type not in PR_EVENT_TYPES:
        return default_agent

    # 解析关联 issue_id：PR 类事件优先用 title/分支提取（无需 token），Issue 类用 payload 字段
    if event_type in PR_EVENT_TYPES:
        issue_id = resolve_pr_issue_id(payload) or resolve_issue_id(payload) or ""
    else:
        issue_id = resolve_issue_id(payload) or ""
    if not issue_id:
        return default_agent

    st = get_issue_stage(repo_key, issue_id)
    if not st:
        # 无记录：issue_opened 已由上面处理；其余关联事件默认 vod（新 Issue 先审视）
        return "vod" if event_type in ISSUE_EVENT_TYPES else default_agent
    stage = st["stage"]

    if event_type in ISSUE_EVENT_TYPES:
        # Issue 类事件：vod→VOD；issue 及之后→Issue Agent（coding/qa/sre 不直接处理）
        if stage == "vod":
            return "vod"
        if stage == "done":
            return ""  # 已结束不再分发
        return "issue"
    else:  # PR 类事件
        if stage == "vod" or stage == "issue":
            return "issue"  # Issue Agent 审视
        if stage == "done":
            return ""
        return stage  # coding/qa/sre → 对应执行 Agent


def resolve_actor(payload: dict, evt: dict) -> str:
    """从 payload 提取执行者"""
    actor = evt.get("actor", "")
    if actor:
        return actor
    for key in ("user", "sender"):
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            return obj.get("username", obj.get("login", ""))
    return "unknown"


def resolve_pr_target_branch(evt: dict) -> str:
    """从 PR 事件提取合入目标分支（GitCode object_attributes.target_branch / GitHub base.ref）。

    归一化的 branch 仅 push 事件有值；PR 合入事件的目标分支需从 payload 补充。
    见 agent-event-subscription-design.md §五。
    """
    branch = evt.get("branch") or ""
    if branch:
        return branch
    payload = evt.get("payload", {}) or {}
    obj = payload.get("object_attributes", {}) or {}
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    _base = pr_obj.get("base", {}) or {}
    _b = obj.get("target_branch", "") or _base.get("ref", "")
    return str(_b or "").strip()


def _match_repo_subscription(repo_key: str, event_type: str, branch: str) -> str:
    """仓库级事件订阅兜底路由（agent-event-subscription-design.md §六）。

    读取 devops_agent_session.subscribe_events（JSON 列表，元素 {event_type, branch}），
    匹配 event_type 且（订阅 branch 为空=不限分支 / 等于事件分支）→ 返回 agent_type。
    未命中返回空。仅当主路由结果为空时由 run_pipeline 调用。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT agent_type, subscribe_events FROM devops_agent_session WHERE repo_key=? AND subscribe_events IS NOT NULL AND subscribe_events!='' AND subscribe_events!='[]'",
                (repo_key,)
            )
            for row in c.fetchall():
                _agent, _raw = row[0], row[1]
                try:
                    subs = json.loads(_raw or "[]")
                except Exception:
                    continue
                if not isinstance(subs, list):
                    continue
                for s in subs:
                    if not isinstance(s, dict):
                        continue
                    if s.get("event_type") != event_type:
                        continue
                    # 分支：空=不限；逗号分隔多分支（前端输入），精确匹配任一
                    _sb = str(s.get("branch", "") or "").replace("，", ",").strip()
                    _subs = [b.strip() for b in _sb.split(",") if b.strip()] if _sb else []
                    if _subs and branch not in _subs:
                        continue
                    return _agent
    except Exception as e:
        log.warning(f"Pipeline: repo subscription match failed: {e}")
    return ""


def _dispatch_subscription_agent(repo_key: str, evt: dict, event_type: str,
                                 agent_type: str, main_agent: str):
    """订阅并行分发（event-routing-subscription-design.md §2.2）。
    为订阅命中的 Agent 创建独立事件并分派任务，不改 issue_state.stage。
    """
    try:
        from src.session_manager import db, db_lock
        backend = "opencode"
        runtime_id = 0
        skill_name = ""
        skill_id = 0
        skill_path = ""
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            row = c.fetchone()
            if row:
                runtime_id = row[0] or 0
                if row[2]:
                    skill_id = row[2]
                    from src.user.db import get_skill_by_id
                    _sk = get_skill_by_id(row[2])
                    if _sk:
                        skill_name = _sk.get("skill_name") or skill_name
                        skill_path = _sk.get("skill_path") or skill_name
                skill_name = skill_name or row[1] or ""
                if row[0]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                    _br = c.fetchone()
                    if _br and _br[0]:
                        backend = _br[0]
        if not skill_name:
            # 回退默认映射
            import json as _json
            _mapping_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
            if os.path.exists(_mapping_path):
                with open(_mapping_path, "r", encoding="utf-8") as _f:
                    _defaults = _json.load(_f)
                _scene = _defaults.get("scenes", {}).get("default", {})
                # 读仓库开发场景（managed_repos.dev_scene），按场景读映射
                try:
                    from src.user.db import get_repo_scene
                    _scene_id = get_repo_scene(repo_key) or "default"
                    _scene = _defaults.get("scenes", {}).get(_scene_id, _defaults.get("scenes", {}).get("default", {}))
                except Exception:
                    pass
                _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                skill_name = _mapping.get(agent_type, "")
            if not skill_name:
                log.warning(f"Pipeline subscription: no skill for agent={agent_type}, skip extra dispatch")
                return
        # Agent 启用检查
        from src.api.session_store import is_agent_enabled
        if not is_agent_enabled(repo_key, agent_type):
            log.info(f"Pipeline subscription: agent={agent_type} disabled, skip extra dispatch")
            return
        # 创建事件
        from src.api.event_store import append_event
        issue_id = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "") or "#0"
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=f"A2A (订阅): {agent_type}", issue_id=issue_id, source="a2a",
            agent_type=agent_type, actor=evt.get("actor", ""),
            summary=f"订阅触发: {event_type} → {agent_type}（主路由: {main_agent}）",
            payload=evt.get("payload", {}), status="running",
        )
        log.info(f"Pipeline subscription: event created seq={seq} agent={agent_type}")
        # 执行分派
        from src.runtime.engine_select import pick_engine, engine_name
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0)
        if not engine_id:
            log.info(f"Pipeline subscription: no engine for agent={agent_type}, skip")
            from src.api.event_store import update_event_status
            update_event_status(seq, "failed")
            return
        _be = engine_name(engine_id) or backend
        kind = runtime_execution_kind(engine_id)
        if kind == EXEC_MODE_DIRECT:
            evt["_skill_path"] = skill_path
            evt["_skill_id"] = skill_id
            _direct_tid = _create_direct_task(repo_key, seq, agent_type, _be, skill_name,
                                skill_path, engine_id, evt, issue_id, skill_id=skill_id)
            start_direct_agent(_execute_agent, (
                repo_key, seq, agent_type, _be, skill_name, evt, engine_id, _direct_tid,
            ), name=f"sub-{agent_type}-{seq}")
        else:
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id)
        log.info(f"Pipeline subscription: dispatched seq={seq} agent={agent_type} mode={kind}")
    except Exception as e:
        log.warning(f"Pipeline subscription dispatch failed: {e}")


def run_pipeline(raw_body: bytes, headers: dict, account: str = "",
                 repo_key_override: str = "") -> dict:
    """事件处理管道入口：归一化 → 路由 → 存储 → 返回结果"""
    result = {"seq": 0, "event_type": "", "skill": "", "repo_key": "", "ok": False}

    # 1. 归一化
    event_header, source = get_event_header(headers)
    evt = normalize_event(raw_body, event_header, source)
    evt["account"] = account

    repo_key = repo_key_override or f"{source}:{evt['repository']}"
    # 统一 repo_key：带平台前缀贯穿（gitcode:owner/repo），与 DB 内部一致
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # account 兜底用仓库全名（无 default 概念）
    if not account and evt.get("repository"):
        evt["account"] = evt["repository"]
    event_type = evt.get("type", "unknown")

    log.info(f"Pipeline: start, raw_type={event_header}, source={source}, repo={evt['repository']}")

    if not evt["repository"] or event_type == "unknown":
        log.info(f"Pipeline skipped: repository='{evt['repository']}' or event_type='{event_type}' not recognized")
        return result

    # 2. 自循环检测：actor 是任一 bot 用户名 → 跳过（评论/评论PR/打标签等 bot 自身动作的回声）
    actor = resolve_actor(evt.get("payload", {}), evt)
    log.info(f"Pipeline: actor={actor}, event_type={event_type}")
    if actor:
        from src.user.db import get_all_bot_usernames
        bot_usernames = get_all_bot_usernames()
        if actor in bot_usernames:
            log.info(f"Pipeline skip: self-comment from bot={actor}")
            return result
        log.info(f"Pipeline: comment from {actor}, not a bot ({bot_usernames})")

    # 2.1 issue_updated 纯标签变更（changes.labels 有变化、其余内容未动）→ bot 打标签回声，跳过
    if event_type == "issue_updated":
        try:
            _payload = evt.get("payload", {}) or {}
            _changes = _payload.get("changes", {}) or {}
            _content_keys = [k for k in ("title", "description") if k in _changes]
            _label_changed = "labels" in _changes
            if _label_changed and not _content_keys:
                log.info(f"Pipeline skip: issue_updated with only label change (likely bot label echo)")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "label_only_change")
                return result
        except Exception as e:
            log.warning(f"Pipeline: issue_updated label-only check failed: {e}")

    # 3. 路由判定（D9 阶段矩阵 + 非 Issue 静态映射，替代 agent_type.subscribe_events，见 vod-skill-upgrade-plan §3.0.4）
    if event_type == "issue_opened":
        agent_type = "vod"
        log.info(f"Pipeline: issue_opened -> VOD (stage init)")
        # project-info 后台刷新（服务端 codegraph 生成基线，不阻塞 webhook；见 codegraph-builder-plan.md）
        try:
            from src.project_info.builder import maybe_refresh_project_info
            threading.Thread(target=maybe_refresh_project_info, args=(repo_key,), daemon=True).start()
        except Exception as e:
            log.warning(f"Pipeline: project-info refresh trigger failed: {e}")
    else:
        if event_type in PR_EVENT_TYPES:
            _default = PR_DEFAULT_AGENT
        elif event_type in ISSUE_EVENT_TYPES:
            _default = "issue"          # Issue 类事件无关联 issue 时默认 Issue Agent（审视）
        else:
            _default = NON_ISSUE_EVENT_ROUTE.get(event_type, "")
        agent_type = _resolve_issue_stage_agent(repo_key, event_type, evt.get("payload", {}) or {}, _default)
        log.info(f"Pipeline: routing result -> agent_type='{agent_type}'")
    # 仓库级事件订阅匹配（提前，供 fallback 与双路径共用）：
    # - 主路由为空 → 订阅兜底（现状）
    # - 主路由非空且订阅命中不同 Agent → 双路径并行（event-routing-subscription-design.md §2.2）
    _pr_branch = resolve_pr_target_branch(evt)
    if _pr_branch and not evt.get("branch"):
        evt["branch"] = _pr_branch
    _sub_agent = _match_repo_subscription(repo_key, event_type, _pr_branch)
    if not agent_type:
        # 主路由为空 → 订阅兜底
        if _sub_agent:
            agent_type = _sub_agent
            log.info(f"Pipeline: repo subscription matched (fallback) -> agent_type='{_sub_agent}'")
    elif _sub_agent and _sub_agent != agent_type:
        # 双路径并行：主路由 agent 正常处理 + 订阅 Agent 独立分派
        _dispatch_subscription_agent(repo_key, evt, event_type, _sub_agent, agent_type)
        log.info(f"Pipeline: subscription double-path -> extra event for '{_sub_agent}' (main='{agent_type}')")
    if not agent_type:
        _reason = "issue_done" if (event_type in ISSUE_EVENT_TYPES or event_type in PR_EVENT_TYPES) else "no_route"
        log.info(f"Pipeline: no route for type={event_type} ({_reason}), skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, _reason)
        return result

    # 3.1 Issue 生命周期阶段初始化（D8/D9）：issue_opened 建 vod 阶段；其余命中 vod 且无记录时补建
    from src.api.session_store import set_issue_stage
    if event_type == "issue_opened":
        _rid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        if _rid:
            _asg = _fetch_issue_assignee(repo_key, _rid)
            set_issue_stage(repo_key, _rid, "vod", "vod", assignee=_asg)
            log.info(f"Pipeline: issue_state init vod for {_rid} assignee={_asg or 'none'}")
    elif agent_type == "vod":
        from src.api.session_store import get_issue_stage
        _rid2 = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "")
        if _rid2 and not get_issue_stage(repo_key, _rid2):
            set_issue_stage(repo_key, _rid2, "vod", "vod")
            log.info(f"Pipeline: issue_state init vod for {_rid2}")

    # 3.2 任务中心全覆盖（task-center-issue-coverage-design.md §3-A）：
    # Issue 事件幂等建档；closed 动作自动完成对应任务（P5 webhook 兜底）。
    # 旁路：失败仅告警，不阻塞事件处理。
    try:
        from src.api.routes.tasks import ensure_issue_task, close_issue_tasks
        _iid3 = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "") or ""
        if _iid3:
            _num3 = str(_iid3).lstrip("#")
            if _num3.isdigit() and int(_num3) > 0:
                _p3 = evt.get("payload", {}) or {}
                _o3 = _p3.get("object_attributes", {}) or {}
                _act3 = str(_o3.get("action", "") or "").lower()
                _closed = _act3 in ("close", "closed") or str(_o3.get("state", "") or "").lower() == "closed"
                if _closed:
                    _n3 = close_issue_tasks(repo_key, int(_num3))
                    log.info(f"Pipeline: issue closed -> complete tasks #{_num3} ({_n3} rows)")
                elif event_type in ISSUE_EVENT_TYPES:
                    _asg3 = ""
                    try:
                        _al3 = _p3.get("assignees") or []
                        if isinstance(_al3, list) and _al3 and isinstance(_al3[0], dict):
                            _asg3 = _al3[0].get("username") or _al3[0].get("login") or ""
                    except Exception:
                        pass
                    if not _asg3:
                        try:
                            _asg3 = _fetch_issue_assignee(repo_key, _iid3) or ""
                        except Exception:
                            pass
                    _t3 = str(_o3.get("title", "") or "") if isinstance(_o3, dict) else ""
                    ensure_issue_task(repo_key, int(_num3), title=_t3, assignee=_asg3, event_type=event_type)
                    log.info(f"Pipeline: ensure task #{_num3} assignee={_asg3 or '(owner)'}")
    except Exception as _e:
        log.warning(f"Pipeline: task-center sync failed: {_e}")

    # 3.3 Agent 启用检查（阶段路由后的最终 agent）
    from src.api.session_store import is_agent_enabled
    if not is_agent_enabled(repo_key, agent_type):
        log.info(f"Pipeline: agent '{agent_type}' disabled for repo={repo_key}, skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "agent_disabled", agent_type)
        return result

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    backend = None
    runtime_id = 0
    skill_name = ""
    skill_id = 0
    skill_path = ""
    repo_name = repo_key  # repo_key 带平台前缀，devops_agent_session/events 统一用带前缀值
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row:
                runtime_id = row[0] or 0
                backend = ""
                if row[0]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend = brow[0]
                skill_id = row[2] or 0
                skill_name = row[1] or ""
                # skill_id 优先：关联 skill 表拿 skill_name + skill_path
                if skill_id:
                    from src.user.db import get_skill_by_id
                    _sk = get_skill_by_id(skill_id)
                    if _sk:
                        skill_name = _sk.get("skill_name") or skill_name
                        skill_path = _sk.get("skill_path") or skill_name
                log.info(f"Pipeline: agent session found, backend={backend}, skill={skill_name or '(default)'}, skill_id={skill_id}")
    except Exception as e:
        log.warning(f"Pipeline: agent session lookup failed: {e}")

    if not backend or not skill_name:
        # 从默认映射配置文件加载 skill（会话缺 skill 时回退默认映射，防事件卡 running）
        try:
            _defaults_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
            if os.path.exists(_defaults_path):
                with open(_defaults_path, "r", encoding="utf-8") as _f:
                    _defaults = json.load(_f)
                _scene = _defaults.get("scenes", {}).get("default", {})
                # 读仓库开发场景（managed_repos.dev_scene），按场景读映射——skill-dev 仓库回退到 skill-creator 而非 implementation
                try:
                    from src.user.db import get_repo_scene
                    _scene_id = get_repo_scene(repo_key) or "default"
                    _scene = _defaults.get("scenes", {}).get(_scene_id, _defaults.get("scenes", {}).get("default", {}))
                except Exception:
                    pass
                _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                _default_skill = _mapping.get(agent_type, "")
                if _default_skill:
                    backend = ""
                    skill_name = _default_skill
                    log.info(f"Pipeline: using default skill for {agent_type}={_default_skill}")
                else:
                    log.info(f"Pipeline: no agent session and no default skill for {agent_type}, skip")
                    return result
            else:
                log.info(f"Pipeline: no agent session for repo={repo_name}, agent={agent_type}, skip")
                return result
        except Exception as e:
            log.warning(f"Pipeline: failed to load default skill config: {e}")
            return result

    # 5. 提取 Issue ID
    payload = evt.get("payload", {})
    if event_type in ("note_hook_pr", "pull_request_opened", "pull_request_updated", "pull_request_merged"):
        # PR 类事件：仅用 resolve_pr_issue_id（title/分支/issues 列表/PR 详情 API）。
        # 不用 resolve_issue_id——GitCode MR payload 的 object_attributes.iid 是 PR 号，
        # 会误当 issue_id（_is_pr_payload 只识别带 pull_request/issues 键的 payload）。
        try:
            from src.user.db import get_agent_bot_token
            _pr_api_token = get_agent_bot_token(repo_key) or ""
            _pr_issue = resolve_pr_issue_id(payload, _pr_api_token)
            if _pr_issue:
                issue_id = _pr_issue
                log.info(f"Pipeline: PR 关联 Issue -> {issue_id}")
            else:
                issue_id = evt.get("issue_id", "")
                log.info(f"Pipeline: PR 无关联 Issue，issue_id={issue_id or '(none)'}")
        except Exception as e:
            log.warning(f"Pipeline: resolve PR issue failed: {e}")
            issue_id = evt.get("issue_id", "")
    else:
        issue_id = resolve_issue_id(payload) or evt.get("issue_id", "")
    title = evt.get("title", "") or event_type
    log.info(f"Pipeline: issue_id={issue_id or '(none)'}, title={title}")

    # 5.5 构建摘要描述
    actor = evt.get("actor", "")
    branch = evt.get("branch", "")
    if event_type == "issue_opened":
        summary = f"新 Issue {issue_id}: {title}" if issue_id else f"新 Issue: {title}"
    elif event_type == "issue_updated":
        summary = f"Issue {issue_id} 已更新" if issue_id else "Issue 已更新"
    elif event_type == "issue_comment":
        summary = f"@{actor} 评论了 Issue {issue_id}" if issue_id else f"@{actor} 发表了评论"
    elif event_type == "note_hook_pr":
        summary = f"@{actor} 评论了 PR {issue_id}" if issue_id else f"@{actor} 评论了 PR"
    elif event_type == "pull_request_opened":
        summary = f"新 PR {issue_id}: {title}" if issue_id else f"新 PR: {title}"
    elif event_type == "pull_request_updated":
        summary = f"PR {issue_id} 已更新" if issue_id else "PR 已更新"
    elif event_type == "pull_request_merged":
        summary = f"PR {issue_id} 已合并: {title}" if issue_id else f"PR 已合并: {title}"
    elif event_type == "push":
        summary = f"@{actor} 推送到 {branch}" if branch else f"@{actor} 推送了代码"
    elif event_type == "tag_created":
        summary = f"创建标签: {title}" if title else "创建新标签"
    elif event_type == "release_created":
        summary = f"发布 Release: {title}" if title else "发布新 Release"
    elif event_type == "build_start":
        summary = "构建开始"
    elif event_type == "build_failure":
        summary = f"构建失败: {title}" if title else "构建失败"
    elif event_type == "deploy_start":
        summary = "部署开始"
    elif event_type == "deploy_failure":
        summary = "部署失败"
    elif event_type == "issue_labeled":
        summary = f"Issue {issue_id} 已打标签" if issue_id else "Issue 已打标签"
    elif event_type == "vod_signal":
        summary = f"VOD 信号: {title}" if title else "VOD 信号"
    elif event_type == "a2a_event":
        summary = f"A2A: {actor} → {agent_type}"
    else:
        summary = title or event_type
    log.info(f"Pipeline: summary={summary}")

    # 6. 立即创建 event（status=running），Agent 执行完后再刷新 result/status/agent_log
    from src.api.event_store import append_event
    seq = append_event(
        repo_key=repo_key,
        event_type=event_type,
        title=title,
        issue_id=issue_id or "#0",
        source=source,
        agent_type=agent_type,
        actor=actor,
        summary=summary,
        payload=payload,
        status="running",
    )
    log.info(f"Pipeline: event created, seq={seq}, repo_key={repo_key}, agent={agent_type}, issue_id={issue_id or '#0'}")

    from src.repo_key import from_repo_key
    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": from_repo_key(repo_key), "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})

    # 执行分派：从引擎集合选引擎（专属→全局→本机，engine-selection-design.md v2）；
    # 选中 remote → 入队该引擎（queue_remote）；选中本机 → direct 就地执行。
    from src.runtime.engine_select import pick_engine, engine_name
    from src.runtime.service import runtime_execution_kind, enqueue_task, EXEC_MODE_DIRECT
    engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0)
    if not engine_id:
        log.info(f"Pipeline: no available engine for repo={repo_key} agent={agent_type}, seq={seq}, skip")
        _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
        return result
    # backend 必须取已注册引擎的类型名（不沿用会话/字面默认），确保执行 CLI 与选中引擎一致
    _be = engine_name(engine_id) or backend
    kind = runtime_execution_kind(engine_id)
    if kind != EXEC_MODE_DIRECT and _be and skill_name and seq > 0:
        _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                               skill_id, skill_path, engine_id, issue_id)
    elif _be and skill_name and seq > 0:
        evt["_skill_path"] = skill_path  # 供 prompt 构建使用（skill_id 解析出的真实路径）
        evt["_skill_id"] = skill_id
        # 先创建 agent_tasks + 设 started_at，防 sweeper 在排队期间误杀
        _direct_tid = _create_direct_task(repo_key, seq, agent_type, _be, skill_name,
                            skill_path, engine_id, evt, issue_id, skill_id=skill_id)
        start_direct_agent(_execute_agent, (
            repo_key, seq, agent_type, _be, skill_name, evt, engine_id, _direct_tid,
        ), name=f"ag-{agent_type}-{seq}")
        log.info(f"Pipeline: started agent execution, seq={seq}, repo={repo_key}, agent={agent_type}")

    return result


def _fetch_issue_assignee(repo_key: str, issue_id: str) -> str:
    """调 GitCode API 获取 Issue 责任人（assignee.login）。失败返回空。

    用于 pipeline 责任人显示（issue_state.assignee），取代"从事件 payload 猜 assignee"。
    """
    num = _issue_num_from(issue_id)
    if not num:
        return ""
    try:
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)
        import urllib.request
        from src.user.db import get_gitcode_token
        tok = get_gitcode_token(repo_key=repo_key) or ""
        if not tok:
            return ""
        req = urllib.request.Request(
            f"https://api.gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
            headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
        import json as _json
        d = _json.load(urllib.request.urlopen(req, timeout=15))
        asg = d.get("assignee") or {}
        login = asg.get("login", "") if isinstance(asg, dict) else ""
        return login or ""
    except Exception:
        return ""


def _issue_num_from(issue_id: str) -> str:
    """"#5" / "5" → "5"；#0/空 → ''（无关联 Issue，按 repo 级兜底）。"""
    s = (issue_id or "").strip().lstrip("#")
    return s if s and s != "0" else ""


def get_agent_workdir(repo_key: str, agent_type: str, issue_id: str = "") -> str:
    """Agent 工作目录（按 Issue 统一隔离，见 workdir-issue-centric-design.md §二）。

    - 有关联 Issue：#5 → {owner}/{repo}/issues/5/{agent_type}/
    - 无关联 Issue（push/#0）→ {owner}/{repo}/{agent_type}-agent（兼容现状）
    """
    from src.repo_key import split_owner_repo
    owner, rname = split_owner_repo(repo_key)
    num = _issue_num_from(issue_id)
    if num:
        return os.path.join(WORKSPACE_DIR, owner, rname, "issues", num, agent_type)
    return os.path.join(WORKSPACE_DIR, owner, rname, f"{agent_type}-agent")


def _fail_event(seq: int, reason: str):
    """事件入队/启动失败 → 立即置 failed 终态（防卡 running 僵尸）。

    事件在 append_event 时即为 running，若后续入队/启动失败而不更新，
    事件将永远 running（无任务支撑、无详细记录）。所有失败路径统一走这里收口。
    """
    if not seq:
        return
    try:
        from datetime import datetime, timezone
        from src.api.event_store import update_event_complete
        update_event_complete(seq, "failed", exit_code=1,
                              completed_at=datetime.now(timezone.utc).isoformat(),
                              result=reason)
        log.info(f"Pipeline: event {seq} -> failed ({reason[:80]})")
    except Exception as e:
        log.warning(f"Pipeline: _fail_event failed for seq={seq}: {e}")


def _enqueue_pipeline_task(repo_key, seq, agent_type, backend, skill_name, evt,
                           skill_id, skill_path, runtime_id, issue_id=""):
    """触发点三连的入队侧（M2，见 agent-runtime-event-tech-impl.md §2.3）：
    init_event_json → enqueue_task（确定路由 runtime_id）。本地 worker/远端 RuntimeAgent 消费。
    """
    from src.api.event_archive import init_event_json
    from src.runtime.service import enqueue_task, get_execution_mode
    from src.runtime.instances import runtime_instance_name
    from src.repo_key import from_repo_key
    import os as _os

    issue_id = issue_id or evt.get("issue_id", "") or "#0"
    # 1) 确定路由：派发已选定具体引擎（runtime_id>0）；为 0 说明派发失败，直接 fail
    #    （不再走 agent_type 级引擎池 / opencode 兜底，见 engine-selection-design.md v2）
    if not runtime_id:
        _fail_event(seq, "入队失败：无可用引擎（runtime_id 为空）")
        return
    # 引擎实例名（`{type}-{device}`，与 UI 同源）：runtime_id 反查，查不到回退类型名。
    # 注意：engine_select v2 派发已选定具体引擎（runtime_id>0），无 pool_mode 路径；
    # 历史 pool 模式（runtime_id=0 + pool_agent_type）由 service.claim 兼容处理。
    inst_name = runtime_instance_name(runtime_id) or backend or ""
    # 2) event.json 骨架（#0 跳过，见 event_archive._extract_num）
    _evt_copy = dict(evt)
    _evt_copy["issue_id"] = issue_id
    init_event_json(repo_key, seq, _evt_copy, agent_type, runtime=inst_name, skill=skill_name)

    repository = evt.get("repository", from_repo_key(repo_key))
    ws = get_agent_workdir(repo_key, agent_type, issue_id)
    _os.makedirs(ws, exist_ok=True)

    # 3) 会话：配置会话（repo+agent）+ 执行会话（Issue 级，串行粒度，见 multi-issue-isolation-design.md §3.3）
    #    每 Issue 独立 session（见 vod-agent-hang-rootcause-fix.md 方案 A）：有关联 Issue 只用 Issue 会话；
    #    repo 级会话仅用于无关联 Issue（push/#0）。
    from src.api.session_store import get_or_create_agent_session, get_or_create_issue_session
    from src.api.event_store import update_event_config
    update_event_config(seq, inst_name, skill_name, datetime.now(timezone.utc).isoformat())
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    oc_session_id = session.get("runtime_session_id") or ""
    issue_session_id = ""
    if _issue_num_from(issue_id):
        _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
        issue_session_id = _isess.get("id") or ""
        oc_session_id = _isess.get("runtime_session_id") or ""

    repo_gitcode_token = ""
    try:
        from src.user.db import get_gitcode_token
        repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception:
        pass

    # 4) 构建 prompt（含技能占位符化，见 skill-delivery-remote.md）
    try:
        from src.prompt_builder import build_prompt
        evt["_skill_path"] = skill_path
        prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                              skill_path=skill_path, runtime_id=runtime_id, agent_type=agent_type)
    except Exception as e:
        log.warning(f"Pipeline enqueue: prompt build failed: {e}")
        _fail_event(seq, f"入队失败：prompt 构建异常 {str(e)[:150]}")
        return

    env_extra = {"REPO_FULL_NAME": repository, "ACCOUNT": evt.get("account") or repository}
    _inject_cloud_credentials(env_extra, repo_key)
    # skill 发布（v4.2/v4.5）：仅 sre 任务、按仓库注入平台令牌 + 发布目标仓库
    if agent_type == "sre":
        # v4.5：评论指定仓库（「到 owner/repo」/「to owner/repo」）覆盖默认目标
        _apply_comment_repo_override(env_extra, evt)
    # coding agent 注入 BOT_USERNAME（create_skill_pr.sh 设置审查人用）
    if agent_type == "coding":
        try:
            from src.session_manager import db, db_lock
            from src.repo_key import to_repo_key
            _rk = to_repo_key(repo_key)
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? AND bot_username!='' LIMIT 1", (_rk,))
                row = c.fetchone()
                _bot_name = row[0] if row else ""
            if not _bot_name:
                from src.repo_key import split_owner_repo
                _o, _r = split_owner_repo(repo_key)
                _bot_name = _o
            env_extra["BOT_USERNAME"] = _bot_name
        except Exception:
            pass
    _issue_num = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "")
    if _issue_num:
        env_extra["ISSUE_NUMBER"] = str(_issue_num).lstrip("#")
    _pr_num = evt.get("payload", {}).get("pr_number", "")
    if _pr_num:
        env_extra["PR_NUMBER"] = str(_pr_num)

    # 方案 D 防御：有关联 Issue 时，Issue 级会话指针不得指向其他 Issue 的 workdir（防 cwd/session 错位回归）
    try:
        _num = _issue_num_from(issue_id)
        if _num and oc_session_id:
            from src.repo_key import split_owner_repo
            _o, _r = split_owner_repo(repo_key)
            _expected_ws = os.path.join(WORKSPACE_DIR, _o, _r, "issues", _num, agent_type)
            if ws != _expected_ws:
                log.warning(f"Pipeline enqueue: cwd/session mismatch, issue={issue_id} ws={ws} expected={_expected_ws}")
    except Exception:
        pass

    try:
        tid = enqueue_task(
            agent_session_id=session.get("id") or "",
            runtime_id=runtime_id,
            event_seq=seq, issue_id=issue_id,
            repo_key=repo_key, agent_type=agent_type, skill=skill_name,
            prompt=prompt, cwd=ws,
            runtime_session_id=oc_session_id,
            issue_session_id=issue_session_id,
            event_type=evt.get("type", ""),
            account=evt.get("account") or "",
            repo_gitcode_token=repo_gitcode_token,
            env_extra=env_extra,
            skill_path=skill_path,
            skill_id=skill_id,
        )
    except Exception as e:
        log.warning(f"Pipeline enqueue: enqueue_task failed for seq={seq}: {e}")
        _fail_event(seq, f"入队失败：enqueue_task 异常 {str(e)[:150]}")
        return
    log.info(f"Pipeline: enqueued task={tid} seq={seq} repo={repo_key} agent={agent_type} engine={runtime_id} issue_session={issue_session_id or '-'} mode={get_execution_mode()}")


def _inject_cloud_credentials(env_extra: dict, repo_key: str) -> dict:
    """按仓库 owner 注入华为云 AK/SK 到任务环境变量（多用户隔离）。

    cloud-deploy 技能读取 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK 环境变量；
    AK/SK 按 user_accounts.username（=GitCode login）分别存储，
    仓库归谁所有就用谁的凭证（repo_owner → user_accounts）。
    owner 未配置时注入空值，由技能侧报"请仓库 owner 配置华为云凭证"。
    """
    try:
        from src.repo_key import split_owner_repo
        owner, _repo = split_owner_repo(repo_key)
        if not owner:
            env_extra.setdefault("HUAWEICLOUD_SDK_AK", "")
            env_extra.setdefault("HUAWEICLOUD_SDK_SK", "")
            return env_extra
        from src.user.db import get_user_account
        acc = get_user_account(owner) or {}
        ak = (acc.get("huawei_cloud_ak") or "").strip()
        sk = (acc.get("huawei_cloud_sk") or "").strip()
        env_extra["HUAWEICLOUD_SDK_AK"] = ak
        env_extra["HUAWEICLOUD_SDK_SK"] = sk
        if not (ak and sk):
            log.warning(f"cloud credentials: repo owner '{owner}' has no Huawei Cloud AK/SK configured, injected empty")
    except Exception as e:
        log.warning(f"cloud credentials injection failed for {repo_key}: {e}")
        env_extra.setdefault("HUAWEICLOUD_SDK_AK", "")
        env_extra.setdefault("HUAWEICLOUD_SDK_SK", "")
    return env_extra


def _apply_comment_repo_override(env_extra: dict, evt: dict) -> None:
    """v4.5：评论里指定发布目标仓库（「同步到 signway/xxx」/「to owner/repo」）→ 覆盖默认。"""
    import re
    note = ""
    payload = evt.get("payload") or {}
    if isinstance(payload, dict):
        note = str(payload.get("object_attributes", {}).get("note") or "")
    if not note:
        return
    m = re.search(r"(?:到|同步到|push\s*到|to)\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", note)
    if m:
        repo = m.group(1)
        # 按平台覆盖（评论提到 github/gitcode 时精确覆盖；否则同时覆盖）
        low = note.lower()
        if "github" in low:
            env_extra["GITHUB_PUBLISH_REPO"] = repo
        if "gitcode" in low:
            env_extra["GITCODE_PUBLISH_REPO"] = repo
        if "github" not in low and "gitcode" not in low:
            env_extra["GITCODE_PUBLISH_REPO"] = repo
            env_extra["GITHUB_PUBLISH_REPO"] = repo
    # v4.6：评论指定 ClawHub 组织发布者（「发布到组织 @my-org」/「owner: my-org」/「ownerHandle: xxx」）
    mo = re.search(r"(?:组织\s*@|owner(?:handle)?\s*[:：]\s*@?)([A-Za-z0-9_.-]+)", note, re.IGNORECASE)
    if mo:
        env_extra["CLAWHUB_OWNER_HANDLE"] = mo.group(1)


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"


def _execute_agent(repo_key: str, seq: int, agent_type: str,
                   backend: str, skill_name: str, evt: dict, runtime_id: int = 0,
                   task_id: str = ""):
    """后台执行 Agent：构建 prompt → stream_agent → 更新 event（result/status/agent_log）
    
    顶层 try/except 兜底：防止线程内未捕获异常导致事件永久卡在 running
    （如 runtime_instance_name / skill 加载 / stream_agent 启动前崩溃）。
    """
    try:
        _execute_agent_inner(repo_key, seq, agent_type, backend, skill_name, evt, runtime_id, task_id)
    except Exception as e:
        import traceback
        _tb = traceback.format_exc()
        log.error(f"Exec agent: [Agent:{agent_type}|repo:{repo_key}] UNHANDLED CRASH: {e}\n{_tb}")
        from datetime import datetime, timezone
        try:
            from src.api.event_store import update_event_complete
            update_event_complete(seq, "failed", exit_code=1,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=f"执行崩溃：{e}")
            _update_direct_task(seq, "failed", exit_code=1, result_text=f"执行崩溃：{e}")
        except Exception:
            pass
    finally:
        # 清理 Future 记录
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES.pop(seq, None)


def _execute_agent_inner(repo_key: str, seq: int, agent_type: str,
                         backend: str, skill_name: str, evt: dict, runtime_id: int = 0,
                         task_id: str = ""):
    """_execute_agent 的实际逻辑（被顶层 try/except 包裹）。"""
    from src.runtime.executor import stream_agent
    from src.prompt_builder import build_prompt
    from src.api.event_store import (
        update_event_config, update_event_status, update_event_complete, evaluate_agent_output,
        append_skipped_event,
    )
    from src.runtime.instances import runtime_instance_name

    # 引擎实例名（`{type}-{device}`，与 UI 同源）；查不到回退类型名
    inst_name = runtime_instance_name(runtime_id) or backend or ""

    from src.repo_key import from_repo_key
    repository = evt.get("repository", from_repo_key(repo_key))
    issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""
    ws = get_agent_workdir(repo_key, agent_type, issue_id)
    os.makedirs(ws, exist_ok=True)

    # 读取上次的 oc_session_id（用于延续对话上下文）——Issue 级执行会话优先，回退 repo 级
    # 注意：有关联 Issue 时只用该 Issue 自己的会话（每 Issue 独立 session，见 vod-agent-hang-rootcause-fix.md 方案 A）；
    #       repo 级 session 仅用于无关联 Issue（push/#0）。
    repo_name = repo_key
    oc_session_id = ""
    issue_session_id = ""
    _has_issue = bool(_issue_num_from(issue_id))
    try:
        if _has_issue:
            from src.api.session_store import get_or_create_issue_session
            _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
            issue_session_id = _isess.get("id") or ""
            oc_session_id = _isess.get("runtime_session_id") or ""
        if not _has_issue and not oc_session_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT runtime_session_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
                row = c.fetchone()
                if row and row[0]:
                    oc_session_id = row[0]
    except Exception:
        pass

    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] type={agent_type}, backend={inst_name}, skill={skill_name}, oc_session={oc_session_id[:20] if oc_session_id else 'none'}")

    # 获取 bot token（统一走 get_gitcode_token：devops_agent_session → managed_repos → 全局）
    repo_gitcode_token = ""
    try:
        from src.user.db import get_gitcode_token
        repo_gitcode_token = get_gitcode_token(repo_key=repo_name) or ""
    except Exception as e:
        log.warning(f"Exec agent: get bot token failed: {e}")
    log.info(f"Exec agent: bot_token={'set' if repo_gitcode_token else 'none'}")

    # Agent 显示名（注入 AGENT_NAME 环境变量，skill 中 [$AGENT_NAME] 使用）
    agent_name = get_agent_display_name(agent_type)

    try:
        prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                              skill_path=evt.get("_skill_path", ""), runtime_id=runtime_id, agent_type=agent_type)
        # 直接模式（direct）：project-info 服务端本地可达，替换占位符为权威路径
        try:
            from src.repo_key import split_owner_repo
            _o, _r = split_owner_repo(repo_name)
            _pi_path = os.path.join(WORKSPACE_DIR, _o, _r, "project-info")
            prompt = prompt.replace("{{PROJECT_INFO_DIR}}", _pi_path)
        except Exception:
            pass
        # 直接模式（direct）：技能路径占位符替换
        # skill_id 优先：按主键查 skill 表拿 file_path / get_skill_path
        # - file_path 非空（OBS 技能）→ 从 OBS 拉 zip 解压到 .skills/ → 替换 {{SKILL_DIR}}
        # - file_path 空（本地技能）→ get_skill_path 返回绝对路径 → 替换 {{SKILL_DIR}}
        # - 无 skill_id / stale → 回退老逻辑（_skill_path dirname 或 executor SKILLS_DIR 兜底）
        try:
            from src.user.db import get_skill_by_id, get_skill_path
            _skill_id = evt.get("_skill_id") or 0
            if _skill_id:
                _sk = get_skill_by_id(_skill_id)
                if _sk:
                    _okey = _sk.get("file_path") or ""
                    if _okey:
                        # OBS 技能：解压到 cwd/.skills/（zip 根 = {skill_name}/，落地 .skills/{skill_name}/SKILL.md）
                        from src.storage import get_skill_zip_bytes
                        _zip = get_skill_zip_bytes(_okey)
                        if _zip:
                            import zipfile as _zf, io as _io
                            _skill_base = os.path.join(ws, ".skills")
                            os.makedirs(_skill_base, exist_ok=True)
                            with _zf.ZipFile(_io.BytesIO(_zip)) as zf:
                                zf.extractall(_skill_base)
                            prompt = prompt.replace("{{SKILL_DIR}}", _skill_base)
                    else:
                        # 本地技能：get_skill_path 返回 SKILLS_DIR/xxx/SKILL.md
                        # dirname(dirname(...)) = SKILLS_DIR（prompt 拼成 SKILLS_DIR/xxx/SKILL.md）
                        _full = get_skill_path(_skill_id)
                        if _full and os.path.exists(_full):
                            prompt = prompt.replace("{{SKILL_DIR}}", os.path.dirname(os.path.dirname(_full)))
            else:
                # 向后兼容：无 skill_id 时走老逻辑
                _real_skill_dir = os.path.dirname(evt.get("_skill_path", "")) if evt.get("_skill_path") else ""
                if _real_skill_dir:
                    prompt = prompt.replace("{{SKILL_DIR}}", _real_skill_dir)
        except Exception:
            pass
        log.info(f"Exec agent: prompt built, {len(prompt)} chars, skill={skill_name or agent_type}: {prompt[:2000]}")
    except Exception as e:
        log.warning(f"Exec agent: prompt build failed: {e}")
        append_skipped_event(repo_key, evt.get("type", ""), "prompt_build_failed", str(e))
        _fail_event(seq, f"执行失败：prompt 构建异常 {str(e)[:150]}")
        return

    exec_account = evt.get("account") or repository
    if not repo_gitcode_token:
        log.warning(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] no bot token available, agent will fail on API calls")

    # 记录执行配置（执行完成有产出后统一写入 event）
    started_at = datetime.utcnow().isoformat()
    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] spawning {inst_name}")

    exit_code = -1
    final_text = ""
    new_oc_session_id = ""
    tool_uses = []
    agent_logs = []
    env_extra = {
        "REPO_FULL_NAME": repository,
        "ACCOUNT": exec_account,
    }
    _inject_cloud_credentials(env_extra, repo_key)
    # skill 发布（v4.2/v4.5）：仅 sre 任务、按仓库注入平台令牌 + 发布目标仓库
    # （direct 模式同样需要——A2A 交办 sre 走本路径，见 _enqueue_pipeline_task 同款逻辑）
    if agent_type == "sre":
        # v4.5：评论指定仓库（「到 owner/repo」/「to owner/repo」）覆盖默认目标
        _apply_comment_repo_override(env_extra, evt)
    # coding agent 注入 BOT_USERNAME（create_skill_pr.sh 设置审查人用）
    if agent_type == "coding":
        try:
            from src.session_manager import db, db_lock
            from src.repo_key import to_repo_key
            _rk = to_repo_key(repo_key)
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? AND bot_username!='' LIMIT 1", (_rk,))
                row = c.fetchone()
                _bot_name = row[0] if row else ""
            if not _bot_name:
                from src.repo_key import split_owner_repo
                _o, _r = split_owner_repo(repo_key)
                _bot_name = _o
            env_extra["BOT_USERNAME"] = _bot_name
        except Exception:
            pass
    # 事件级扩展环境变量（如 onboarding 的 ONBOARDING_RESULT / ONBOARDING_TARGET）
    _ext = evt.get("_env_extra") or {}
    if isinstance(_ext, dict):
        env_extra.update(_ext)
    _issue_num = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "")
    if _issue_num:
        env_extra["ISSUE_NUMBER"] = str(_issue_num).lstrip("#")
    _pr_num = evt.get("payload", {}).get("pr_number", "")
    if _pr_num:
        env_extra["PR_NUMBER"] = str(_pr_num)

    # git worktree（coding/qa/sre，direct 模式；见 multi-issue-isolation-design.md §3.4.1）
    try:
        from src.runtime import repogit
        _gnum = repogit.issue_num_from(issue_id)
        if repogit.is_git_agent(agent_type) and _gnum and issue_session_id:
            _wt = repogit.ensure_issue_worktree(repo_key, agent_type, _gnum, token=repo_gitcode_token)
            if _wt:
                env_extra["GIT_ASKPASS"] = os.path.join(_wt, ".git-askpass.sh")
    except Exception as e:
        log.warning(f"Exec agent: git worktree ensure failed: {e}")

    try:
        for evt_type, data in stream_agent(
            prompt, ws, session_id=oc_session_id, event_type=evt.get("type", ""),
            backend_name=backend, account=exec_account, bot_token=repo_gitcode_token,
            agent_name=agent_name,
            agent_label=f"[Agent:{agent_type}|repo:{repo_name}|engine:{inst_name}]",
            env_extra=env_extra,
        ):
            # 写库兜底脱敏：即使上游 executor 已脱敏，此处再防御一次（agent 可能在 text/result 复述 token）
            _msg = str(data)[:500]
            if repo_gitcode_token:
                _msg = _msg.replace(repo_gitcode_token, "[REDACTED]")
            agent_logs.append({"time": datetime.utcnow().isoformat(), "type": evt_type, "msg": _msg})
            # 实时推送：direct 模式复用 worker 的 report_messages（落库 task_messages + fanout SSE）
            # 让前端「实时日志」面板在 hermes 执行期间就能看到逐步输出，不用等执行完成
            if task_id and evt_type != "done":
                try:
                    from src.runtime.service import report_messages
                    report_messages(task_id, [{"type": evt_type, "data": data}])
                except Exception as _re:
                    log.warning(f"direct report_messages failed (task={task_id}): {_re}")
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                new_oc_session_id = d.get("oc_session_id", "")
                log.info(f"Agent done: [Agent:{agent_type}|repo:{repo_name}] exit={exit_code}, text_len={len(final_text)}, new_oc_session={new_oc_session_id[:20] if new_oc_session_id else 'none'}")
            elif evt_type == "error":
                log.warning(f"Agent error: [Agent:{agent_type}|repo:{repo_name}] {data}")
            elif evt_type == "step_start":
                log.info(f"[agent {agent_type}|repo:{repo_name}] {data}")
            elif evt_type == "tool_use":
                tool_uses.append(data if isinstance(data, dict) else {})
                log.info(f"[agent {agent_type}|repo:{repo_name}] TOOL: {str(data)[:160]}")
            elif evt_type == "text":
                log.info(f"[agent {agent_type}|repo:{repo_name}] TEXT: {str(data)[:160]}")
            elif evt_type == "reasoning":
                log.info(f"[agent {agent_type}|repo:{repo_name}] REASONING: {str(data)[:160]}")
            else:
                log.info(f"[agent {agent_type}|repo:{repo_name}] {evt_type}: {str(data)[:160]}")
    except Exception as e:
        log.warning(f"Exec agent failed: [Agent:{agent_type}|repo:{repo_name}] {e}")

    # 更新 oc_session_id（持久化对话上下文）——Issue 级执行会话优先，回退 repo 级
    if new_oc_session_id and new_oc_session_id != oc_session_id:
        try:
            if issue_session_id:
                from src.api.session_store import update_issue_session
                update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id)
            # 本函数内其他分支有局部 import db_lock（1157 行）→ 会遮蔽模块级名；
            # 此处必须局部重新绑定，否则 NameError（2026-08-20 修复 e7a93a5b 副作用）
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE devops_agent_session SET runtime_session_id=?, last_active_at=datetime('now') WHERE repo_key=? AND agent_type=?",
                          (new_oc_session_id, repo_name, agent_type))
                db.commit()
        except Exception as e:
            log.warning(f"Exec agent: failed to update oc_session_id: {e}")

    # 判定 Agent 是否有明确答复产出
    has_output, result_text = evaluate_agent_output(agent_type, tool_uses, final_text)
    if repo_gitcode_token and result_text:
        result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
    completed_at = datetime.utcnow().isoformat()
    duration = None
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(completed_at)
        duration = round((e - s).total_seconds(), 1)
    except Exception:
        pass

    if not has_output:
        # 无产出 → 标记 failed（保留 event，不删）
        log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] no output, mark failed")
        _update_direct_task(seq, "failed", exit_code=exit_code, result_text=result_text)
        update_event_status(seq, "failed")
        update_event_complete(seq, "failed", exit_code=exit_code, duration=duration,
                              agent_logs=agent_logs, completed_at=completed_at, result=result_text)
        return

    # 有产出 → 更新该 event（写入配置 + 结果 + 结论 + 执行日志）
    new_status = "completed" if exit_code == 0 else "failed"
    _update_direct_task(seq, new_status, exit_code=exit_code, result_text=result_text)
    update_event_config(seq, inst_name, skill_name, started_at)
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at, result=result_text)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}")

    # 执行完成 → 自动回帖到 Issue（仅 sre 等有实质产出的 Agent，且有关联 Issue）
    if new_status == "completed" and result_text and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            _post_result_to_issue(repository, _issue_num, result_text, repo_gitcode_token, agent_type)

    # 执行完成 → 同步 agent 结果到任务会话（用户「去对话」可见 agent 澄清/报告）
    # 独立于 _post_result_to_issue：agent 可能自己用 curl 发了 Issue 评论，
    # 但 task conversation 仍需写入才能在 Chat 页展示。
    if new_status == "completed" and result_text and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num:
            try:
                from src.repo_key import to_repo_key
                from src.session_manager import db as _db, db_lock as _dl, add_message as _am
                _rk = to_repo_key(repo_key)
                with _dl:
                    _c = _db.cursor()
                    _c.execute("SELECT conversation_id FROM tasks WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                               (_rk, _issue_num))
                    _row = _c.fetchone()
                if _row and _row[0]:
                    _am(_row[0], "agent", f"[{agent_type.upper()} Agent] {result_text[:3000]}")
                    log.info(f"Exec agent: synced result to task conversation {_row[0]} (issue #{_issue_num})")
            except Exception as _e:
                log.warning(f"Exec agent: sync to task conversation failed: {_e}")

    # 执行完成 → 从 GitCode Issue 拉取 agent 发的评论，同步到任务会话
    # （agent 通过技能 curl 直接在 Issue 上发的澄清追问等评论，上面 result_text 同步的是
    #   结构化输出摘要，不含 Issue 上的实际评论内容；此处补拉 Issue 评论去重写入会话）
    if new_status == "completed" and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            _sync_issue_comments_to_conversation(repo_key, _issue_num, repo_gitcode_token, agent_type, repository)


def _post_result_to_issue(repository: str, issue_number, result_text: str, bot_token: str, agent_type: str):
    """Agent 执行完成后，将结果作为评论发回 GitCode Issue，并同步写入任务会话（去对话可见）。"""
    if not repository or not issue_number or not bot_token:
        return
    try:
        import urllib.request, json as _json
        _comment = f"[{agent_type.upper()} Agent] {result_text[:3000]}"
        _data = _json.dumps({"body": _comment}).encode("utf-8")
        _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments"
        _req = urllib.request.Request(
            _url, data=_data,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(_req, timeout=15)
        log.info(f"Exec agent: posted result comment to {repository}/issues/{issue_number}")
    except Exception as e:
        log.warning(f"Exec agent: failed to post result comment: {e}")
    # 同步写入任务会话：用户「去对话」时可见 agent 澄清/报告
    try:
        from src.repo_key import to_repo_key
        from src.session_manager import db, db_lock, add_message
        rk = to_repo_key(repository)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT conversation_id FROM tasks WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                      (rk, issue_number))
            row = c.fetchone()
        if row and row[0]:
            add_message(row[0], "agent", f"[{agent_type.upper()} Agent] {result_text[:3000]}")
            log.info(f"Exec agent: synced result to task conversation {row[0]}")
    except Exception as e:
        log.warning(f"Exec agent: sync to task conversation failed: {e}")


def _sync_issue_comments_to_conversation(repo_key: str, issue_number, bot_token: str,
                                         agent_type: str = "", repository: str = ""):
    """从 GitCode Issue 拉取 agent 发的评论，去重后同步到任务会话。

    VOD 等 agent 通过技能 curl 直接在 Issue 上发澄清追问评论，这些评论内容
    不会出现在 result_text（结构化输出摘要）中。此函数补拉 Issue 上的实际评论，
    与会话已有消息去重后写入，确保 Chat Agent「去对话」可见完整内容。

    agent_type 为空时同步所有 agent 前缀评论（用于打开会话时全量补拉）。
    """
    import urllib.request
    import re
    import json as _json
    try:
        from src.repo_key import to_repo_key
        from src.session_manager import db as _db, db_lock as _dl, add_message as _am, get_messages as _get_msgs
    except Exception:
        return

    rk = to_repo_key(repo_key)

    # 1) 查关联的 task conversation
    try:
        with _dl:
            c = _db.cursor()
            c.execute("SELECT conversation_id FROM tasks WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                      (rk, issue_number))
            row = c.fetchone()
        if not row or not row[0]:
            return
        conv_id = row[0]
    except Exception as e:
        log.warning(f"Sync issue comments: find conversation failed: {e}")
        return

    # 2) 从 GitCode API 拉取 Issue 评论
    try:
        if not repository:
            from src.repo_key import from_repo_key
            repository = from_repo_key(rk)
        api_url = f"https://api.gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments"
        req = urllib.request.Request(api_url)
        req.add_header("Authorization", f"Bearer {bot_token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            comments = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"Sync issue comments: fetch from GitCode failed: {e}")
        return

    if not comments:
        return

    # 3) 获取会话已有消息，提取内容指纹用于去重
    try:
        existing_msgs = _get_msgs(conv_id, limit=100)
    except Exception:
        existing_msgs = []
    existing_sigs = set()
    # 匹配 [XXX Agent] 标记（可能在引用块 > 之后出现，不要求在 body 开头）
    _agent_tag_re = re.compile(r'\[([A-Za-z_]+)\s+Agent\]', re.IGNORECASE)
    for m in existing_msgs:
        content = m.get("content", "")
        # 去掉引用块和 [XXX Agent] 标记后取前 80 字符做指纹
        sig = _agent_tag_re.sub("", content).strip()[:80].lower()
        existing_sigs.add(sig)

    # 4) 筛选含 [XXX Agent] 标记的评论，去重后写入会话
    synced = 0
    for cm in comments:
        body = cm.get("body", "")
        if not body:
            continue
        # 评论中必须包含 [XXX Agent] 标记（可能在引用块之后）
        tag_match = _agent_tag_re.search(body)
        if not tag_match:
            continue
        # 如果指定了 agent_type，只同步该 agent 的评论
        if agent_type:
            if tag_match.group(1).upper() != agent_type.upper():
                continue
        # 去重：去掉 [XXX Agent] 标记后取前 80 字符做指纹（大小写不敏感）
        sig = _agent_tag_re.sub("", body).strip()[:80].lower()
        if sig in existing_sigs:
            continue
        try:
            _am(conv_id, "agent", body[:4000])
            synced += 1
        except Exception:
            pass

    if synced > 0:
        log.info(f"Sync issue comments: synced {synced} comments to conversation {conv_id} (issue #{issue_number})")
