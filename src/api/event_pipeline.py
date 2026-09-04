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


def is_plugin_mode() -> bool:
    """Check if pipeline is in plugin mode.

    plugin mode: uses pipelines.json + tag-based enqueue/claim
    legacy mode: uses agent-skill-mapping.json + runtime_id binding

    Priority: meta table `pipeline_mode` > env var GRAPE_PIPELINE_MODE > default (plugin)
    """
    try:
        from src.user.db import get_meta_value
        _mode = (get_meta_value("pipeline_mode") or "").strip().lower()
        if _mode:
            return _mode == "plugin"
    except Exception:
        pass
    return os.environ.get("GRAPE_PIPELINE_MODE", "plugin").lower() == "plugin"


def resolve_skill_from_pipeline(repo_key: str, agent_type: str, scene_id: str = "", user_id: int = 0) -> str:
    """Look up skill name from DB (plugin mode).

    scene_id 非空时优先使用（Issue 级场景），否则回退到仓库默认场景。
    user_id > 0 时可查用户级场景。
    Returns skill name or empty string.
    """
    try:
        from src.api.pipeline_config import get_node_by_tag
        if not scene_id:
            try:
                from src.user.db import get_repo_scene
                scene_id = get_repo_scene(repo_key) or "default"
            except Exception:
                scene_id = "default"
        node = get_node_by_tag(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
        if node:
            return node.get("skill", "")
    except Exception as e:
        log.warning(f"resolve_skill_from_pipeline failed: {e}")
    return ""


def start_direct_agent(fn, args, name=""):
    """提交一个本机 direct 执行任务到有界池（webhook / run_pipeline 共用）。"""
    future = _DIRECT_POOL.submit(fn, *args)
    # 从 args 中提取 seq（约定第一个参数是 repo_key，第二个是 seq）
    seq = args[1] if len(args) > 1 else 0
    if seq:
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES[seq] = future
    return future


def _task_insert_row(_cols, **kw):
    """按 agent_tasks 列清单顺序组装 INSERT 值。

    以列清单为唯一来源，缺列即抛错——防止列/值数目漂移
    （历史事故：_TASK_COLUMNS_LIST 扩到 39 列后，两处字面量仍 37/36 值导致 INSERT 失败，
    事件卡 running 被 sweeper 置 failed，见 docs/new-version3/agent-tasks-direct-create-binding-fix-design.md）。
    """
    missing = [c for c in _cols if c not in kw]
    if missing:
        raise ValueError(f"agent_tasks INSERT 缺少列值: {missing}")
    return tuple(kw[c] for c in _cols)


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

    # 更新 session 的 runtime_id 为实际执行引擎（pick_engine 结果可能与创建时不同）
    if engine_id:
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("UPDATE devops_agent_session SET runtime_id=? WHERE id=?", (engine_id, agent_session_id))
                db.commit()
        except Exception:
            pass

    task_id = "t_" + uuid.uuid4().hex[:12]
    # issue_id 优先用传参，兜底从 evt 解析
    if not issue_id:
        issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""
    from src.repo_key import from_repo_key

    # 获取 bot token（统一走 get_gitcode_token：devops_agent_session → managed_repos → 全局）
    repo_gitcode_token = ""
    try:
        from src.user.db import get_gitcode_token
        repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception as e:
        log.warning(f"create_direct_task: get_gitcode_token failed: {e}")

    try:
        row = _task_insert_row(_TASK_COLUMNS_LIST,
            id=task_id, agent_session_id=agent_session_id, runtime_id=engine_id,
            event_seq=seq, issue_id=issue_id, repo_key=repo_key or "",
            agent_type=agent_type or "", skill=skill_name or "", skill_id=skill_id or 0,
            prompt="", cwd="", runtime_session_id="", event_type="", account="",
            user_id=0, repo_gitcode_token=repo_gitcode_token or "",
            env_extra="{}", skill_files="{}", server_url="", issue_session_id="",
            project_info_files="{}", project_info_meta="{}", knowledge_files="{}",
            project_info_file_urls="{}", knowledge_file_urls="{}", attempts=0,
            pool_agent_type="", affinity_runtime_id=0, pool_tag="", pool_engine_ids="",
            status="queued", exit_code=None, result_text="", errors="", stderr="",
            usage_json="{}", started_at=now, completed_at=None, created_at=now)
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
                f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})", row)
            db.commit()
        log.info(f"Direct task created: {task_id} seq={seq} engine={engine_id}({inst_name}) skill={skill_name}")
    except Exception as e:
        log.warning(f"create_direct_task failed for seq={seq}: {e}")
        return ""
    return task_id


def _create_failed_task(repo_key, seq, agent_type, backend, skill_name,
                        skill_path, skill_id, evt, issue_id="", runtime_id=0):
    """引擎不可用时创建 failed 状态的 task 快照（供重派复用参数）。

    重派逻辑查 agent_tasks WHERE event_seq=?，无记录则报"无关联任务"。
    此函数确保引擎不可用时也有一条 failed task 记录，重派可走"终态→复用快照重建"路径。
    """
    from src.runtime.service import _TASK_COLUMNS_LIST
    from src.api.session_store import get_or_create_agent_session
    from datetime import datetime, timezone
    import os

    # 查 agent_session_id（重派需要）
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"create_failed_task: no agent_session_id for {repo_key}/{agent_type}")
        return

    task_id = "t_" + uuid.uuid4().hex[:12]
    if not issue_id:
        issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""

    # 构建 prompt（重派复用）
    prompt = ""
    try:
        from src.prompt_builder import build_prompt
        evt["_skill_path"] = skill_path
        evt["_skill_id"] = skill_id
        prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                              skill_path=skill_path, runtime_id=runtime_id, agent_type=agent_type)
    except Exception as e:
        log.warning(f"create_failed_task: prompt build failed: {e}")

    # cwd
    try:
        cwd = get_agent_workdir(repo_key, agent_type, issue_id)
        os.makedirs(cwd, exist_ok=True)
    except Exception:
        cwd = ""

    # env_extra
    repository = evt.get("repository", "")
    env_extra = {"REPO_FULL_NAME": repository, "ACCOUNT": evt.get("account") or repository}
    _inject_cloud_credentials(env_extra, repo_key)

    # repo_gitcode_token
    repo_gitcode_token = ""
    try:
        from src.user.db import get_gitcode_token
        repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    _reason = json.dumps(["无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎"][:500])
    _pool_tag = agent_type or "" if is_plugin_mode() else ""
    try:
        row = _task_insert_row(_TASK_COLUMNS_LIST,
            id=task_id, agent_session_id=agent_session_id, runtime_id=runtime_id,
            event_seq=seq, issue_id=issue_id, repo_key=repo_key or "",
            agent_type=agent_type or "", skill=skill_name or "", skill_id=skill_id or 0,
            prompt=prompt, cwd=cwd, runtime_session_id="",
            event_type=evt.get("type", "") or "",
            account=repository or evt.get("account", "") or "", user_id=0,
            repo_gitcode_token=repo_gitcode_token,
            env_extra=json.dumps(env_extra), skill_files="{}", server_url="", issue_session_id="",
            project_info_files="{}", project_info_meta="{}", knowledge_files="{}",
            project_info_file_urls="{}", knowledge_file_urls="{}", attempts=0,
            pool_agent_type=_pool_tag, affinity_runtime_id=0, pool_tag=_pool_tag, pool_engine_ids="",
            status="failed", exit_code=1, result_text="", errors=_reason, stderr="",
            usage_json="{}", started_at=now, completed_at=now, created_at=now)
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
                f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})", row)
            db.commit()
        log.info(f"Failed task snapshot created: {task_id} seq={seq} agent={agent_type} (engine_unavailable)")
    except Exception as e:
        log.warning(f"create_failed_task failed for seq={seq}: {e}")


def _update_direct_task(seq, status, exit_code=0, result_text=""):
    """direct 执行完成后更新 agent_tasks 状态（通过 event_seq 反查）。"""
    try:
        from datetime import datetime, timezone
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, completed_at=? "
                "WHERE event_seq=? AND status IN ('queued','running','dispatched')",
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
        # PR updated 事件 + stage=coding：检查是否有 QA 历史记录
        # 如果 QA 曾经审查过（说明 coding 是被 QA 回流后的修复），PR updated 应路由到 QA 复审
        if event_type == "pull_request_updated" and stage == "coding":
            try:
                from src.session_manager import db, db_lock
                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "SELECT 1 FROM events WHERE repo_key=? AND issue_id=? AND agent_type='qa' LIMIT 1",
                        (repo_key, issue_id))
                    if c.fetchone():
                        return "qa"  # QA 有历史 → 路由到 QA 复审
            except Exception:
                pass
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
            # Plugin mode: try pipelines.json first
            if is_plugin_mode():
                _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type)
                if _pipe_skill:
                    skill_name = _pipe_skill
                    log.info(f"Pipeline subscription (plugin): skill for {agent_type}={_pipe_skill}")
            if not skill_name:
                # Legacy fallback: agent-skill-mapping.json
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
        # 从 tasks 表获取 Issue 真实标题（避免 Pipeline 列表显示 "A2A (订阅): xxx"）
        _sub_title = ""
        _sub_iss_num = str(issue_id).lstrip("#")
        if _sub_iss_num and _sub_iss_num.isdigit():
            try:
                from src.api.routes.tasks import _get_issue_title
                _sub_title = _get_issue_title(repo_key, int(_sub_iss_num)) or ""
            except Exception:
                pass
        if not _sub_title:
            _sub_title = f"A2A (订阅): {agent_type}"
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=_sub_title, issue_id=issue_id, source="a2a",
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
            from src.runtime.engine_select import _repo_user_id
            _sub_uid = _repo_user_id(repo_key)
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id, user_id=_sub_uid)
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
            # 多任务模式豁免：bot 创建的子任务 issue_opened（body 含「## 父任务」）不跳过
            _allow_through = False
            if event_type == "issue_opened":
                try:
                    _payload = evt.get("payload", {}) or {}
                    _oa = _payload.get("object_attributes", {}) or {}
                    _body = _oa.get("description", "") or ""
                    if "## 父任务" in _body:
                        _allow_through = True
                except Exception:
                    pass
            if _allow_through:
                log.info(f"Pipeline: bot={actor} created subtask issue, allow through")
            else:
                log.info(f"Pipeline skip: self-comment from bot={actor}")
                return result
        log.info(f"Pipeline: comment from {actor}, not a bot ({bot_usernames})")

    # 2.0a 系统回帖标记检测：含 <!-- grape-auto-comment --> 的评论是系统自动回帖的回声，跳过
    if event_type in ("issue_comment", "note_hook_pr"):
        try:
            _payload = evt.get("payload", {}) or {}
            _oa = _payload.get("object_attributes", {}) or {}
            _note = str(_oa.get("note") or _payload.get("body") or "")
            if "<!-- grape-auto-comment -->" in _note:
                log.info(f"Pipeline skip: system auto-comment marker detected, event={event_type}")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "system_auto_comment")
                return result
        except Exception as e:
            log.warning(f"Pipeline: auto-comment marker check failed: {e}")

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
            # 子任务识别：Issue body 含 "## 父任务" + Issue 链接 → 标记 parent_issue_id
            _parent = _parse_parent_issue_from_payload(evt.get("payload", {}) or {})
            if _parent:
                try:
                    from src.api.session_store import set_parent_issue
                    set_parent_issue(repo_key, _rid, _parent)
                    log.info(f"Pipeline: subtask detected, issue={_rid} parent={_parent}")
                except Exception:
                    pass
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
                    if not _t3:
                        _t3 = evt.get("title", "") or ""
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

    # 提前解析 issue_id + 场景（所有路径都需要，pick_engine 用 _issue_scene 选引擎）
    _issue_scene = "default"
    _issue_scene_uid = 0
    _early_issue_id = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "")
    if not _early_issue_id and event_type in PR_EVENT_TYPES:
        _early_issue_id = resolve_pr_issue_id(evt.get("payload", {}) or {}) or ""
    if _early_issue_id:
        try:
            from src.api.session_store import get_issue_scene
            _issue_scene, _issue_scene_uid = get_issue_scene(repo_key, _early_issue_id)
            _issue_scene = _issue_scene or "default"
        except Exception:
            pass
    # scene_user_id=0 时兜底反查（旧数据 is_join=0 导致 scene_user_id 未写入）
    if not _issue_scene_uid:
        try:
            from src.runtime.engine_select import _repo_user_id
            _issue_scene_uid = _repo_user_id(repo_key)
        except Exception:
            pass
    log.info(f"Pipeline: scene resolved -> scene_id={_issue_scene} uid={_issue_scene_uid} issue={_early_issue_id or '(none)'}")
    # Issue 级场景优先：scene 非 default 时，用 issue scene 的 skill 覆盖 session 的 skill
    if is_plugin_mode() and _issue_scene and _issue_scene != "default":
        _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
        if _pipe_skill:
            skill_name = _pipe_skill
            # 同步更新 skill_id 和 skill_path，否则执行时仍用旧 skill_id 查到旧 skill
            from src.user.db import resolve_skill_id, get_skill_by_id, get_skill_path
            _new_sid = resolve_skill_id(_pipe_skill)
            if _new_sid:
                skill_id = _new_sid
                _sk = get_skill_by_id(_new_sid)
                skill_path = (_sk.get("skill_path") or _pipe_skill) if _sk else _pipe_skill
            else:
                skill_path = _pipe_skill
                skill_id = 0
            log.info(f"Pipeline (plugin): issue scene skill override: {agent_type}={_pipe_skill} sid={skill_id} (scene={_issue_scene})")
    if not backend or not skill_name:
        # Plugin mode: try pipelines.json first（用 Issue 级场景）
        if is_plugin_mode():
            _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
            if _pipe_skill:
                skill_name = _pipe_skill
                backend = backend or ""
                log.info(f"Pipeline (plugin): skill for {agent_type}={_pipe_skill} (scene={_issue_scene})")
        if not skill_name:
            # Legacy fallback: agent-skill-mapping.json（用 Issue 级场景）
            try:
                _defaults_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
                if os.path.exists(_defaults_path):
                    with open(_defaults_path, "r", encoding="utf-8") as _f:
                        _defaults = json.load(_f)
                    _scene = _defaults.get("scenes", {}).get(_issue_scene, _defaults.get("scenes", {}).get("default", {}))
                    _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                    _default_skill = _mapping.get(agent_type, "")
                    if _default_skill:
                        backend = ""
                        skill_name = _default_skill
                        log.info(f"Pipeline: using default skill for {agent_type}={_default_skill} (scene={_issue_scene})")
                    else:
                        log.info(f"Pipeline: no agent session and no default skill for {agent_type}, skip")
                        return result
                else:
                    log.info(f"Pipeline: no agent session for repo={repo_name}, agent={agent_type}, skip")
                    return result
            except Exception as e:
                log.warning(f"Pipeline: failed to load default skill config: {e}")
                return result

    # 4.5 onboarding 引导上下文（§11 v1.5）：模板一键创建模拟 issue_opened webhook 提交 run_pipeline，
    #     完全走普通链路（广播模式靠 pool_tag 认领，不依赖仓库 runtime_id 绑定）。
    #     事件体 payload 携带 _onboarding={"target","skill","scene","result_path"} → 覆盖技能 + 注入 ONBOARDING_* 环境变量。
    try:
        _onb = (evt.get("payload") or {}).get("_onboarding")
    except Exception:
        _onb = None
    if isinstance(_onb, dict) and (_onb.get("target") or _onb.get("skill")):
        _ob_skill = str(_onb.get("skill", "") or "").strip() or skill_name
        log.info(f"Pipeline onboarding ctx: skill={_ob_skill} target={_onb.get('target')} scene={_onb.get('scene')}")
        if _ob_skill:
            try:
                from src.user.db import resolve_skill_id, get_skill_by_id
                _ob_sid = resolve_skill_id(_ob_skill)
                if _ob_sid:
                    skill_id = _ob_sid
                    _ob_sk = get_skill_by_id(_ob_sid)
                    skill_name = (_ob_sk.get("skill_name") or _ob_skill) if _ob_sk else _ob_skill
                    skill_path = (_ob_sk.get("skill_path") or skill_name) if _ob_sk else skill_name
                else:
                    skill_name = _ob_skill
                    skill_path = _ob_skill
            except Exception as e:
                log.warning(f"Pipeline onboarding skill resolve failed: {e}")
                skill_name = _ob_skill
                skill_path = _ob_skill
        # 环境变量注入（repo-onboarding skill 读取 ONBOARDING_RESULT/TARGET）
        _ob_ext = dict(evt.get("_env_extra") or {})
        if _onb.get("target"):
            _ob_ext["ONBOARDING_TARGET"] = str(_onb["target"])
        if _onb.get("result_path"):
            _ob_ext["ONBOARDING_RESULT"] = str(_onb["result_path"])
        if _onb.get("scene"):
            _ob_ext["ONBOARDING_SCENE"] = str(_onb["scene"])
        evt["_env_extra"] = _ob_ext
        backend = backend or ""
        log.info(f"Pipeline: onboarding ctx applied, skill={skill_name} env={list(_ob_ext.keys())}")

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

    # 5.1 去重检查（2026-08-29 修复）：同 issue + 同 agent_type 已有 queued/running 任务时跳过
    # 原因：每次 issue_comment webhook 都会创建新 VOD 任务，导致同一 issue 产生大量重复任务。
    # A2A 事件已有去重（webhook.py:296），但 issue_opened/issue_comment 没有。
    # 豁免：issue_opened（新 Issue 应该总是创建）；a2a_event（已有自己的去重）
    if issue_id and agent_type and event_type not in ("issue_opened", "a2a_event"):
        try:
            with db_lock:
                _dc = db.cursor()
                _dc.execute(
                    "SELECT seq FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
                    "AND status IN ('running','dispatched') "
                    "AND event_type != 'a2a_event' "
                    "ORDER BY seq DESC LIMIT 1",
                    (repo_key, issue_id, agent_type))
                _existing_evt = _dc.fetchone()
                if _existing_evt:
                    log.info(f"Pipeline: dedup skip — issue={issue_id} agent={agent_type} "
                             f"already has running event seq={_existing_evt[0]}")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, f"dedup:active_{agent_type}_for_{issue_id}")
                    result["skipped"] = "duplicate_active_task"
                    return result
        except Exception as _de:
            log.warning(f"Pipeline: dedup check failed: {_de}")

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
    from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
    from src.runtime.service import runtime_execution_kind, enqueue_task, EXEC_MODE_DIRECT, get_execution_mode
    engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0,
                            scene_id=_issue_scene, user_id=_issue_scene_uid)
    if engine_id is None:
        log.info(f"Pipeline: no available engine for repo={repo_key} agent={agent_type}, seq={seq}, skip")
        # 创建 failed 状态的 task 快照（供重派复用参数，否则重派报"无关联任务"）
        _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                            skill_path, skill_id, evt, issue_id, runtime_id or 0)
        _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
        return result
    # Plugin 模式 + engine_id=0：无主池任务（广播），多个引擎通过 claim_by_tag 竞争认领。
    # 但强制 direct 模式（DEVOPS_EXECUTION_MODE=direct）不走广播，走本机 direct。
    # 场景 engine_runtime_ids 限定广播范围（写入 pool_engine_ids 字段）。
    if engine_id == 0 and is_plugin_mode() and get_execution_mode() != EXEC_MODE_DIRECT:
        _be = backend or "opencode"
        _pool_engine_ids = get_scene_pool_engine_ids(_issue_scene, agent_type, repo_key=repo_key, user_id=_issue_scene_uid)
        log.info(f"Pipeline (plugin broadcast): engine_id=0 pool_tag={agent_type} pool_engine_ids={_pool_engine_ids} seq={seq}")
        if _be and skill_name and seq > 0:
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id,
                                   pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid)
        return result
    # backend 必须取已注册引擎的类型名（不沿用会话/字面默认），确保执行 CLI 与选中引擎一致
    _be = engine_name(engine_id) or backend
    if not _be or not skill_name or seq <= 0:
        return result
    # engine_id=0 且无可用引擎（plugin direct 兜底 / legacy 无绑定）→ 跳过执行，不创建空任务
    if not engine_id:
        log.info(f"Pipeline: engine_id=0 no engine to execute, seq={seq}, skip")
        _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
        return result
    kind = runtime_execution_kind(engine_id)
    if kind != EXEC_MODE_DIRECT and _be and skill_name and seq > 0:
        _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                               skill_id, skill_path, engine_id, issue_id, user_id=_issue_scene_uid)
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
    s = (issue_id or "").strip().lstrip("#")
    return s if s and s != "0" else ""


def _fetch_issue_title_via_api(repo_key: str, issue_id: str) -> str:
    """调 GitCode API 获取 Issue 标题。失败返回空。

    用于 pipeline 展示兜底：当 tasks/events 表中 title 均为占位符时，
    通过 API 获取真实标题并回写。
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
        return d.get("title", "") or ""
    except Exception:
        return ""


def _parse_parent_issue_from_payload(payload: dict) -> str:
    """Parse parent issue ID from Issue body."""
    import re
    body = ""
    obj = payload.get("object_attributes") or payload.get("issue") or {}
    if isinstance(obj, dict):
        body = obj.get("description") or obj.get("body") or ""
    if not body and isinstance(payload, dict):
        body = payload.get("description") or payload.get("body") or ""
    if not body:
        return ""
    m = re.search(r'父任务.{0,30}?#?(\d+)', body, re.I | re.S)
    if m:
        return f"#{m.group(1)}"
    m2 = re.search(r'parent.?issue.{0,30}?#?(\d+)', body, re.I | re.S)
    if m2:
        return f"#{m2.group(1)}"
    return ""


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
    同步更新 agent_tasks 表 status=failed（防止 events 已 failed 但 agent_tasks 仍 running）。
    """
    if not seq:
        return
    try:
        from datetime import datetime, timezone
        from src.api.event_store import update_event_complete
        update_event_complete(seq, "failed", exit_code=1,
                              completed_at=datetime.now(timezone.utc).isoformat(),
                              result=reason)
        # 同步更新 agent_tasks 表（如果有关联的 task）
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE agent_tasks SET status='failed', completed_at=?, "
                "exit_code=COALESCE(exit_code, 1), "
                "errors=COALESCE(errors, ?) "
                "WHERE event_seq=? AND status IN ('running','queued','dispatched')",
                (datetime.now(timezone.utc).isoformat(),
                 json.dumps([reason][:500]) if reason else None, seq)
            )
            db.commit()
            if c.rowcount > 0:
                log.info(f"Pipeline: synced agent_tasks event_seq={seq} -> failed")
        log.info(f"Pipeline: event {seq} -> failed ({reason[:80]})")
    except Exception as e:
        log.warning(f"Pipeline: _fail_event failed for seq={seq}: {e}")


def _enqueue_pipeline_task(repo_key, seq, agent_type, backend, skill_name, evt,
                           skill_id, skill_path, runtime_id, issue_id="", pool_engine_ids="", user_id=0):
    """init_event_json -> enqueue_task (deterministic routing runtime_id).
    Local worker / remote RuntimeAgent consume.

    Plugin mode (GRAPE_PIPELINE_MODE=plugin): sets pool_tag on enqueue
    so workers with matching engine tags can claim via claim_by_tag.
    """
    from src.api.event_archive import init_event_json
    from src.runtime.service import enqueue_task, get_execution_mode
    from src.runtime.instances import runtime_instance_name
    from src.repo_key import from_repo_key
    import os as _os

    issue_id = issue_id or evt.get("issue_id", "") or "#0"
    # 1) 路由判定：
    #    runtime_id>0 → 确定性路由（指定引擎）
    #    runtime_id=0 + plugin 模式 → 无主池任务（广播），设 pool_agent_type 让引擎竞争认领
    #    runtime_id=0 + 传统模式 → 派发失败
    _pool_agent_type = ""
    if not runtime_id:
        if is_plugin_mode():
            _pool_agent_type = agent_type
            log.info(f"Pipeline enqueue (broadcast): runtime_id=0 pool_agent_type={agent_type} seq={seq}")
        else:
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
    # 更新 session 的 runtime_id 为实际执行引擎
    if runtime_id:
        try:
            with db_lock:
                _c = db.cursor()
                _c.execute("UPDATE devops_agent_session SET runtime_id=? WHERE id=?", (runtime_id, session.get("id","")))
                db.commit()
        except Exception:
            pass
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

    from src.prompt_builder import _resolve_server_url
    env_extra = {
        "REPO_FULL_NAME": repository,
        "ACCOUNT": evt.get("account") or repository,
        "GRAPE_SERVER_URL": _resolve_server_url(runtime_id),
    }
    _inject_cloud_credentials(env_extra, repo_key)
    # 事件级扩展环境变量（如 onboarding 的 ONBOARDING_RESULT/TARGET，§11 v1.5）
    _evt_ext = evt.get("_env_extra") or {}
    if isinstance(_evt_ext, dict):
        env_extra.update(_evt_ext)
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
        _pool_tag = agent_type if is_plugin_mode() else ""
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
            user_id=user_id,
            repo_gitcode_token=repo_gitcode_token,
            env_extra=env_extra,
            skill_path=skill_path,
            skill_id=skill_id,
            pool_tag=_pool_tag,
            pool_agent_type=_pool_agent_type,
            pool_engine_ids=pool_engine_ids,
        )
    except Exception as e:
        log.warning(f"Pipeline enqueue: enqueue_task failed for seq={seq}: {e}")
        _fail_event(seq, f"enqueue_task error {str(e)[:150]}")
        return
    _mode_tag = "plugin" if is_plugin_mode() else "legacy"
    log.info(f"Pipeline: enqueued task={tid} seq={seq} repo={repo_key} agent={agent_type} engine={runtime_id} issue_session={issue_session_id or '-'} mode={get_execution_mode()} dispatch={_mode_tag} pool_tag={_pool_tag or '-'}")


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


def _materialize_direct_workdir(ws: str, repo_key: str, agent_type: str, skill_name: str,
                                evt: dict, issue_id: str, prompt: str) -> str:
    """组件4：direct 端统一物化 project-info / skill / knowledge 到工位 ws 下。

    替代旧的「占位符替换为服务端绝对路径（仓库级 project-info 原件 / 全局 SKILLS_DIR）」，
    使 direct 端与 worker 端 cwd 布局一致（.project-info/.skills/.knowledge 均在 ws 内），
    后续 bwrap 锁只需挂 ws，无需放行仓库级目录或全局 SKILLS_DIR。

    与 worker 端 _execute_task 物化语义对齐：
    - .project-info/  ← load_project_info + member_files_pi + agent_files（rel 相对 project-info）
    - .skills/{skill}/ ← OBS zip 解压 或 本地技能目录 copytree
    - .knowledge/      ← knowledge_files（rel 相对 WORKSPACE_DIR，与 worker 原样一致）

    任一步失败仅告警并跳过（不抛异常、不阻断执行），保留其它占位符继续替换。
    """
    import shutil as _shutil

    _issue = issue_id or evt.get("issue_id", "") or (evt.get("payload") or {}).get("issue_number", "") or ""

    # 1) project-info 物化（复用 enqueue_task 打包逻辑）
    proj_dir = os.path.join(ws, ".project-info")
    try:
        from src.runtime.service import load_project_info
        _pif, _pim = load_project_info(repo_key)
        try:
            from src.api.knowledge import member_files_pi, agent_files
            for rel, content in (member_files_pi(repo_key) or {}).items():
                _pif.setdefault(rel, content)
            for rel, content in (agent_files(repo_key, agent_type) or {}).items():
                _pif.setdefault(rel, content)
        except Exception:
            pass
        for rel, content in (_pif or {}).items():
            _dest = os.path.join(proj_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else str(content))
            except Exception:
                pass
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
    except Exception as e:
        log.warning(f"direct project-info materialize failed for {repo_key}: {e}")

    # 2) skill 物化（OBS zip 解压 / 本地技能 copytree 到 ws/.skills/{skill_name}/）
    skill_dir = os.path.join(ws, ".skills")
    try:
        _skill_local = ""  # 本地技能根目录（非空则 copytree）
        from src.user.db import get_skill_by_id, get_skill_path
        _skill_id = evt.get("_skill_id") or 0
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
                if _okey:
                    # OBS 技能：zip 根 = {skill_name}/，解压到 ws/.skills/
                    from src.storage import get_skill_zip_bytes
                    _zip = get_skill_zip_bytes(_okey)
                    if _zip:
                        import zipfile as _zf, io as _io
                        os.makedirs(skill_dir, exist_ok=True)
                        with _zf.ZipFile(_io.BytesIO(_zip)) as zf:
                            zf.extractall(skill_dir)
                else:
                    _full = get_skill_path(_skill_id)
                    if _full and os.path.exists(_full):
                        _skill_local = os.path.dirname(_full)
        else:
            from src.config import resolve_official_skill_path as _resolve_skill_path
            _real_skill_md = _resolve_skill_path(skill_name or "")
            if not _real_skill_md:
                _sp = evt.get("_skill_path", "") or ""
                if _sp and os.path.isfile(_sp):
                    _real_skill_md = _sp
                elif _sp and os.path.isfile(os.path.join(_sp, "SKILL.md")):
                    _real_skill_md = os.path.join(_sp, "SKILL.md")
            if _real_skill_md:
                _skill_local = os.path.dirname(_real_skill_md)
        if _skill_local and skill_name and os.path.isdir(_skill_local):
            _dst = os.path.join(skill_dir, skill_name)
            if not os.path.isdir(_dst):
                try:
                    _shutil.copytree(_skill_local, _dst)
                except Exception:
                    pass
        prompt = prompt.replace("{{SKILL_DIR}}", skill_dir)
    except Exception as e:
        log.warning(f"direct skill materialize failed for {skill_name}: {e}")

    # 3) knowledge 物化（对齐 worker 端 .knowledge，direct 补齐）
    know_dir = os.path.join(ws, ".knowledge")
    try:
        from src.api.knowledge import knowledge_files as _know_files
        from src.runtime.service import _issue_num as _svc_issue_num
        _num = _svc_issue_num(_issue)
        _know = _know_files(repo_key, agent_type=agent_type, issue_num=_num)
        for rel, content in (_know or {}).items():
            _dest = os.path.join(know_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else str(content))
            except Exception:
                pass
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"direct knowledge materialize failed for {repo_key}: {e}")

    return prompt


def _cleanup_direct_workdir(repo_key: str, agent_type: str, issue_id: str = ""):
    """direct 端任务级工位清理：删轻文件（.skills/.project-info/.knowledge）+ 安全卸载 worktree。

    在 _execute_agent 的 finally 中调用，覆盖正常/失败/异常全部路径且幂等。
    清理失败只告警，不影响事件结果上报。
    """
    try:
        ws = get_agent_workdir(repo_key, agent_type, issue_id or "")
        import shutil as _shutil
        for _sub in (".skills", ".project-info", ".knowledge"):
            _p = os.path.join(ws, _sub)
            try:
                if os.path.isdir(_p):
                    _shutil.rmtree(_p, ignore_errors=True)
            except Exception:
                pass
        from src.runtime import repogit
        _num = repogit.issue_num_from(issue_id or "")
        if _num:
            _token = ""
            try:
                from src.user.db import get_gitcode_token
                _token = get_gitcode_token(repo_key=repo_key) or ""
            except Exception:
                pass
            repogit.remove_issue_worktree_if_pushed(repo_key, agent_type, _num, token=_token)
        # 连空 ws 目录一起删（non-git agent 删完物化文件后为空；git agent 的 worktree 已被整目录删掉）
        if os.path.isdir(ws) and not os.listdir(ws):
            _shutil.rmtree(ws, ignore_errors=True)
    except Exception as e:
        log.warning(f"direct workdir cleanup failed for {repo_key}/{agent_type}: {e}")


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
            if task_id:
                from src.runtime.service import update_task_status
                update_task_status(task_id, "failed", exit_code=1, result_text=f"执行崩溃：{e}")
            else:
                _update_direct_task(seq, "failed", exit_code=1, result_text=f"执行崩溃：{e}")
        except Exception:
            pass
    finally:
        # 清理 Future 记录
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES.pop(seq, None)
        # 任务级工位清理（direct 端）：轻文件 + 确认已 push 的 worktree
        try:
            _cleanup_direct_workdir(
                repo_key, agent_type,
                evt.get("issue_id", "") or (evt.get("payload") or {}).get("issue_number", "") or ""
            )
        except Exception:
            pass


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

    # direct 执行开始 → 更新 task status 为 running（direct 路径无 worker task_start 调用）
    if task_id:
        try:
            from src.runtime.service import update_task_status
            update_task_status(task_id, "running")
            log.info(f"direct exec: task {task_id} status -> running")
        except Exception as e:
            log.warning(f"direct exec: failed to update task {task_id} to running: {e}")

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
        # 直接模式（direct）：组件4 统一物化 project-info / skill / knowledge 到 ws 内
        # （替代旧的「占位符替换为服务端绝对路径：仓库级 project-info 原件 / 全局 SKILLS_DIR」），
        # 使 direct 端与 worker 端 cwd 布局一致，后续 bwrap 锁只需挂 ws。
        prompt = _materialize_direct_workdir(
            ws, repo_key, agent_type, skill_name, evt, issue_id, prompt
        )
        log.info(f"Exec agent: prompt built, {len(prompt)} chars, skill={skill_name or agent_type}: {prompt[:2000]}")
        # 回写 prompt 到 agent_tasks（direct 模式创建时 prompt 为空，此处补写）
        log.info(f"Exec agent: prompt writeback check: task_id={task_id!r}, prompt_len={len(prompt)}")
        if task_id and prompt:
            try:
                from src.session_manager import db as _wdb, db_lock as _wdl
                with _wdl:
                    c = _wdb.cursor()
                    c.execute("UPDATE agent_tasks SET prompt=? WHERE id=? AND prompt=''", (prompt, task_id))
                    _wdb.commit()
                log.info(f"Exec agent: prompt writeback OK for task={task_id}, rows={c.rowcount}")
            except Exception as _pe:
                log.warning(f"Exec agent: prompt writeback failed for task={task_id}: {_pe}")
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
                env_extra["GIT_ASKPASS"] = repogit.askpass_path(_wt)
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
                # VOD 场景推荐兜底：解析 JSON 中的 recommended_scene 并持久化
                if agent_type == "vod" and final_text and issue_id:
                    try:
                        from src.api.event_store import parse_agent_output_json
                        _scene_out = parse_agent_output_json(final_text)
                        if _scene_out and _scene_out.get("recommended_scene"):
                            _rec_scene = _scene_out["recommended_scene"]
                            from src.api.session_store import set_issue_scene
                            from src.runtime.engine_select import _repo_user_id
                            _vod_uid = _repo_user_id(repo_key)
                            set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
                            log.info(f"VOD done: recommended_scene={_rec_scene} uid={_vod_uid} set for {issue_id}")
                    except Exception as _se:
                        log.warning(f"VOD done: scene parse failed: {_se}")
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
        if task_id:
            from src.runtime.service import update_task_status
            update_task_status(task_id, "failed", exit_code=exit_code, result_text=result_text)
        else:
            _update_direct_task(seq, "failed", exit_code=exit_code, result_text=result_text)
        update_event_status(seq, "failed")
        update_event_complete(seq, "failed", exit_code=exit_code, duration=duration,
                              agent_logs=agent_logs, completed_at=completed_at, result=result_text)
        return

    # 有产出 → 更新该 event（写入配置 + 结果 + 结论 + 执行日志）
    # exit_code < 0 表示进程被信号终止（如 -15=SIGTERM, -9=SIGKILL），
    # 但 has_output=True 说明 Agent 已完成业务产出（PR/测试/评论/结构化 conclusion），
    # 信号终止常发生在引擎完成输出后的进程清理阶段，不应判为失败。
    if exit_code == 0 or (has_output and exit_code < 0):
        new_status = "completed"
        if exit_code < 0:
            log.info(f"Exec agent: seq={seq} exit_code={exit_code} but has_output=True, mark completed (signal death after work done)")
    else:
        new_status = "failed"
    if task_id:
        from src.runtime.service import update_task_status
        update_task_status(task_id, new_status, exit_code=exit_code, result_text=result_text)
    else:
        _update_direct_task(seq, new_status, exit_code=exit_code, result_text=result_text)
    update_event_config(seq, inst_name, skill_name, started_at)
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at, result=result_text)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}")

    # 多任务模式：父任务 Issue Agent 完成后 → stage=done（不 A2A coding，子任务由 webhook 自动触发）
    if new_status == "completed" and agent_type == "issue" and issue_id:
        try:
            from src.api.session_store import is_subtask_mode_enabled, set_issue_stage
            if is_subtask_mode_enabled(repo_key, issue_id):
                set_issue_stage(repo_key, issue_id, "done", "issue")
                log.info(f"Multi-task mode: parent issue {issue_id} stage=done (subtasks will trigger independently)")
        except Exception as _e:
            log.warning(f"Multi-task mode stage=done check failed: {_e}")

    # 执行完成 → 自动回帖到 Issue（仅 sre 等有实质产出的 Agent，且有关联 Issue）
    if new_status == "completed" and result_text and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            _post_result_to_issue(repository, _issue_num, result_text, repo_gitcode_token, agent_type)

    # SRE 完成后 → 从 result_text 提取部署 URL 并写入 managed_repos.deploy_url
    if new_status == "completed" and agent_type == "sre" and result_text:
        try:
            import re
            _urls = re.findall(r'https?://[\d\w.\-]+(?::\d+)?(?:/)?', result_text)
            if _urls:
                from src.session_manager import db, db_lock
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (_urls[0], repo_key))
                    if c.rowcount > 0:
                        db.commit()
                        log.info(f"SRE deploy_url auto-registered: {_urls[0]} for {repo_key}")
        except Exception as _e:
            log.warning(f"SRE deploy_url extraction failed: {_e}")

    # 子任务 SRE 完成后 → 登记完成情况到父 Issue
    if new_status == "completed" and agent_type == "sre" and issue_id:
        try:
            from src.api.session_store import get_parent_issue, get_subtask_issues
            _parent = get_parent_issue(repo_key, issue_id)
            if _parent and repo_gitcode_token:
                _parent_num = _issue_num_from(_parent)
                _sub_num = _issue_num_from(issue_id)
                if _parent_num and _sub_num:
                    _post_subtask_completion_to_parent(repository, _sub_num, _parent_num, result_text, repo_gitcode_token)
                    # 检查所有子任务是否全部完成
                    _subs = get_subtask_issues(repo_key, _parent)
                    _all_done = all(s.get("stage") == "done" or s.get("issue_id") == issue_id for s in _subs)
                    if _all_done and len(_subs) > 0:
                        _post_all_subtasks_done(repository, _parent_num, _subs, repo_gitcode_token)
        except Exception as _e:
            log.warning(f"Subtask completion post failed: {_e}")

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
        _comment += "\n<!-- grape-auto-comment -->"
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



def _post_subtask_completion_to_parent(repository: str, sub_issue_num: str, parent_issue_num: str,
                                        result_text: str, bot_token: str):
    """子任务完成后，在父 Issue 评论登记完成情况。"""
    try:
        import urllib.request, json as _json
        from src.api.event_store import parse_agent_output_json
        _out = parse_agent_output_json(result_text)
        _summary = (_out.get("conclusion", "") if _out else "") or result_text[:200]
        _comment = f"### \u5b50\u4efb\u52a1 #{sub_issue_num} \u5df2\u5b8c\u6210\n\n{_summary}\n\n---\n\u5b50\u4efb\u52a1\u6d41\u6c34\u7ebf\u5df2\u5168\u90e8\u5b8c\u6210\u3002"
        _comment += "\n<!-- grape-auto-comment -->"
        _data = _json.dumps({"body": _comment}).encode("utf-8")
        _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{parent_issue_num}/comments"
        _req = urllib.request.Request(_url, data=_data,
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(_req, timeout=15)
        log.info(f"Subtask #{sub_issue_num} completion posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post subtask completion failed: {e}")


def _post_all_subtasks_done(repository: str, parent_issue_num: str, subtasks: list, bot_token: str):
    """所有子任务完成后，在父 Issue 发汇总报告。"""
    try:
        import urllib.request, json as _json
        _lines = [f"### \u5168\u90e8\u5b50\u4efb\u52a1\u5df2\u5b8c\u6210\n"]
        for s in subtasks:
            _sid = s.get("issue_id", "?")
            _stage = s.get("stage", "?")
            _status = "\u2705" if _stage == "done" else "\u274c"
            _lines.append(f"- {_status} {_sid} ({_stage})")
        _lines.append(f"\n\u7236\u4efb\u52a1 #{parent_issue_num} \u7684\u6240\u6709\u5b50\u4efb\u52a1\u5df2\u5b8c\u6210\u3002")
        _comment = "\n".join(_lines)
        _comment += "\n<!-- grape-auto-comment -->"
        _data = _json.dumps({"body": _comment}).encode("utf-8")
        _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{parent_issue_num}/comments"
        _req = urllib.request.Request(_url, data=_data,
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(_req, timeout=15)
        log.info(f"All subtasks done summary posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post all subtasks done failed: {e}")

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
    # 系统回帖标记——统一过滤，确保 existing_sigs 和新评论指纹生成方式一致
    _auto_comment_marker = "<!-- grape-auto-comment -->"
    for m in existing_msgs:
        content = m.get("content", "")
        # 统一过滤系统标记 + 去掉 [XXX Agent] 标记后取前 80 字符做指纹
        content = content.replace(_auto_comment_marker, "").rstrip()
        sig = _agent_tag_re.sub("", content).strip()[:80].lower()
        existing_sigs.add(sig)

    # 4) 筛选含 [XXX Agent] 标记的评论，去重后写入会话
    synced = 0
    for cm in comments:
        body = cm.get("body", "")
        if not body:
            continue
        # 过滤系统标记，避免在 Chat 页面显示
        body = body.replace(_auto_comment_marker, "").rstrip()
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
