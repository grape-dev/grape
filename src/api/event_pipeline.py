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


def _fail_scene_all_offline(seq: int, scene_id: str, agent_type: str, context: str = "",
                            repo_key: str = ""):
    """Issue #259 Optional：场景引擎全灭时统一置事件 failed 并告警（5 处调用点共用）。

    调用方在 return 前调用本 helper——helper 负责 log + fail，调用方负责 return / return False。

    引擎绑定真实性同步（docs/weekly-06/engine-binding-truth-sync-design.md §4.4）：
    原裸 update_event_status(seq, "failed") 无原因落库，排障需倒查日志。
    现改走 _fail_event 同款通路：人话原因写 events.result + agent_tasks.errors，
    前端 observability/issues 点开即见。文案含引擎名（engine_name 反查）。
    repo_key 必传（调用方都有）：仓库级场景查询依赖它，缺失时 get_pipeline_config
    会回退官方模板导致引擎名查不到（卡点①修复）。
    """
    _ctx = context or "Pipeline"
    log.warning(f"{_ctx}: scene={scene_id} {agent_type} engines all offline, skip (Issue #259)")
    # 反查场景绑定的引擎名列表，拼人话
    _engine_names = []
    try:
        from src.api.pipeline_config import get_scene_engine_ids
        _eids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo_key) or []
        if _eids:
            from src.runtime.engine_select import engine_name as _en
            _seen = set()
            for _eid in _eids:
                _nm = _en(int(_eid))
                if _nm and _nm not in _seen:
                    _seen.add(_nm)
                    _engine_names.append(_nm)
    except Exception as _e:
        log.warning(f"_fail_scene_all_offline: engine name lookup failed: {_e}")
    if _engine_names:
        _reason = (f"绑定的 {len(_engine_names)} 台引擎（{', '.join(_engine_names)}）全部离线，"
                   f"任务无法派发（scene={scene_id}, agent={agent_type}）")
    else:
        _reason = f"场景绑定的引擎全部离线，任务无法派发（scene={scene_id}, agent={agent_type}）"
    _fail_event(seq, _reason)

def _filter_pr_sub_matches(matches: list, issue_scene_id: str, issue_qa_skill: str = "") -> list:
    """PR 订阅匹配结果三步过滤：
    1. 排除 PR 关联 Issue 所在场景（同场景的 QA 由流水线自己派）
    2. 排除跟 Issue 场景 qa_skill 相同的场景（同 skill 审同 PR 结果一样，无意义）
    3. 剩余场景中 qa_skill 相同的只保留第一个（同 skill 重复审查浪费资源）

    matches: [(scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids), ...]
    issue_scene_id: PR 关联 Issue 所在场景（空=无关联 Issue）
    issue_qa_skill: Issue 所在场景的 qa skill（空=无关联 Issue 或场景无 qa 节点）
    """
    # 1. 排除 Issue 所在场景
    if issue_scene_id:
        matches = [m for m in matches if m[0] != issue_scene_id]
    # 2. 排除跟 Issue 场景 qa_skill 相同的场景
    if issue_qa_skill:
        matches = [m for m in matches if m[1] != issue_qa_skill]
    # 3. 相同 qa_skill 只保留第一个
    seen_skills = set()
    deduped = []
    for m in matches:
        if m[1] not in seen_skills:
            seen_skills.add(m[1])
            deduped.append(m)
    return deduped


def _match_pr_subscription(repo_key: str, evt: dict, event_type: str, pr_branch: str) -> list:
    """PR 无关联 Issue 时，按 PR target_branch 匹配场景 work_branch，唤醒 QA agent。

    支持多个场景命中同一 PR（多 QA 并行审查）。
    返回 [(scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids), ...] 匹配列表。
    """
    if not pr_branch:
        return []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, nodes_json, advanced_options FROM devops_pipeline_scene "
                "WHERE repo_key=? AND pr_subscription_enabled=1 AND is_active=1",
                (repo_key,))
            rows = c.fetchall()
        matches = []
        for scene_id, nodes_json, adv_opts_json in rows:
            # 匹配 work_branch
            import json as _json
            try:
                adv_opts = _json.loads(adv_opts_json or "{}")
            except Exception:
                adv_opts = {}
            work_branch = adv_opts.get("work_branch", "main") or "main"
            if work_branch != pr_branch:
                continue
            nodes = json.loads(nodes_json or "[]")
            qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
            if not qa_node or not qa_node.get("skill"):
                continue
            sre_node = next((n for n in nodes if n.get("tag") == "sre" and n.get("enabled", True)), None)
            sre_skill = sre_node.get("skill", "") if sre_node else ""
            sre_engine_ids = sre_node.get("engine_runtime_ids", []) if sre_node else []
            matches.append((
                scene_id, qa_node["skill"], qa_node.get("engine_runtime_ids", []),
                sre_skill, sre_engine_ids
            ))
        return matches
    except Exception as e:
        log.warning(f"Pipeline: PR subscription match failed: {e}")
        return []


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

    scene_id 非空时优先使用（Issue 级场景），否则回退到仓库默认场景（managed_repos.dev_scene）。
    issue-39 方案 B：无「default」场景——仓库无场景时 scene_id 为空，返回空技能。
    user_id > 0 时可查用户级场景。
    Returns skill name or empty string.
    """
    try:
        from src.api.pipeline_config import get_node_by_tag
        if not scene_id:
            return ""
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
                        skill_path, engine_id, evt, issue_id="", skill_id=0,
                        skip_event_config=False):
    """direct 执行前创建 agent_tasks + 设 started_at，防 sweeper 误杀。

    skip_event_config=True 时跳过 update_event_config（autopilot 场景：不写 events 表）。
    """
    from src.runtime.service import _TASK_COLUMNS_LIST
    from src.api.session_store import get_or_create_agent_session
    from src.runtime.instances import runtime_instance_name
    from datetime import datetime, timezone

    inst_name = runtime_instance_name(engine_id) or backend or ""
    now = datetime.now(timezone.utc).isoformat()

    # 先设 started_at/runtime/skill，让 sweeper 的 no_start 检查跳过
    # autopilot 不写 events 表，跳过这步
    if not skip_event_config:
        from src.api.event_store import update_event_config
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

    # 获取 bot token（按 provider 分流：GitHub 仓库用 get_github_token）
    repo_gitcode_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception as e:
        log.warning(f"create_direct_task: get bot token failed: {e}")

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
            status="queued", stream_ended=0, self_reported=0,
            exit_code=None, result_text="", errors="", stderr="",
            usage_json="{}", started_at=now, completed_at=None, created_at=now,
            user_gitcode_token="")
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
        from src.prompt_builder import build_devops_agent_prompt
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        evt["_skill_path"] = skill_path
        evt["_skill_id"] = skill_id
        prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
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
    from src.repo_key import split_owner_repo as _sor
    _owner, _repo_name = _sor(repository)
    env_extra = {
        "REPO_FULL_NAME": repository,
        "REPO_OWNER": _owner,
        "REPO_NAME": _repo_name,
        "ACCOUNT": evt.get("actor") or evt.get("account") or "",
    }

    # repo_gitcode_token（按 provider 分流）
    repo_gitcode_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
        else:
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
            status="failed", stream_ended=1, self_reported=0,
            exit_code=1, result_text="", errors=_reason, stderr="",
            usage_json="{}", started_at=now, completed_at=now, created_at=now,
            user_gitcode_token="")
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
    """判断是否为 PR 事件 payload（PR webhook 带 pull_request/merge_request 键）。

    Issue #89 修复：旧逻辑 `"issues" in payload` 会误判 Issue Hook（GitCode Issue Hook
    payload 可能含 issues 交叉引用键）为 PR 事件，导致 resolve_issue_id 跳过
    object_attributes 解析 → 子任务 issue_id=#0。
    改为：仅当有 pull_request/merge_request 键，或 object_attributes 含 PR 特有字段
    （source_branch/target_branch）时才判定为 PR。
    """
    if bool(payload.get("pull_request")) or bool(payload.get("merge_request")):
        return True
    obj = payload.get("object_attributes", {})
    if isinstance(obj, dict):
        if obj.get("source_branch") or obj.get("target_branch"):
            return True
        # noteable_type 明确为 Issue → 非 PR
        if obj.get("noteable_type") == "Issue":
            return False
    return False


def resolve_issue_id(payload: dict) -> str:
    """从事件 payload 提取 Issue 编号。

    注意：PR 事件不返回 PR 号（其 object_attributes.iid 是 PR 号，会误当 issue_id）；
    PR 事件的真实关联 Issue 用 resolve_pr_issue_id（title/分支/issues 列表）。

    Issue #89 修复：Issue Hook payload 可能含 issues 交叉引用键（父任务），
    旧逻辑先查 issues 列表 → 返回父任务编号而非当前 Issue。
    修正：非 PR 事件优先查 object_attributes，issues 列表仅作 PR 事件兜底。
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
    # Issue #89: 非 PR 事件优先查 object_attributes（当前 Issue 编号）
    # 旧逻辑先查 issues 列表 → 对含交叉引用的 Issue Hook 返回父任务编号
    _is_pr = _is_pr_payload(payload)
    if not _is_pr:
        obj = payload.get("object_attributes", {})
        if isinstance(obj, dict):
            num = obj.get("number", obj.get("iid", obj.get("noteable_iid", "")))
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
    # PR 事件的 object_attributes 不查（其 iid 是 PR 号，会误当 issue_id）
    return ""


def resolve_pr_issue_id(payload: dict, api_token: str = "", repo_key: str = "") -> str:
    """从 PR 事件 payload 解析关联的 Issue 编号。
    优先级：payload 标题 #N → 分支名 issue-N → 同步调 PR 详情 API。

    Issue #252: 当 issue 与 PR 编号相同时，通过 entity_type 区分——
    从文本提取编号时检查 URL 路径：/pulls/N 是 PR 自引用（跳过），/issues/N 是关联 Issue（采用）。
    """
    import re, json, urllib.request
    from src.api.event_store import _extract_issue_id_from_text, _extract_entity_type_from_text

    # PR 自身编号（用于自引用检测）
    obj = payload.get("object_attributes", {}) or {}
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    _pr_self_num = str(obj.get("iid", obj.get("number", "")) or pr_obj.get("number", "")).lstrip("#")

    # 1. 从 object_attributes / pull_request 的 title 提取 #N
    title = obj.get("title", "") or pr_obj.get("title", "")
    issue = _extract_issue_id_from_text(title)
    if issue:
        _et = _extract_entity_type_from_text(title)
        _issue_num = issue.lstrip("#")
        # Issue #252: 跳过 PR 自引用（编号==PR自身编号 + URL路径为 /pulls/）
        if _et == "pull_request" and _issue_num == _pr_self_num:
            pass  # 自引用，继续找下一个
        else:
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
                if _is_github_repo(repo_key):
                    url = f"https://api.github.com/repos/{repo_path}/pulls/{pr_number}"
                    hdrs = {"Authorization": f"Bearer {api_token}",
                            "Accept": "application/vnd.github+json",
                            "User-Agent": "Grape-DevOps"}
                else:
                    url = f"https://gitcode.com/api/v5/repos/{repo_path}/pulls/{pr_number}"
                    hdrs = {"Authorization": f"Bearer {api_token}"}
                req = urllib.request.Request(url, headers=hdrs)
                data = json.loads(urllib.request.urlopen(req, timeout=10).read())
                # title / head.ref
                _api_title = str(data.get("title", ""))
                issue = _extract_issue_id_from_text(_api_title)
                if issue:
                    _et = _extract_entity_type_from_text(_api_title)
                    _issue_num = issue.lstrip("#")
                    # Issue #252: 跳过 PR 自引用
                    if _et == "pull_request" and _issue_num == _pr_self_num:
                        pass
                    else:
                        return issue
                head = data.get("head", {}) or {}
                m = re.search(r'issue[-_](\d+)', str(head.get("ref", "")))
                if m and m.group(1) != "0":
                    return f"#{m.group(1)}"
            except Exception:
                return ""
    return ""


# L2 阶段分派映射表（Issue #49 §5.3）：stage × 事件类型 → Agent
# - 对应列 stage 命中 → 目标 Agent；"—" 表示该阶段下该事件不分发（返回空）
# - 特例：PR updated + stage=coding + QA 有历史 → qa（复审）
_STAGE_EVENT_AGENT_MAP = {
    "issue_opened":        {"vod": "vod", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "issue_comment":       {"vod": "vod", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "issue", "done": ""},
    # close 已由 L1 副作用短路，此处安全兜底：close 一律不分发（issue_updated 其余 action 归 issue）
    "issue_updated":       {"vod": "issue", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "issue_labeled":       {"vod": "vod", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "pull_request_opened": {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
    "pull_request_updated": {"vod": "issue", "issue": "issue", "coding": "qa", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
    "pull_request_merged": {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "sre", "sre": "sre", "pending_review": "", "done": ""},
    "note_hook_pr":        {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
}


def _resolve_issue_stage_agent(repo_key: str, event_type: str, payload: dict,
                               default_agent: str) -> str:
    """阶段矩阵路由（D9，见 vod-skill-upgrade-plan.md §3.0.3）——表驱动（Issue #49 §5.3）。

    - issue_opened 恒给 vod（D8）
    - 其余按 issue_state.stage × event_type 查 `_STAGE_EVENT_AGENT_MAP`
    - 特例：pull_request_updated + stage=coding + QA 有历史 → qa 复审
    - 无 issue_state / 非关联事件 → default_agent（现状 get_domain_agent 结果）
    - stage=done → 一律不分发
    """
    from src.api.session_store import get_issue_stage

    if event_type == "issue_opened":
        return "vod"

    if event_type not in ISSUE_EVENT_TYPES and event_type not in PR_EVENT_TYPES:
        return default_agent

    # 解析关联 issue_id：PR 类事件优先用 title/分支提取（无需 token），Issue 类用 payload 字段
    if event_type in PR_EVENT_TYPES:
        issue_id = resolve_pr_issue_id(payload, repo_key=repo_key) or resolve_issue_id(payload) or ""
    else:
        issue_id = resolve_issue_id(payload) or ""
    if not issue_id:
        return default_agent

    st = get_issue_stage(repo_key, issue_id)
    if not st:
        # 无记录：issue_opened 已由上面处理；其余关联事件默认 vod（新 Issue 先审视）
        return "vod" if event_type in ISSUE_EVENT_TYPES else default_agent
    stage = st["stage"]

    # Issue #51: cancelled 任务不再分发（防止取消后 A2A 继续流转）
    from src.session_manager import db as _ck_db, db_lock as _ck_lock
    try:
        _ck_issue_num = str(issue_id).lstrip("#")
        if _ck_issue_num.isdigit():
            with _ck_lock:
                _ck_c = _ck_db.cursor()
                _ck_c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=? AND status='cancelled' LIMIT 1",
                              (repo_key, int(_ck_issue_num)))
                if _ck_c.fetchone():
                    return ""
    except Exception as _ck_err:
        log.warning(f"Pipeline: cancelled-task check failed for {repo_key}/{issue_id}: {_ck_err} — defaulting to no-dispatch (safe fail)")
        return ""

    # 表驱动查找；显式列缺失视为不分发
    _row = _STAGE_EVENT_AGENT_MAP.get(event_type)
    _agent = (_row or {}).get(stage, "") if _row else ""

    # pending_review → 审核点 owner（issue-comment-stage-owner-routing-design.md §3.1）：
    # 评论应回给「设审核点/当前 stage owner」的 agent（如 QA 设待审核则由 QA 收口 review-pass/reject）
    if stage == "pending_review" and _agent:
        _owner = (st.get("owner_agent") or "")
        if _owner in ("vod", "issue", "coding", "qa", "sre"):
            _agent = _owner

    # 特例已由 _STAGE_EVENT_AGENT_MAP 表驱动处理：
    # pull_request_updated + coding → qa（weekly-02 修复 Issue #46：QA↔coding 回流闭环）
    # 无需额外 QA 历史记录检查

    return _agent


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


def _dispatch_pr_subscription_extra(repo_key: str, evt: dict, event_type: str, match: tuple):
    """多场景命中同一 PR 时，为额外场景并行分发 QA 任务。

    match = (scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids)
    """
    try:
        scene_id, qa_skill, qa_engine_ids, _, _ = match
        # 构造 evt 副本，写入额外场景信息
        _extra_evt = dict(evt)
        _extra_evt["_pr_sub_scene_id"] = scene_id
        _extra_evt["_pr_sub_skill"] = qa_skill
        _extra_evt["_pr_sub_engine_ids"] = qa_engine_ids
        # 提取 PR 号用于回帖
        _payload = evt.get("payload", {}) or {}
        _pr_obj = _payload.get("pull_request", {}) or _payload.get("merge_request", {}) or {}
        _obj_attrs = _payload.get("object_attributes", {}) or {}
        _pr_num = _pr_obj.get("number", _pr_obj.get("iid", "")) or _obj_attrs.get("iid", "")
        if _pr_num:
            _extra_evt["_pr_sub_pr_number"] = _pr_num
        # 构造伪 issue_id（带 scene_id 区分）
        _pseudo_id = f"#pr_{_pr_num}_{scene_id}" if _pr_num else ""
        # 创建事件
        from src.api.event_store import append_event
        _title = f"PR 订阅 QA: {scene_id}"
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=_title, issue_id=_pseudo_id, source="a2a",
            agent_type="qa", actor=evt.get("actor", ""),
            summary=f"PR 订阅并行: scene={scene_id} → qa",
            payload=evt.get("payload", {}), status="running",
            scene_id=scene_id,
            entity_type="pull_request",
        )
        log.info(f"Pipeline: PR subscription extra dispatch seq={seq} scene={scene_id}")
        # 分派执行：跟主场景 run_pipeline 一致的判断逻辑
        # engine_id=0 + plugin + auto(非direct) → broadcast enqueue（远端worker认领）
        # 仅 direct 模式或选中具体本机引擎时走 DIRECT
        from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
        _engine_id = pick_engine(repo_key, "qa", fallback_runtime_id=0, scene_id=scene_id)
        if _engine_id is None:
            log.info(f"Pipeline: PR subscription extra: no engine for scene={scene_id}, skip")
            from src.api.event_store import update_event_status
            update_event_status(seq, "failed")
            return
        _be = engine_name(_engine_id) or ""
        _is_broadcast = (_engine_id == 0 and is_plugin_mode()
                         and get_execution_mode() != EXEC_MODE_DIRECT)
        if _is_broadcast:
            # 复用 _enqueue_pipeline_task：自动构建 prompt/env_extra/会话/init_event_json
            # Issue #259：get_scene_pool_engine_ids 已按健康过滤；None = 场景引擎全灭（方案A，不入队）
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")
        else:
            _kind = runtime_execution_kind(_engine_id)
            if _kind == EXEC_MODE_DIRECT:
                _extra_evt["_skill_path"] = qa_skill
                _extra_evt["_skill_id"] = 0
                _direct_tid = _create_direct_task(repo_key, seq, "qa", _be, qa_skill,
                                    qa_skill, _engine_id, _extra_evt, _pseudo_id, skill_id=0)
                start_direct_agent(_execute_agent, (
                    repo_key, seq, "qa", _be, qa_skill, _extra_evt, _engine_id, _direct_tid
                ))
            else:
                _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
                if _pool_ids is None:
                    _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub else-branch", repo_key=repo_key)
                    return
                _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                       0, qa_skill, _engine_id, _pseudo_id,
                                       pool_engine_ids=_pool_ids or "")
    except Exception as e:
        log.warning(f"Pipeline: PR subscription extra dispatch failed: {e}")


def _dispatch_pr_subscription_redispatch(evt: dict, seq: int, scene_id: str) -> bool:
    """PR 订阅事件重派：在原事件上直接重新创建 task，不创建新事件。

    用于 redispatch 路径 — 当 PR 订阅事件的 task 派发失败（如远程 enqueue 报错）、
    需要手动重派时调用。不能走 A2A 端点（payload 是 webhook 原始格式，无 issue_number）。

    返回 True = 派发成功，False = 引擎不可用或无 QA 节点。
    """
    try:
        from src.session_manager import db, db_lock
        import json as _json

        repo_key = evt.get("repo_key", "") or ""

        # 从 devops_pipeline_scene 查 scene_id 对应的 QA 节点 skill
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, nodes_json FROM devops_pipeline_scene "
                "WHERE repo_key=? AND scene_id=? AND is_active=1",
                (repo_key, scene_id))
            row = c.fetchone()
        if not row:
            log.warning(f"Pipeline: PR subscription redispatch: scene={scene_id} not found")
            return False

        nodes = json.loads(row[1] or "[]")
        qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
        if not qa_node or not qa_node.get("skill"):
            log.warning(f"Pipeline: PR subscription redispatch: no QA node in scene={scene_id}")
            return False

        qa_skill = qa_node["skill"]
        qa_engine_ids = qa_node.get("engine_runtime_ids", [])

        # 提取 PR 号
        _payload = evt.get("payload", {}) or {}
        if isinstance(_payload, str):
            _payload = _json.loads(_payload)
        _pr_obj = _payload.get("pull_request", {}) or _payload.get("merge_request", {}) or {}
        _obj_attrs = _payload.get("object_attributes", {}) or {}
        _pr_num = _pr_obj.get("number", _pr_obj.get("iid", "")) or _obj_attrs.get("iid", "")

        # 伪 issue_id（跟 _dispatch_pr_subscription_extra 一致）
        _pseudo_id = f"#pr_{_pr_num}_{scene_id}" if _pr_num else ""

        # 选引擎
        from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
        _engine_id = pick_engine(repo_key, "qa", fallback_runtime_id=0, scene_id=scene_id)
        if _engine_id is None:
            log.info(f"Pipeline: PR subscription redispatch: no engine for scene={scene_id}")
            return False

        _be = engine_name(_engine_id) or ""
        _is_broadcast = (_engine_id == 0 and is_plugin_mode()
                         and get_execution_mode() != EXEC_MODE_DIRECT)
        _extra_evt = dict(evt)
        _extra_evt["payload"] = _payload  # 确保是 dict（_load_event 返回 JSON 字符串）
        # 补全 build_devops_agent_prompt 必需的字段（_load_event 不返回 repository/source）
        from src.repo_key import from_repo_key
        _extra_evt["repository"] = _extra_evt.get("repository") or from_repo_key(repo_key)
        _extra_evt["source"] = _extra_evt.get("source") or (repo_key.split(":", 1)[0] if ":" in repo_key else "gitcode")
        _extra_evt["repo_key"] = repo_key
        _extra_evt["_pr_sub_scene_id"] = scene_id
        _extra_evt["_pr_sub_skill"] = qa_skill
        _extra_evt["_pr_sub_engine_ids"] = qa_engine_ids
        if _pr_num:
            _extra_evt["_pr_sub_pr_number"] = _pr_num

        if _is_broadcast:
            # 复用 _enqueue_pipeline_task：自动构建 prompt/env_extra/会话/init_event_json
            # Issue #259：get_scene_pool_engine_ids 已按健康过滤；None = 场景引擎全灭（方案A，不入队）
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")
        else:
            _kind = runtime_execution_kind(_engine_id)
            if _kind == EXEC_MODE_DIRECT:
                _extra_evt["_skill_path"] = qa_skill
                _extra_evt["_skill_id"] = 0
                _direct_tid = _create_direct_task(repo_key, seq, "qa", _be, qa_skill,
                                    qa_skill, _engine_id, _extra_evt, _pseudo_id, skill_id=0)
                start_direct_agent(_execute_agent, (
                    repo_key, seq, "qa", _be, qa_skill, _extra_evt, _engine_id, _direct_tid
                ))
            else:
                _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
                if _pool_ids is None:
                    _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub else-branch", repo_key=repo_key)
                    return
                _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                       0, qa_skill, _engine_id, _pseudo_id,
                                       pool_engine_ids=_pool_ids or "")

        # 重派成功：把事件状态改回 running（重派前可能是 failed）
        from src.api.event_store import update_event_status
        update_event_status(seq, "running")
        log.info(f"Pipeline: PR subscription redispatch ok, seq={seq} scene={scene_id} engine={_engine_id}")
        return True
    except Exception as e:
        log.warning(f"Pipeline: PR subscription redispatch failed: {e}")
        return False


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
                from src.api.event_store import append_skipped_event
                _sub_skip_iid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
                append_skipped_event(repo_key, evt.get("type", ""), "subscription_no_skill", agent_type,
                                     issue_id=_sub_skip_iid)
                return
        # Agent 启用检查
        from src.api.session_store import is_agent_enabled
        if not is_agent_enabled(repo_key, agent_type):
            log.info(f"Pipeline subscription: agent={agent_type} disabled, skip extra dispatch")
            from src.api.event_store import append_skipped_event
            _sub_skip_iid2 = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
            append_skipped_event(repo_key, evt.get("type", ""), "subscription_agent_disabled", agent_type,
                                 issue_id=_sub_skip_iid2)
            return
        # 创建事件
        from src.api.event_store import append_event
        issue_id = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "") or "#0"
        # 从 tasks 表获取 Issue 真实标题（避免 Pipeline 列表显示 "A2A (订阅): xxx"）
        _sub_title = ""
        _sub_iss_num = str(issue_id).lstrip("#")
        if _sub_iss_num and _sub_iss_num.isdigit():
            try:
                from src.api.issues_center import _get_issue_title
                _sub_title = _get_issue_title(repo_key, int(_sub_iss_num)) or ""
            except Exception:
                pass
        if not _sub_title:
            _sub_title = f"A2A (订阅): {agent_type}"
        # Issue #252: 订阅事件继承原事件实体类型
        _sub_entity_type = "pull_request" if event_type in PR_EVENT_TYPES else ("issue" if event_type in ISSUE_EVENT_TYPES else "")
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=_sub_title, issue_id=issue_id, source="a2a",
            agent_type=agent_type, actor=evt.get("actor", ""),
            summary=f"订阅触发: {event_type} → {agent_type}（主路由: {main_agent}）",
            payload=evt.get("payload", {}), status="running",
            entity_type=_sub_entity_type,
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
            _sub_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))
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
    from src.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    # account 兜底用仓库 owner 段（无 default 概念；避免全名作为 ACCOUNT）
    if not account and evt.get("repository"):
        _o_acc, _ = split_owner_repo(evt["repository"])
        evt["account"] = _o_acc
    event_type = evt.get("type", "unknown")

    log.info(f"Pipeline: start, raw_type={event_header}, source={source}, repo={evt['repository']}")

    # Issue #89: 提前解析 issue_id 供 skipped 事件审计（所有提前 return 路径共用）
    _pre_issue_id = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "")
    if not _pre_issue_id and event_type in PR_EVENT_TYPES:
        try:
            _pre_issue_id = resolve_pr_issue_id(evt.get("payload", {}) or {}, repo_key=repo_key) or ""
        except Exception:
            pass

    if not evt["repository"] or event_type == "unknown":
        log.info(f"Pipeline skipped: repository='{evt['repository']}' or event_type='{event_type}' not recognized")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "unrecognized_event", issue_id=_pre_issue_id)
        return result

    # 1.5 PR 订阅预匹配（在自循环检测之前，确保 bot 创建/更新的 PR 也能触发其他场景 QA）
    _pr_branch_early = resolve_pr_target_branch(evt)
    if _pr_branch_early and not evt.get("branch"):
        evt["branch"] = _pr_branch_early
    if event_type in PR_EVENT_TYPES:
        # PR 订阅触发条件：
        # - pull_request_opened：新建 PR，必定有首次 commit → 订阅
        # - pull_request_updated：需有新 commit（oldrev 非空或有 commits 字段）→ 订阅，否则跳过
        # - pull_request_merged：已合并，审查无意义 → 不订阅
        _pr_sub_payload = evt.get("payload", {}) or {}
        _pr_sub_action = str(_pr_sub_payload.get("_raw_action", "") or
                             (_pr_sub_payload.get("object_attributes", {}) or {}).get("action", "")).lower()
        if event_type == "pull_request_merged" or _pr_sub_action == "merge":
            log.info(f"Pipeline: PR subscription skip: merged (no need to review)")
            _pr_sub_matches_early = []
        elif _pr_sub_action == "update" and event_type == "pull_request_updated":
            _pr_sub_oa = _pr_sub_payload.get("object_attributes", {}) or {}
            _pr_sub_has_commit = bool(_pr_sub_payload.get("commits")) or bool(_pr_sub_oa.get("oldrev"))
            if not _pr_sub_has_commit:
                log.info(f"Pipeline: PR subscription skip: no_commit_on_pr_update (action=update, no new commit)")
                _pr_sub_matches_early = []
            else:
                _pr_sub_matches_early = _match_pr_subscription(repo_key, evt, event_type, _pr_branch_early)
        else:
            _pr_sub_matches_early = _match_pr_subscription(repo_key, evt, event_type, _pr_branch_early)
        if _pr_sub_matches_early:
            _issue_scene_id_early = ""
            _issue_qa_skill_early = ""
            if _pre_issue_id:
                try:
                    from src.api.session_store import get_issue_scene
                    _issue_scene_id_early, _ = get_issue_scene(repo_key, _pre_issue_id)
                except Exception:
                    pass
            # 从匹配列表中查找 Issue 所在场景的 qa_skill（排除前先取）
            if _issue_scene_id_early:
                for m in _pr_sub_matches_early:
                    if m[0] == _issue_scene_id_early:
                        _issue_qa_skill_early = m[1]
                        break
            _pr_sub_matches_early = _filter_pr_sub_matches(_pr_sub_matches_early, _issue_scene_id_early, _issue_qa_skill_early)
            if _pr_sub_matches_early:
                for _extra in _pr_sub_matches_early:
                    _dispatch_pr_subscription_extra(repo_key, evt, event_type, _extra)
                log.info(f"Pipeline: PR subscription pre-match -> scenes={[m[0] for m in _pr_sub_matches_early]} (before self-loop check)")

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
                log.info(f"Pipeline skip: bot={actor} self-action")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "self_loop", actor, issue_id=_pre_issue_id)
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
                append_skipped_event(repo_key, event_type, "system_auto_comment", issue_id=_pre_issue_id)
                return result

            # Issue #51: 开发者指令检测（/done /cancel /resume）
            _note_lower = _note.lower().strip()
            _issue_num = ""
            try:
                _issue_num = str(_oa.get("iid") or _oa.get("number") or (_payload.get("issue", {}) or {}).get("number") or "").lstrip("#")
            except Exception:
                pass
            if _issue_num and _issue_num.isdigit():
                _issue_num_int = int(_issue_num)
                if any(kw in _note_lower for kw in ["/done", "完成任务", "任务完成", "确认完成"]):
                    from src.api.issues_center import confirm_issue_done
                    confirm_issue_done(repo_key, _issue_num_int, confirmed_by="issue_comment")
                    log.info(f"Issue #{_issue_num} confirmed done via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_confirm_done")
                    return result
                elif any(kw in _note_lower for kw in ["/cancel", "取消任务"]):
                    from src.api.issues_center import cancel_issue_task
                    cancel_issue_task(repo_key, _issue_num_int)
                    log.info(f"Issue #{_issue_num} cancelled via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_cancel")
                    return result
                elif any(kw in _note_lower for kw in ["/resume", "恢复任务"]):
                    from src.api.issues_center import resume_issue_task
                    resume_issue_task(repo_key, _issue_num_int)
                    log.info(f"Issue #{_issue_num} resumed via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_resume")
                    return result

            # 澄清/审核回复自动恢复（§3.6/§3.5）：非 bot 用户评论且 issue 处 awaiting_input 或 pending_review →
            # 恢复 processing。bot 自评在上方 2.0 自循环检测已跳过；note_hook_pr 不适用（PR 讨论非 issue 澄清）。
            if event_type == "issue_comment" and _issue_num and _issue_num.isdigit() and int(_issue_num) > 0:
                try:
                    from src.api.issues_center import _resume_issue_waiting
                    if _resume_issue_waiting(repo_key, int(_issue_num)):
                        log.info(f"Pipeline: issue #{_issue_num} awaiting/pending_review → processing (issue_comment reply)")
                except Exception as _re:
                    log.warning(f"Pipeline: issue_comment resume failed: {_re}")
        except Exception as e:
            log.warning(f"Pipeline: auto-comment marker / command check failed: {e}")

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
                append_skipped_event(repo_key, event_type, "label_only_change", issue_id=_pre_issue_id)
                return result
        except Exception as e:
            log.warning(f"Pipeline: issue_updated label-only check failed: {e}")

    # 2.2 L1 事件准入过滤（Issue #49，可配置规则 event_action_filter）
    # 按 action + 操作者 + changes 判定：过滤无意义动作、edit 需 description 变更、PR update 按操作者、
    # close 副作用-only（执行关闭后短路不进入 agent）。
    try:
        from src.api.event_rule import evaluate_event_action
        _vcs_source = evt.get("source", "") or ""
        _d = evaluate_event_action(event_type, evt.get("payload", {}) or {}, vcs_source=_vcs_source)
        if _d["decision"] == "skip":
            log.info(f"Pipeline skip: {_d['reason']} (event_action_filter, actor={_d['actor_type']})")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, _d["reason"], issue_id=_pre_issue_id)
            return result
        if _d["decision"] == "side_effect":
            # 执行副作用后短路：close → close_issue_tasks
            if _d.get("side_effect") == "close_issue_tasks":
                try:
                    from src.api.issues_center import close_issue_tasks
                    _ce_id = (evt.get("payload", {}) or {}).get("object_attributes", {}) or {}
                    _ce_num = str(_ce_id.get("iid") or _ce_id.get("number") or "").lstrip("#")
                    if _ce_num.isdigit() and int(_ce_num) > 0:
                        _ce_n = close_issue_tasks(repo_key, int(_ce_num))
                        log.info(f"Pipeline (L1 side_effect): closed tasks for issue #{_ce_num} ({_ce_n} rows)")
                except Exception as _ce:
                    log.warning(f"Pipeline L1 side_effect close_issue_tasks failed: {_ce}")
            else:
                log.info(f"Pipeline side_effect: {_d['side_effect'] or 'n/a'} (event_action_filter)")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, f"side_effect:{_d.get('side_effect', '')}",
                                 issue_id=_pre_issue_id)
            return result
    except Exception as e:
        log.warning(f"Pipeline: event_action_filter check failed: {e}")

    # 3. 路由判定（D9 阶段矩阵 + 非 Issue 静态映射，替代 agent_type.subscribe_events，见 vod-skill-upgrade-plan §3.0.4）
    _is_scene_recommend = False  # 智能推荐模式标记
    if event_type == "issue_opened":
        agent_type = "vod"
        log.info(f"Pipeline: issue_opened -> VOD (stage init)")
        # 判断是否需要推荐：issue_state 有 scene_id → 用户已选场景，跳过推荐
        # GitCode 直接创建的 Issue 无 scene_id → 走智能推荐
        # Task Center 创建 Issue 后 GitCode webhook 可能比 set_issue_scene 先到达，
        # 最多重试 3 次、每次间隔 0.5s，等 scene_id 写入
        _rid_early = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        _is_scene_recommend = True  # 默认走推荐
        if _rid_early:
            import time as _time
            for _attempt in range(3):
                try:
                    from src.api.session_store import get_issue_scene
                    _existing_scene, _ = get_issue_scene(repo_key, _rid_early)
                    if _existing_scene:
                        _is_scene_recommend = False
                        log.info(f"Pipeline: issue_opened 已有场景 {_existing_scene}（attempt={_attempt+1}），跳过推荐")
                        break
                    if _attempt < 2:
                        _time.sleep(0.5)
                except Exception as _re:
                    log.warning(f"Pipeline: issue_opened scene check failed: {_re}")
                    break
        # project-info 后台刷新（服务端 codegraph 生成基线，不阻塞 webhook；见 codegraph-builder-plan.md）
        # Issue #200: 传入 bot token，私有仓库 git fetch 需认证 header
        try:
            from src.project_info.builder import maybe_refresh_project_info
            from src.repo import Repo
            _pi_token = Repo(repo_key).bot_token
            threading.Thread(target=maybe_refresh_project_info, args=(repo_key, _pi_token), daemon=True).start()
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
    # PR 订阅：无主路由时（PR 无关联 Issue，非 bot 操作未被自循环拦截），
    # 第一个匹配作为主 agent。PR 订阅的并行分发已在自循环检测前完成（1.5 预匹配）。
    if not agent_type and event_type in PR_EVENT_TYPES:
        _pr_sub_matches = _match_pr_subscription(repo_key, evt, event_type, _pr_branch)
        if _pr_sub_matches:
            # 排除 PR 关联 Issue 所在场景（避免同场景重复 QA）
            _issue_scene_id = ""
            _issue_qa_skill = ""
            if _pre_issue_id:
                try:
                    from src.api.session_store import get_issue_scene
                    _issue_scene_id, _ = get_issue_scene(repo_key, _pre_issue_id)
                except Exception:
                    pass
            # 从匹配列表中查找 Issue 所在场景的 qa_skill（排除前先取）
            if _issue_scene_id:
                for m in _pr_sub_matches:
                    if m[0] == _issue_scene_id:
                        _issue_qa_skill = m[1]
                        break
            _pr_sub_matches = _filter_pr_sub_matches(_pr_sub_matches, _issue_scene_id, _issue_qa_skill)
            if _pr_sub_matches:
                _first = _pr_sub_matches[0]
                evt["_pr_sub_scene_id"] = _first[0]
                evt["_pr_sub_skill"] = _first[1]
                evt["_pr_sub_engine_ids"] = _first[2]
                if _first[3]:
                    evt["_pr_sub_sre_skill"] = _first[3]
                    evt["_pr_sub_sre_engine_ids"] = _first[4]
                agent_type = "qa"
                log.info(f"Pipeline: PR subscription triggered -> scene={_first[0]} agent=qa branch={_pr_branch}")
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
        append_skipped_event(repo_key, event_type, _reason, issue_id=_pre_issue_id)
        return result

    # 3.1 Issue 生命周期阶段初始化（D8/D9）：issue_opened 建 vod 阶段；其余命中 vod 且无记录时补建
    from src.api.session_store import set_issue_stage
    if event_type == "issue_opened":
        _rid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        if _rid:
            _asg = _fetch_issue_assignee(repo_key, _rid)
            # Issue #49: assignee 为空时 fallback 到 issue 创建者（actor），而非仓库 owner
            if not _asg:
                _asg = actor or ""
                if _asg:
                    log.info(f"Pipeline: issue_assignee API empty, fallback to actor={_asg} for {_rid}")
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
    # PR merged 跳过 QA 直接路由 SRE：补推 stage qa→sre（原由 QA A2A handoff 推进）
    if event_type == "pull_request_merged" and agent_type == "sre":
        _rid_sre = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "")
        if _rid_sre:
            from src.api.session_store import get_issue_stage as _gis
            _st_sre = _gis(repo_key, _rid_sre)
            if _st_sre and _st_sre.get("stage") == "qa":
                set_issue_stage(repo_key, _rid_sre, "sre", "sre")
                log.info(f"Pipeline: PR merged skip QA, advance stage qa→sre for {_rid_sre}")

    # 3.2 任务中心全覆盖（task-center-issue-coverage-design.md §3-A）：
    # Issue 事件幂等建档；closed 动作自动完成对应任务（P5 webhook 兜底）。
    # 旁路：失败仅告警，不阻塞事件处理。
    try:
        from src.api.issues_center import ensure_issue_task, close_issue_tasks
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
                    # Issue #178: 提取 webhook 操作用户（sender）作为 assignee fallback
                    _sender3 = ""
                    try:
                        _snd = _p3.get("sender") or _p3.get("user") or {}
                        if isinstance(_snd, dict):
                            _sender3 = _snd.get("username") or _snd.get("login") or ""
                    except Exception:
                        pass
                    _t3 = str(_o3.get("title", "") or "") if isinstance(_o3, dict) else ""
                    if not _t3:
                        _t3 = evt.get("title", "") or ""
                    ensure_issue_task(repo_key, int(_num3), title=_t3, assignee=_asg3,
                                      event_type=event_type, sender=_sender3)
                    log.info(f"Pipeline: ensure task #{_num3} assignee={_asg3 or _sender3 or '(owner)'}")
    except Exception as _e:
        log.warning(f"Pipeline: task-center sync failed: {_e}")

    # 3.3 Agent 启用检查（阶段路由后的最终 agent）
    from src.api.session_store import is_agent_enabled
    if not is_agent_enabled(repo_key, agent_type):
        log.info(f"Pipeline: agent '{agent_type}' disabled for repo={repo_key}, skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "agent_disabled", agent_type, issue_id=_pre_issue_id)
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
        _early_issue_id = resolve_pr_issue_id(evt.get("payload", {}) or {}, repo_key=repo_key) or ""
    if _early_issue_id:
        try:
            from src.api.session_store import get_issue_scene
            _issue_scene, _issue_scene_uid = get_issue_scene(repo_key, _early_issue_id)
            _issue_scene = _issue_scene or ""   # 方案 B：无「default」场景，空=无场景
        except Exception:
            pass
    # scene_user_id=0 时兜底反查（旧数据 is_join=0 导致 scene_user_id 未写入）
    # 优先用 issue 创建者（actor）查 unified_user_mapping，避免多人仓库取错用户
    if not _issue_scene_uid:
        try:
            from src.runtime.engine_select import _repo_user_id
            _issue_scene_uid = _repo_user_id(repo_key, issue_creator=actor)
        except Exception:
            pass
    log.info(f"Pipeline: scene resolved -> scene_id={_issue_scene} uid={_issue_scene_uid} issue={_early_issue_id or '(none)'}")
    # PR 订阅场景：直接从场景 nodes 取 skill，不走 agent_session / issue_scene
    _is_pr_sub = bool(evt.get("_pr_sub_scene_id"))
    if _is_pr_sub:
        skill_name = evt["_pr_sub_skill"]
        skill_id = 0
        skill_path = skill_name
        _issue_scene = evt["_pr_sub_scene_id"]
        try:
            from src.user.db import resolve_skill_id, get_skill_by_id
            _sid = resolve_skill_id(skill_name, repo_key=repo_key)
            if _sid:
                skill_id = _sid
                _sk = get_skill_by_id(_sid)
                if _sk:
                    skill_path = _sk.get("skill_path") or skill_name
        except Exception:
            pass
        backend = ""  # PR 订阅不走 agent_session，backend 置空让 pick_engine 自动选
        log.info(f"Pipeline (PR-sub): skill from scene nodes: {skill_name} sid={skill_id} (scene={_issue_scene})")
    # Issue 级场景优先：scene 非 default 时，用 issue scene 的 skill 覆盖 session 的 skill
    # 智能推荐模式：跳过 skill 解析（场景未知，prompt 不用 skill）
    if _is_scene_recommend:
        skill_name = ""
        skill_id = 0
        skill_path = ""
        log.info("Pipeline (plugin): 智能推荐模式, skip skill resolution")
    elif is_plugin_mode() and _issue_scene and not _is_pr_sub:
        # 2026-10-10：去掉 != "default" —— default 场景的 skill 改动同样生效（前端已无
        # Agent 配置页，场景 nodes 成为唯一可编辑入口），与 webhook.py a2a 路径口径一致
        _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
        if _pipe_skill:
            skill_name = _pipe_skill
            # 同步更新 skill_id 和 skill_path，否则执行时仍用旧 skill_id 查到旧 skill
            from src.user.db import resolve_skill_id, get_skill_by_id, get_skill_path
            _new_sid = resolve_skill_id(_pipe_skill, repo_key=repo_key, user_id=_issue_scene_uid)
            if _new_sid:
                skill_id = _new_sid
                _sk = get_skill_by_id(_new_sid)
                skill_path = (_sk.get("skill_path") or _pipe_skill) if _sk else _pipe_skill
            else:
                skill_path = _pipe_skill
                skill_id = 0
            log.info(f"Pipeline (plugin): issue scene skill override: {agent_type}={_pipe_skill} sid={skill_id} (scene={_issue_scene})")
    if not backend or not skill_name:
        if _is_pr_sub:
            pass  # PR 订阅已设好 skill，跳过 fallback
        elif _is_scene_recommend:
            skill_name = ""
            backend = ""
            log.info("Pipeline (plugin): 智能推荐模式, using recommender prompt (no skill)")
        elif is_plugin_mode():
            _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
            if _pipe_skill:
                skill_name = _pipe_skill
                backend = backend or ""
                log.info(f"Pipeline (plugin): skill for {agent_type}={_pipe_skill} (scene={_issue_scene})")
        if not skill_name and not _is_scene_recommend:
            # 推荐模式不需要 skill（用 build_scene_recommender_prompt 内置 prompt），
            # 跳过 fallback 的 no_agent_session 检查，直接走引擎选择 → 广播入队
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
                        from src.api.event_store import append_skipped_event
                        append_skipped_event(repo_key, event_type, "no_skill_config", agent_type, issue_id=_pre_issue_id)
                        return result
                else:
                    log.info(f"Pipeline: no agent session for repo={repo_name}, agent={agent_type}, skip")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "no_agent_session", agent_type, issue_id=_pre_issue_id)
                    return result
            except Exception as e:
                log.warning(f"Pipeline: failed to load default skill config: {e}")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "skill_config_error", str(e)[:200], issue_id=_pre_issue_id)
                return result

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    payload = evt.get("payload", {})
    if event_type in ("note_hook_pr", "pull_request_opened", "pull_request_updated", "pull_request_merged"):
        # PR 类事件：仅用 resolve_pr_issue_id（title/分支/issues 列表/PR 详情 API）。
        # 不用 resolve_issue_id——GitCode MR payload 的 object_attributes.iid 是 PR 号，
        # 会误当 issue_id（_is_pr_payload 只识别带 pull_request/issues 键的 payload）。
        try:
            from src.user.db import get_agent_bot_token
            _pr_api_token = get_agent_bot_token(repo_key) or ""
            _pr_issue = resolve_pr_issue_id(payload, _pr_api_token, repo_key=repo_key)
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
    # PR 订阅：用 PR 号 + scene_id 构造伪 issue_id 参与去重（区分不同场景）
    if not issue_id and evt.get("_pr_sub_scene_id"):
        pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
        obj_attrs = payload.get("object_attributes", {}) or {}
        pr_num = pr_obj.get("number", pr_obj.get("iid", "")) or obj_attrs.get("iid", "")
        if pr_num:
            _sub_scene = evt["_pr_sub_scene_id"]
            issue_id = f"#pr_{pr_num}_{_sub_scene}"
            evt["_pr_sub_pr_number"] = pr_num
            log.info(f"Pipeline: PR subscription pseudo issue_id={issue_id}")
    title = evt.get("title", "") or event_type
    log.info(f"Pipeline: issue_id={issue_id or '(none)'}, title={title}")

    # 5.5 构建摘要描述（纯内存操作，放在锁外）
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

    # Issue #252: 实体类型区分（issue vs pull_request），编号相同时不混淆
    _entity_type = "pull_request" if event_type in PR_EVENT_TYPES else ("issue" if event_type in ISSUE_EVENT_TYPES else "")

    # 5.1+6 去重检查 + 写入事件（合并到同一个 db_lock 临界区，消除竞态窗口）
    # 原因：GitHub 对同一 Issue 创建会发多个 webhook（opened/assigned/labeled 等），
    # 如果 action 不细分会导致同一 issue 产生重复任务。
    # 豁免：a2a_event（已有自己的去重）
    _scene_id_for_event = evt.get("_pr_sub_scene_id", "") or _issue_scene
    _seq = 0
    # issue_comment 豁免 webhook 去重（issue-comment-stage-owner-routing-design.md §3.3）：
    # 评论是人操作、频率低，被「同 (issue,agent) 有 running 事件」的 dedup 挡掉会导致评论丢失；
    # 改为始终新建事件 + 入队（agent_tasks），由 issue_agent_session 的串行执行自然排队，
    # 待当前事件终态后再跑，不丢弃。bot 自评回声/系统回帖已在上游过滤，不会因豁免循环触发。
    if issue_id and agent_type and event_type not in ("a2a_event", "issue_comment"):
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
                    append_skipped_event(repo_key, event_type, f"dedup:active_{agent_type}_for_{issue_id}",
                                         issue_id=issue_id)
                    result["skipped"] = "duplicate_active_task"
                    return result
                # dedup 通过 → 直接 INSERT（不再调 append_event 函数，避免重复加锁开销）
                _dc.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result, scene_id, entity_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (repo_key, issue_id or "#0", event_type, source, agent_type, title, actor, summary,
                     json.dumps(payload or {}), "running", "", _scene_id_for_event, _entity_type)
                )
                db.commit()
                _seq = _dc.lastrowid or 0
                # 验证行确实写入（防止 WAL 并发写入冲突时 lastrowid 返回旧值）
                _dc.execute("SELECT seq FROM events WHERE seq=?", (_seq,))
                if not _dc.fetchone():
                    db.rollback()
                    log.warning(f"Pipeline: INSERT returned lastrowid={_seq} but row not found (WAL conflict?), repo={repo_key}, type={event_type}")
        except Exception as _de:
            log.warning(f"Pipeline: dedup+insert failed: {_de}")
    else:
        # a2a_event 或无 issue_id/agent_type：直接写入（无需 dedup）
        from src.api.event_store import append_event
        _seq = append_event(
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
            scene_id=_scene_id_for_event,
            entity_type=_entity_type,
        )
    seq = _seq
    # WAL checkpoint 在 db_lock 释放后执行（避免阻塞其他需要 db_lock 的操作）
    if seq and issue_id and agent_type and event_type != "a2a_event":
        try:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
    log.info(f"Pipeline: event created, seq={seq}, repo_key={repo_key}, agent={agent_type}, issue_id={issue_id or '#0'}")

    from src.repo_key import from_repo_key
    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": from_repo_key(repo_key), "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})

    # 执行分派：从引擎集合选引擎（专属→全局→本机，engine-selection-design.md v2）；
    # 选中 remote → 入队该引擎（queue_remote）；选中本机 → direct 就地执行。
    from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
    from src.runtime.service import runtime_execution_kind, enqueue_task, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE, get_execution_mode
    try:
        engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0,
                                scene_id=_issue_scene, user_id=_issue_scene_uid)
        log.info(f"Pipeline: pick_engine returned engine_id={engine_id} for repo={repo_key} agent={agent_type} seq={seq}")
    except Exception as _pe:
        log.warning(f"Pipeline: pick_engine exception: {_pe}")
        engine_id = None
    if engine_id is None:
        log.info(f"Pipeline: no available engine for repo={repo_key} agent={agent_type}, seq={seq}, skip")
        # 创建 failed 状态的 task 快照（供重派复用参数，否则重派报"无关联任务"）
        _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                            skill_path, skill_id, evt, issue_id, runtime_id or 0)
        _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
        return result
    # Plugin 模式 + engine_id=0：无主池任务（广播），多个引擎通过 claim_by_tag 竞争认领。
    # 非 direct 模式均走广播（queue_remote + auto）；仅 direct 模式走本机 direct（自动选引擎）。
    # 场景 engine_runtime_ids 限定广播范围（写入 pool_engine_ids 字段）。
    # Issue #112 修复：所有模式都获取 pool_engine_ids，确保场景引擎绑定生效
    # Issue #259：写入前已按健康过滤（get_scene_pool_engine_ids 内部）。
    _pool_engine_ids = get_scene_pool_engine_ids(_issue_scene, agent_type, repo_key=repo_key, user_id=_issue_scene_uid)
    if _pool_engine_ids is None:
        # 方案A：场景绑定的引擎全部离线/失联 → 不入队，事件置 failed 并明确提示
        log.warning(f"Pipeline: scene={_issue_scene} agent={agent_type} engines all offline, skip (Issue #259)")
        _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                            skill_path, skill_id, evt, issue_id, runtime_id or 0)
        _fail_event(seq, f"场景绑定的引擎全部离线（scene={_issue_scene}, agent={agent_type}），请检查引擎在线状态")
        return result
    if engine_id == 0 and is_plugin_mode() and get_execution_mode() != EXEC_MODE_DIRECT:
        _be = backend or "opencode"
        # 智能推荐模式：强制限定 opencode 引擎认领（推荐任务轻量，opencode 最快）
        if _is_scene_recommend:
            try:
                import json as _json
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT id FROM agent_runtimes WHERE name='opencode' AND status='online'")
                    _oc_ids = [r[0] for r in c.fetchall()]
                if _oc_ids:
                    _pool_engine_ids = _json.dumps(_oc_ids)
                    log.info(f"Pipeline (plugin broadcast): 智能推荐限定 opencode engines={_oc_ids}")
                else:
                    log.info("Pipeline (plugin broadcast): 智能推荐无 online opencode，不限定（兜底放行）")
            except Exception as _oe:
                log.warning(f"Pipeline: 智能推荐 opencode 过滤失败: {_oe}")
        log.info(f"Pipeline (plugin broadcast): engine_id=0 pool_tag={agent_type} pool_engine_ids={_pool_engine_ids} seq={seq}")
        if _be and seq > 0 and (skill_name or _is_scene_recommend):
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id,
                                   pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid,
                                   is_scene_recommend=_is_scene_recommend)
        return result
    # backend 必须取已注册引擎的类型名（不沿用会话/字面默认），确保执行 CLI 与选中引擎一致
    _be = engine_name(engine_id) or backend
    # 智能推荐模式 backend 被清空 → 兜底 opencode（auto-select 会覆盖为实际引擎名）
    if not _be and _is_scene_recommend:
        _be = "opencode"
    if not _be or seq <= 0:
        return result
    # 智能推荐模式允许 skill_name 为空（纯 prompt，不用 skill 文件）
    if not skill_name and not _is_scene_recommend:
        return result
    # engine_id=0：plugin direct/auto 兜底 → 自动选一个本机在线引擎执行
    if not engine_id:
        # Issue #253 改动4：有显式绑定时 direct 自动选机只允许在绑定集合内挑（宁失败不跑飞）。
        # 原行为：全站 online/healthy 查询（opencode>codearts>其他），hermes 等池外引擎可被选中。
        _pool_ids_253 = []
        if _pool_engine_ids:
            try:
                import json as _json253
                _pool_ids_253 = [int(x) for x in _json253.loads(_pool_engine_ids) if x]
            except Exception:
                _pool_ids_253 = []
        if _pool_ids_253:
            # 绑定集合内选在线健康引擎；全灭 → failed + 人话原因，不碰全站
            _bind_names_253 = []
            try:
                from src.runtime.engine_select import engine_name as _en253
                _bind_names_253 = [_en253(int(x)) or str(x) for x in _pool_ids_253]
            except Exception:
                _bind_names_253 = [str(x) for x in _pool_ids_253]
            _picked_253 = 0
            try:
                from src.session_manager import db as _db, db_lock as _dbl
                with _dbl:
                    _c = _db.cursor()
                    _ph253 = ",".join("?" * len(_pool_ids_253))
                    _c.execute(
                        f"SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 "
                        f"AND id IN ({_ph253}) "
                        f"ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1",
                        tuple(_pool_ids_253))
                    _row = _c.fetchone()
                if _row:
                    _picked_253 = _row[0]
                    engine_id = _row[0]
                    _be = _row[1] or _be
                    log.info(f"Pipeline: direct auto-select within scene binding {list(set(_bind_names_253))} "
                             f"-> id={engine_id} name={_be}, seq={seq} (Issue #253)")
            except Exception as _e:
                log.warning(f"Pipeline: binding-constrained engine query failed: {_e}, seq={seq}")
            if not _picked_253:
                log.info(f"Pipeline: scene binding {list(set(_bind_names_253))} all offline for direct "
                         f"auto-select, seq={seq}, skip (Issue #253)")
                _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                    skill_path, skill_id, evt, issue_id, runtime_id or 0)
                _fail_event(seq, f"场景绑定的引擎（{', '.join(sorted(set(_bind_names_253)))}）"
                                 f"全部不可用，direct 模式无法选机（Issue #253）")
                return result
        else:
            # 无绑定（空 pool）→ 原全站自动选机行为不变
            log.info(f"Pipeline: engine_id=0, auto-selecting local engine for seq={seq}")
            # 尝试取一个本机在线引擎（优先 opencode，其次 codearts，最后 hermes）
            from src.user.db import get_meta_value as _gmv
            try:
                from src.session_manager import db as _db, db_lock as _dbl
                log.info(f"Pipeline: acquiring db_lock for engine query, seq={seq}")
                with _dbl:
                    _c = _db.cursor()
                    _c.execute("SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1")
                    _row = _c.fetchone()
                log.info(f"Pipeline: engine query done, row={_row}, seq={seq}")
                if _row:
                    engine_id = _row[0]
                    _be = _row[1] or _be
                    log.info(f"Pipeline: auto-selected local engine id={engine_id} name={_be}, seq={seq}")
                else:
                    log.info(f"Pipeline: engine_id=0 no online engine, seq={seq}, skip")
                    _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                        skill_path, skill_id, evt, issue_id, runtime_id or 0)
                    _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
                    return result
            except Exception as _e:
                log.warning(f"Pipeline: auto-select engine failed: {_e}")
                _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                    skill_path, skill_id, evt, issue_id, runtime_id or 0)
                _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
                return result
    kind = runtime_execution_kind(engine_id)
    if kind != EXEC_MODE_DIRECT and _be and skill_name and seq > 0:
        _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                               skill_id, skill_path, engine_id, issue_id,
                               pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid,
                               is_scene_recommend=_is_scene_recommend)
    elif _be and seq > 0 and (skill_name or _is_scene_recommend):
        # 智能推荐模式 skill_name 可为空（纯 prompt）
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


# ====== VCS provider 分流辅助 ======

def _is_github_repo(repo_key: str) -> bool:
    """判断 repo_key 是否为 GitHub 仓库（provider 前缀 github:）。"""
    return bool(repo_key) and repo_key.startswith("github:")


def _get_repo_api_token(repo_key: str) -> str:
    """根据 repo_key 的 provider 获取对应 VCS API token。

    GitHub → get_github_token；GitCode → get_gitcode_token。
    """
    if _is_github_repo(repo_key):
        try:
            from src.user.db import get_github_token
            return get_github_token(repo_key=repo_key) or ""
        except Exception:
            return ""
    try:
        from src.user.db import get_gitcode_token
        return get_gitcode_token(repo_key=repo_key) or ""
    except Exception:
        return ""


def _github_fetch_issue(owner: str, repo: str, issue_num: str, token: str) -> dict:
    """通过 GitHub API 获取 Issue 详情。失败抛异常。"""
    from src.user.repo_manager import _github_req
    return _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}", token=token)


def _github_list_issue_comments(owner: str, repo: str, issue_num: str, token: str) -> list:
    """通过 GitHub API 获取 Issue 评论列表。失败抛异常。"""
    from src.user.repo_manager import _github_req
    result = _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}/comments?per_page=100", token=token)
    return result if isinstance(result, list) else []


def _github_post_issue_comment(owner: str, repo: str, issue_num: str, body: str, token: str) -> dict:
    """通过 GitHub API 发 Issue 评论。失败抛异常。"""
    from src.user.repo_manager import _github_req
    return _github_req("POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}/comments", token=token, body={"body": body})


def _fetch_issue_assignee(repo_key: str, issue_id: str) -> str:
    """调 VCS API 获取 Issue 责任人（assignee.login）。失败返回空。

    根据 repo_key provider 分流 GitHub/GitCode。
    用于 pipeline 责任人显示（issue_state.assignee），取代"从事件 payload 猜 assignee"。
    """
    num = _issue_num_from(issue_id)
    if not num:
        return ""
    try:
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        if _is_github_repo(repo_key):
            from src.user.db import get_github_token
            tok = get_github_token(repo_key=repo_key) or ""
            if not tok:
                return ""
            d = _github_fetch_issue(owner, rname, num, tok)
            asg = d.get("assignee") or {}
            login = asg.get("login", "") if isinstance(asg, dict) else ""
            return login or ""
        # GitCode 原逻辑
        import urllib.request
        from src.user.db import get_gitcode_token
        tok = get_gitcode_token(repo_key=repo_key) or ""
        if not tok:
            return ""
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
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
    """调 VCS API 获取 Issue 标题。失败返回空。

    根据 repo_key provider 分流 GitHub/GitCode。
    用于 pipeline 展示兜底：当 tasks/events 表中 title 均为占位符时，
    通过 API 获取真实标题并回写。
    """
    num = _issue_num_from(issue_id)
    if not num:
        return ""
    try:
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        if _is_github_repo(repo_key):
            from src.user.db import get_github_token
            tok = get_github_token(repo_key=repo_key) or ""
            if not tok:
                return ""
            d = _github_fetch_issue(owner, rname, num, tok)
            return d.get("title", "") or ""
        # GitCode 原逻辑
        import urllib.request
        from src.user.db import get_gitcode_token
        tok = get_gitcode_token(repo_key=repo_key) or ""
        if not tok:
            return ""
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
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

    - 有关联 Issue：#5 → {provider}/{owner}/{repo}/issues/5/{agent_type}/
    - 无关联 Issue（push/#0）→ {provider}/{owner}/{repo}/{agent_type}-agent（兼容现状）
    """
    from src.repo_key import workspace_repo_dir
    _base = workspace_repo_dir(repo_key)
    num = _issue_num_from(issue_id)
    if num:
        return os.path.join(_base, "issues", num, agent_type)
    return os.path.join(_base, f"{agent_type}-agent")


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


def _pool_only_local_direct() -> int | None:
    """方案 B（weekly-05）：广播无 worker 时，若全局可执行引擎全为「本机 direct」则回落本机执行。

    判定：所有 status='online' AND healthy=1 的 agent_runtimes 的 credential_id 是否全为 0
    （credential_id=0 = 本机 direct 引擎；>0 = 远端 worker 引擎池）。
    - 全本机 → 返回首个在线健康引擎 id（服务端 self-run）
    - 无在线引擎 或 存在远端 worker 引擎 → None（保持广播/等待 worker）
    """
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id, credential_id FROM agent_runtimes "
                "WHERE status='online' AND healthy=1 ORDER BY id").fetchall()
        if not rows:
            return None
        if all((r[1] or 0) == 0 for r in rows):
            return int(rows[0][0])
        return None
    except Exception as e:
        log.warning(f"_pool_only_local_direct failed: {e}")
        return None


def _enqueue_pipeline_task(repo_key, seq, agent_type, backend, skill_name, evt,
                           skill_id, skill_path, runtime_id, issue_id="", pool_engine_ids="", user_id=0,
                           is_scene_recommend=False):
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
    from src.api.session_store import get_or_create_agent_session, get_or_create_issue_session, guard_issue_session_pointer
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
    oc_session_id = ""
    issue_session_id = ""
    if _issue_num_from(issue_id):
        _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
        issue_session_id = _isess.get("id") or ""
        # session 引擎守卫：runtime_id>0（确定性路由）时校验指针引擎与目标引擎同家，
        # 跨引擎指针弃用（hermes resume opencode 会话号会静默挂死）；广播/查不到 → 放行，
        # 由 worker 侧执行时兜底校验。
        _cur_engine = ""
        if runtime_id:
            from src.runtime.instances import engine_type_of_runtime
            _cur_engine = engine_type_of_runtime(runtime_id)
        oc_session_id = guard_issue_session_pointer(_isess, _cur_engine)
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回退读取。

    repo_gitcode_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception:
        pass

    # 4) 构建 prompt（含技能占位符化，见 skill-delivery-remote.md）
    try:
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        evt["_skill_path"] = skill_path
        if is_scene_recommend:
            from src.prompt_builder import build_scene_recommender_prompt
            prompt = build_scene_recommender_prompt(evt, repo_key)
        else:
            from src.prompt_builder import build_devops_agent_prompt
            prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                                  skill_path=skill_path, runtime_id=runtime_id, agent_type=agent_type)
    except Exception as e:
        log.warning(f"Pipeline enqueue: prompt build failed: {e}")
        _fail_event(seq, f"入队失败：prompt 构建异常 {str(e)[:150]}")
        return

    from src.prompt_builder import _resolve_server_url
    from src.repo_key import split_owner_repo as _sor2
    _owner2, _repo_name2 = _sor2(repository)
    env_extra = {
        "REPO_FULL_NAME": repository,
        "REPO_OWNER": _owner2,
        "REPO_NAME": _repo_name2,
        "ACCOUNT": evt.get("actor") or evt.get("account") or "",
        "GRAPE_SERVER_URL": _resolve_server_url(runtime_id),
    }
    # 注入场景环境变量（scene_id / work_branch / custom_fields；值不暴露在 prompt 文本里）
    try:
        from src.runtime.repogit import _read_scene_work_branch
        _wb = _read_scene_work_branch(repo_key, issue_id)
        if _wb:
            env_extra["GRAPE_WORK_BRANCH"] = _wb
    except Exception:
        pass
    _inject_scene_env(env_extra, repo_key, issue_id, agent_type)
    # Issue #242: 注入关联仓库列表（coding agent 读取后初始化多仓库 worktree）
    try:
        _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
        if _linked_repos:
            env_extra["GRAPE_LINKED_REPOS"] = json.dumps(_linked_repos, ensure_ascii=False)
    except Exception:
        pass
    # 事件级扩展环境变量（evt["_env_extra"] 通用机制；onboarding 已不使用，Issue #46）
    _evt_ext = evt.get("_env_extra") or {}
    if isinstance(_evt_ext, dict):
        env_extra.update(_evt_ext)
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
            from src.repo_key import workspace_repo_dir
            _expected_ws = os.path.join(workspace_repo_dir(repo_key), "issues", _num, agent_type)
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

    # 方案 B（weekly-05）：plugin 广播无 worker 认领时，若全局引擎全为本机 direct → 服务端 self-run
    if not runtime_id and _pool_agent_type:
        try:
            _local_rid = _pool_only_local_direct()
            if _local_rid:
                log.info(f"Pipeline enqueue: pool only local direct, self-run seq={seq} rid={_local_rid}")
                from src.runtime.engine_select import engine_name as _en
                from src.runtime.instances import runtime_instance_name as _rin
                _be = _en(_local_rid) or _rin(_local_rid) or backend or ""
                _evt2 = dict(evt)
                _evt2.setdefault("repo_key", repo_key)
                _evt2.setdefault("issue_id", issue_id)
                _evt2.setdefault("repository", from_repo_key(repo_key))
                _evt2.setdefault("skill_path", skill_path)
                # 从本地 import（模块内协程函数）
                from src.api.event_pipeline import _execute_agent as _exe_agent
                from src.api.event_pipeline import start_direct_agent as _sda
                _sda(_exe_agent, (
                    repo_key, seq, agent_type, _be, skill_name, _evt2, _local_rid, tid,
                ), name=f"self-{seq}")
        except Exception as e:
            log.warning(f"Pipeline enqueue: local self-run failed for seq={seq}: {e}")


def _resolve_scene_id(repo_key: str, issue_id: str = "") -> str:
    """解析 Issue 当前绑定开发场景（issue-219：env 注入 GRAPE_SCENE_ID 用）。

    优先读 issue 卡片 scene_id（get_issue_scene → issue 单表权威），空则回退
    managed_repos.dev_scene（与 runtime/repogit._read_scene_work_branch 同源，保证同场景）。
    未选定返回空串——agent 借此区分「场景未判定」，不回落 default（避免误导）。
    """
    scene_id = ""
    if issue_id:
        try:
            from src.api.session_store import get_issue_scene
            scene_id, _ = get_issue_scene(repo_key, issue_id)
        except Exception:
            scene_id = ""
    if not scene_id:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
                row = c.fetchone()
            scene_id = (row[0] if row else "") or ""
        except Exception:
            scene_id = ""
    return scene_id


def _inject_scene_env(env_extra: dict, repo_key: str, issue_id: str = "",
                      agent_type: str = "") -> None:
    """注入场景相关环境变量：GRAPE_SCENE_ID + 高级选项 custom_fields（Issue #236 直接注入）。

    值不暴露在 prompt 文本里，agent 通过 printenv / os.environ 直接读取。
    - GRAPE_SCENE_ID：当前 Issue 绑定的场景标识；未选定注入空串（不回落 default，agent 判「未判定」）。
    - custom_fields：以自定义变量名（name）直接作为环境变量名注入（如 MY_API_KEY=xxx）。
      注入前做二次冲突/大写校验（兜底存量或旁路数据），冲突项跳过并告警。
      scope 过滤：字段 scope 含 "all" 或含当前 agent_type 才注入；scope 为空/未传
      agent_type 时全量注入（保持现状行为）。
    """
    try:
        scene_id = _resolve_scene_id(repo_key, issue_id)
        env_extra["GRAPE_SCENE_ID"] = scene_id
        if not scene_id:
            return
        from src.session_manager import db, db_lock
        from src.scene_custom_env import is_upper_name, is_reserved_name, normalize_scope
        import json as _json
        from src.config import log
        # 读 advanced_options 的 custom_fields
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return
        opts = _json.loads(row[0]) if row[0] else {}
        fields = [f for f in (opts.get("custom_fields", []) or []) if f.get("name")]
        for f in fields:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            if not is_upper_name(name) or is_reserved_name(name):
                log.warning(f"scene custom env '{name}' rejected: reserved or not uppercase")
                continue
            # scope 多选过滤：["all"] 或包含当前 agent_type 才注入
            scope = normalize_scope(f.get("scope"))
            if agent_type and "all" not in scope and agent_type not in scope:
                continue
            env_extra[name] = str(f.get("value", ""))
    except Exception as _e:
        from src.config import log
        log.warning(f"inject scene env failed: {_e}")


def _read_scene_linked_repos(repo_key: str, issue_id: str = "") -> list:
    """读取场景关联仓库列表（Issue #242）。

    优先读 issue_state.scene_id 对应场景的 linked_repos 列，
    空则读 advanced_options JSON 中的 linked_repos。
    返回 [{repo_key, branch}] 列表。
    """
    try:
        scene_id = _resolve_scene_id(repo_key, issue_id)
        if not scene_id:
            return []
        from src.session_manager import db, db_lock
        import json as _json
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT linked_repos, advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row:
            return []
        # 优先读独立列
        if row[0]:
            try:
                return _json.loads(row[0]) or []
            except Exception:
                pass
        # 回退读 advanced_options JSON
        if row[1]:
            try:
                opts = _json.loads(row[1])
                return opts.get("linked_repos", []) or []
            except Exception:
                pass
        return []
    except Exception:
        return []


def build_task_env(repo_key: str, agent_type: str, issue_id: str, runtime_id: int = 0,
                   account: str = "", issue_number: str = "", pr_number: str = "") -> dict:
    """构建任务完整 env_extra（正常入队/重派/对话入队共用，防重派丢变量）。

    2026-10-10 事故：events.py 重派路径 enqueue_task(env_extra={}) 丢了
    GRAPE_SERVER_URL（远程 worker 上 CLI 落 localhost:8000，A2A 交办全连不上），
    同时丢 GRAPE_WORK_BRANCH / GRAPE_SCENE_ID / custom_fields / GRAPE_LINKED_REPOS
    （worktree 建错分支、场景自定义变量失效）。本函数对齐 _enqueue_pipeline_task
    （原 L2243-2293）的 env 组装，重派处调用方按 old.runtime_id 重建。

    与正常入队的差异（调用方已持有或无意义的不重复注入）：
    - ISSUE_NUMBER/PR_NUMBER：参数传入则注入
    - BOT_USERNAME（coding）：与正常入队同逻辑
    - evt._env_extra 事件级扩展：重派无 evt，无法还原（快照已在 prompt 里），不注入
    """
    import json as _json
    env = {}
    # ── 仓库/服务地址基础四项 ──
    from src.repo_key import split_owner_repo as _sor
    _owner, _repo_name = _sor(repo_key)
    env["REPO_FULL_NAME"] = _repo_name and f"{_owner}/{_repo_name}" or repo_key
    env["REPO_OWNER"] = _owner
    env["REPO_NAME"] = _repo_name
    env["ACCOUNT"] = account
    from src.prompt_builder import _resolve_server_url
    env["GRAPE_SERVER_URL"] = _resolve_server_url(runtime_id)
    # ── 场景环境（work_branch / scene_id / custom_fields / linked_repos）──
    try:
        from src.runtime.repogit import _read_scene_work_branch
        _wb = _read_scene_work_branch(repo_key, issue_id)
        if _wb:
            env["GRAPE_WORK_BRANCH"] = _wb
    except Exception:
        pass
    _inject_scene_env(env, repo_key, issue_id, agent_type)
    try:
        _linked = _read_scene_linked_repos(repo_key, issue_id)
        if _linked:
            env["GRAPE_LINKED_REPOS"] = _json.dumps(_linked, ensure_ascii=False)
    except Exception:
        pass
    # ── coding 注入 BOT_USERNAME（审查人）──
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
                _bot_name = _owner
            env["BOT_USERNAME"] = _bot_name
        except Exception:
            pass
    # ── issue/pr 编号 ──
    if issue_number:
        env["ISSUE_NUMBER"] = str(issue_number).lstrip("#")
    if pr_number:
        env["PR_NUMBER"] = str(pr_number)
    return env


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

    # 1) project-info + knowledge 物化（Issue #95：统一调 collect_knowledge_files）
    proj_dir = os.path.join(ws, ".project-info")
    know_dir = os.path.join(ws, ".knowledge")
    try:
        from src.runtime.service import collect_knowledge_files
        _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=_issue)
        # project-info 物化（baseline + member）
        for rel, meta in (_collected.get("project_info") or {}).items():
            if rel == ".metadata.json":
                continue
            _dest = os.path.join(proj_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                _content = meta.get("content", "") if isinstance(meta, dict) else str(meta)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(_content)
            except Exception:
                pass
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
        # knowledge 物化（agent 沉淀知识）
        for rel, meta in (_collected.get("knowledge") or {}).items():
            _dest = os.path.join(know_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                _content = meta.get("content", "") if isinstance(meta, dict) else str(meta)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(_content)
            except Exception:
                pass
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"direct knowledge materialize failed for {repo_key}: {e}")

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

    return prompt


def _submit_direct_linked_prs(env_extra: dict, repo_key: str, issue_id: str,
                              repo_gitcode_token: str = ""):
    """Issue #242: direct 模式 coding agent 完成后，向关联仓库提交 PR。

    与 worker 端 _submit_linked_repo_prs 等价：遍历 GRAPE_LINKED_WORKTREES，
    对每个有改动的关联仓库 worktree 执行 push + create_pr_for_repo。
    失败只告警，不阻塞任务完成与主仓库 PR 流程。
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    from src.runtime import repogit
    from src.config import log as _log

    _linked_wt_json = env_extra.get("GRAPE_LINKED_WORKTREES", "")
    if not _linked_wt_json:
        return
    try:
        _wts = _json.loads(_linked_wt_json)
    except Exception:
        return
    if not _wts or not isinstance(_wts, list):
        return

    _issue_num = repogit.issue_num_from(issue_id)
    if not _issue_num:
        _log.warning("direct linked PR: cannot determine issue_num, skip")
        return

    _wt_branch = f"feat/issue-{_issue_num}"
    _bot_name = env_extra.get("BOT_USERNAME") or "grape_bot"
    _results = []

    for wt in _wts:
        if not isinstance(wt, dict):
            continue
        _rk = wt.get("repo_key") or ""
        _wt_dir = wt.get("dir") or ""
        _base_branch = wt.get("branch") or "main"
        if not _rk or not _wt_dir or not _os.path.isdir(_wt_dir):
            _log.warning(f"direct linked PR: skip invalid worktree: rk={_rk} dir={_wt_dir}")
            continue

        # 0. 分支护栏：检查当前分支是否是 feat/issue-{N}，禁止直推基线分支
        from src.runtime.repogit import ensure_feature_branch
        ensure_feature_branch(_wt_dir, _wt_branch, _base_branch, logger=_log)

        try:
            # 1. git identity（防 commit 被拒）
            _sp.run(["git", "-C", _wt_dir, "config", "user.name", _bot_name],
                    capture_output=True, text=True, timeout=10)
            _sp.run(["git", "-C", _wt_dir, "config", "user.email",
                     f"{_bot_name}@noreply.git.com"],
                    capture_output=True, text=True, timeout=10)

            # 2. stage 未提交改动
            _sp.run(["git", "-C", _wt_dir, "add", "-A"],
                    capture_output=True, text=True, timeout=60)
            _r_diff = _sp.run(["git", "-C", _wt_dir, "diff", "--cached", "--quiet"],
                              capture_output=True, text=True, timeout=30)
            _has_staged = _r_diff.returncode != 0

            # 3. 检查是否有领先于 base 分支的提交
            _r_ahead = _sp.run(
                ["git", "-C", _wt_dir, "rev-list", "--count",
                 f"origin/{_base_branch}..HEAD"],
                capture_output=True, text=True, timeout=15)
            _ahead = 0
            if _r_ahead.returncode == 0:
                try:
                    _ahead = int((_r_ahead.stdout or "0").strip() or "0")
                except ValueError:
                    _ahead = 0
            else:
                _ahead = 1  # origin/{base} 不存在 → 保守认为有改动

            if not _has_staged and _ahead == 0:
                _log.info(f"direct linked PR: no changes in {_rk}, skip")
                _results.append({"repo_key": _rk, "status": "skipped", "reason": "no changes"})
                continue

            # 4. commit staged changes
            if _has_staged:
                _msg = f"feat: #{_issue_num} linked repo changes"
                _r_commit = _sp.run(["git", "-C", _wt_dir, "commit", "-m", _msg],
                                    capture_output=True, text=True, timeout=60)
                if _r_commit.returncode != 0:
                    _log.warning(f"direct linked PR: commit failed {_rk}: {(_r_commit.stderr or '')[:200]}")
                    _results.append({"repo_key": _rk, "status": "failed", "reason": "commit failed"})
                    continue

            # 5. push（token 经 http.extraheader 注入）
            _r_push = repogit._run_git(
                ["-C", _wt_dir, "push", "origin", f"HEAD:refs/heads/{_wt_branch}"],
                token=repo_gitcode_token, timeout=120)
            if _r_push is None or _r_push.returncode != 0:
                _err = (_r_push.stderr[:300] if _r_push else "git exec failed") if _r_push else "git exec failed"
                _log.warning(f"direct linked PR: push failed {_rk}: {_err}")
                _results.append({"repo_key": _rk, "status": "failed", "reason": f"push: {_err[:100]}"})
                continue

            # 6. create PR
            from src.vcs.api_client import create_pr_for_repo
            _pr_title = f"feat: #{_issue_num} linked repo ({_rk})"
            _pr_body = (f"跨仓库需求开发 PR（Issue #{_issue_num}）\n\n"
                        f"由 coding agent 自动提交，关联主仓库 {repo_key}。\n\n"
                        f"Closes #{_issue_num}")
            _pr_result = create_pr_for_repo(
                repo_key=_rk, token=repo_gitcode_token,
                title=_pr_title, head=_wt_branch, base=_base_branch,
                body=_pr_body)
            if isinstance(_pr_result, dict) and "error" not in _pr_result:
                _pr_num = _pr_result.get("number") or ""
                _pr_url = _pr_result.get("html_url") or ""
                _log.info(f"direct linked PR: created #{_pr_num} for {_rk}: {_pr_url}")
                _results.append({"repo_key": _rk, "status": "created",
                                 "pr_number": _pr_num, "pr_url": _pr_url})
            else:
                _err = _pr_result.get("error", "unknown") if isinstance(_pr_result, dict) else "unknown"
                _log.warning(f"direct linked PR: create_pr failed {_rk}: {_err}")
                _results.append({"repo_key": _rk, "status": "failed", "reason": f"PR create: {_err}"})
        except Exception as e:
            _log.warning(f"direct linked PR: error for {_rk}: {e}")
            _results.append({"repo_key": _rk, "status": "error", "reason": str(e)[:200]})

    _created = [r for r in _results if r.get("status") == "created"]
    _skipped = [r for r in _results if r.get("status") == "skipped"]
    _failed = [r for r in _results if r.get("status") in ("failed", "error")]
    _log.info(f"direct linked PR summary: {len(_created)} created, {len(_skipped)} skipped, {len(_failed)} failed")


def _cleanup_direct_workdir(repo_key: str, agent_type: str, issue_id: str = ""):
    """direct 端任务级工位清理：删轻文件（.skills/.project-info/.knowledge）+ 安全卸载 worktree。

    在 _execute_agent 的 finally 中调用，覆盖正常/失败/异常全部路径且幂等。
    清理失败只告警，不影响事件结果上报。

    Issue #242: coding agent 多仓库时，同步清理关联仓库 worktree。
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
                from src.repo import Repo
                _token = Repo(repo_key).bot_token
            except Exception:
                pass
            repogit.remove_issue_worktree_if_pushed(repo_key, agent_type, _num, token=_token)
            # Issue #242: 清理关联仓库 worktree（coding agent 多仓库）
            # 关联 worktree 路径由 repogit.issue_dir_for(repo_key, "coding", issue_num) 推导，
            # 通过查询场景 linked_repos 获取关联仓库列表（与初始化路径对称）
            if agent_type == "coding":
                try:
                    _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
                    for lr in (_linked_repos or []):
                        if not isinstance(lr, dict):
                            continue
                        _lrk = lr.get("repo_key") or ""
                        if _lrk:
                            repogit.remove_issue_worktree_if_pushed(
                                _lrk, "coding", _num, token=_token)
                except Exception as _le:
                    log.warning(f"direct linked worktree cleanup failed: {_le}")
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
        # Issue-45 M2：direct 会话结束 → 会话级临时 token 即时失效
        try:
            from src.api.auth_token import revoke_session_token
            revoke_session_token(task_id or f"d_{seq}")
        except Exception as e:
            log.warning(f"Exec agent: revoke session token failed: {e}")
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
    from src.runtime.executor import stream_devops_agent
    from src.prompt_builder import build_devops_agent_prompt
    from src.api.event_store import (
        update_event_config, update_event_status, update_event_complete, evaluate_agent_output, _extract_tool_command,
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
            from src.api.session_store import get_or_create_issue_session, guard_issue_session_pointer
            _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
            issue_session_id = _isess.get("id") or ""
            # session 引擎守卫：backend 即本次引擎名，跨引擎指针弃用（防 hermes resume
            # opencode 会话号静默挂死）；引擎未知 → 放行。
            oc_session_id = guard_issue_session_pointer(_isess, backend or "")
        # 无关联 Issue 任务不执行（push 忽略），不再回退读 repo 级 runtime_session_id。
    except Exception:
        pass

    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] type={agent_type}, backend={inst_name}, skill={skill_name}, oc_session={oc_session_id[:20] if oc_session_id else 'none'}")

    # 获取 bot token（按 provider 分流：GitHub 仓库用 get_github_token，其他用 get_gitcode_token）
    repo_gitcode_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_gitcode_token = get_gitcode_token(repo_key=repo_name) or ""
    except Exception as e:
        log.warning(f"Exec agent: get bot token failed: {e}")
    log.info(f"Exec agent: bot_token={'set' if repo_gitcode_token else 'none'} (provider={'github' if repo_key.startswith('github:') else 'gitcode'})")

    # Agent 显示名（注入 AGENT_NAME 环境变量，skill 中 [$AGENT_NAME] 使用）
    agent_name = get_agent_display_name(agent_type)

    try:
        from src.prompt_builder import build_devops_agent_prompt
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
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
        _prompt_skip_iid = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "")
        if _prompt_skip_iid and not str(_prompt_skip_iid).startswith("#"):
            _prompt_skip_iid = f"#{_prompt_skip_iid}"
        append_skipped_event(repo_key, evt.get("type", ""), "prompt_build_failed", str(e),
                             issue_id=_prompt_skip_iid)
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
        "REPO_OWNER": (exec_account or repository).split("/", 1)[0],
        "REPO_NAME": (exec_account or repository).rsplit("/", 1)[-1],
        "ACCOUNT": evt.get("actor") or exec_account or "",
    }
    # 注入场景环境变量（scene_id / work_branch / custom_fields；direct 模式同样注入，保持与 worker 模式一致）
    try:
        from src.runtime.repogit import _read_scene_work_branch
        _wb = _read_scene_work_branch(repo_key, issue_id)
        if _wb:
            env_extra["GRAPE_WORK_BRANCH"] = _wb
    except Exception:
        pass
    _inject_scene_env(env_extra, repo_key, issue_id, agent_type)
    # Issue #242: 注入关联仓库列表（coding agent 读取后初始化多仓库 worktree）
    try:
        _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
        if _linked_repos:
            env_extra["GRAPE_LINKED_REPOS"] = json.dumps(_linked_repos, ensure_ascii=False)
    except Exception:
        pass
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
            _wb = env_extra.get("GRAPE_WORK_BRANCH", "")
            _wt = repogit.ensure_issue_worktree(repo_key, agent_type, _gnum,
                                                token=repo_gitcode_token, work_branch=_wb)
            if _wt:
                env_extra["GIT_ASKPASS"] = repogit.askpass_path(_wt)
    except Exception as e:
        log.warning(f"Exec agent: git worktree ensure failed: {e}")

    # Issue #242: 关联仓库 worktree 初始化（direct 模式 coding/sre agent 多仓库并行操作）
    # 与 worker 端 _execute_task 中的逻辑等价：遍历 GRAPE_LINKED_REPOS → ensure_linked_repo_worktrees
    # → 注入 GRAPE_LINKED_WORKTREES（供 _submit_linked_repo_prs 消费 / SRE 构建部署消费）
    try:
        from src.runtime import repogit
        _linked_repos_env_dr = env_extra.get("GRAPE_LINKED_REPOS", "")
        if _linked_repos_env_dr and agent_type in ("coding", "sre", "qa"):
            import json as _json_lr_dr
            _linked_repos_list_dr = _json_lr_dr.loads(_linked_repos_env_dr) if _linked_repos_env_dr else []
            if _linked_repos_list_dr:
                _gnum_lr = repogit.issue_num_from(issue_id)
                _wt_list_dr = repogit.ensure_linked_repo_worktrees(
                    _linked_repos_list_dr, agent_type, _gnum_lr,
                    token=repo_gitcode_token)
                if _wt_list_dr:
                    env_extra["GRAPE_LINKED_WORKTREES"] = _json_lr_dr.dumps(
                        [{"repo_key": w["repo_key"], "branch": w["branch"], "dir": w["worktree_dir"]}
                         for w in _wt_list_dr], ensure_ascii=False)
                    log.info(f"Exec agent: linked repos: {len(_wt_list_dr)} worktrees initialized for issue={_gnum_lr} agent={agent_type}")
    except Exception as e:
        log.warning(f"Exec agent: linked repos worktree init failed: {e}")

    # Issue-45 M2：direct 会话签发会话级临时 token（session_key=task_id 或 d_{seq}）
    try:
        from src.api.auth_token import issue_session_token
        if repo_key and agent_type:
            env_extra["GRAPE_SESSION_TOKEN"] = issue_session_token(
                session_key=task_id or f"d_{seq}",
                repo_key=repo_key, agent_type=agent_type, user_id=0)
            # CLI 命令按 token 作用域过滤：agent 持 tk_ 会话 token → 只暴露 dual+session_only 命令
            # （见 grape-cli-mode-filter-design.md；避免 agent 打 user_only 接口 403 重试）
            env_extra["GRAPE_CLI_MODE"] = "WORKER"
    except Exception as e:
        log.warning(f"Exec agent: issue session token failed: {e}")

    try:
        for evt_type, data in stream_devops_agent(
            prompt, ws, session_id=oc_session_id, event_type=evt.get("type", ""),
            backend_name=backend, account=exec_account, bot_token=repo_gitcode_token,
            agent_name=agent_name,
            agent_label=f"[Agent:{agent_type}|repo:{repo_name}|engine:{inst_name}]",
            env_extra=env_extra,
            repo_key=repo_key,
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
                # 智能推荐完成处理：解析 scene_id → 持久化 → 重派 VOD
                if agent_type == "vod" and final_text and issue_id:
                    _task_prompt = ""
                    try:
                        from src.session_manager import db as _rdb, db_lock as _rdb_lock
                        with _rdb_lock:
                            c = _rdb.cursor()
                            c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
                            _trow = c.fetchone()
                            _task_prompt = _trow[0] if _trow else ""
                    except Exception:
                        pass
                    # 判断是否为智能推荐任务：prompt 含"智能推荐器"且无 {{SKILL_DIR}}
                    if _task_prompt and "智能推荐器" in _task_prompt and "{{SKILL_DIR}}" not in _task_prompt:
                        try:
                            import re as _re
                            _rec_scene = ""
                            # 优先解析 JSON
                            try:
                                from src.api.event_store import parse_agent_output_json
                                _scene_out = parse_agent_output_json(final_text) or {}
                                _rec_scene = _scene_out.get("scene_id", "") or ""
                            except Exception:
                                pass
                            # 正则兜底
                            if not _rec_scene:
                                _m_rec = _re.search(r'"scene_id"\s*:\s*"([^"]*)"', final_text)
                                _rec_scene = _m_rec.group(1) if _m_rec else ""
                            if _rec_scene:
                                from src.api.session_store import set_issue_scene
                                from src.runtime.engine_select import _repo_user_id
                                _vod_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))
                                set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
                                log.info(f"智能推荐完成: scene_id={_rec_scene} set for {issue_id}")
                                # 重派 VOD（用匹配场景的 vod 技能）
                                from src.api.pipeline_config import get_first_node, get_pipeline_config
                                _vod_node = get_first_node(_rec_scene, repo_key=repo_key)
                                if not _vod_node:
                                    _pcfg = get_pipeline_config(_rec_scene, repo_key=repo_key)
                                    _nodes = (_pcfg or {}).get("nodes", [])
                                    if _nodes:
                                        _vod_node = _nodes[0]
                                if _vod_node:
                                    _vod_skill = _vod_node.get("skill", "vod")
                                    log.info(f"智能推荐完成: redispatch VOD with skill={_vod_skill} for {issue_id}")
                                    _redispatch_vod_after_recommend(repo_key, issue_id, _vod_skill, evt, runtime_id, repo_gitcode_token)
                                else:
                                    log.warning(f"智能推荐完成: no vod node for scene={_rec_scene}, skip redispatch")
                            else:
                                log.info(f"智能推荐完成: no scene_id for {issue_id}, pipeline terminated")
                        except Exception as _se:
                            log.warning(f"智能推荐完成处理失败: {_se}")
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

    # 更新 oc_session_id（持久化对话上下文）——仅写回 Issue 级执行会话
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回写。
    # 同步记录 session_engine（本次执行引擎），供消费端跨引擎守卫比对。
    if new_oc_session_id and new_oc_session_id != oc_session_id:
        try:
            if issue_session_id:
                from src.api.session_store import update_issue_session
                update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id,
                                     session_engine=backend or "")
        except Exception as e:
            log.warning(f"Exec agent: failed to update oc_session_id: {e}")

    # ── 单信号收尾（C，stream-end-finalize-single-signal-design.md）──
    # 引擎流结束即终态触发点；stream_ended 仅作仪器记录，不再按技能类型区分/门控。
    if task_id:
        try:
            from src.runtime.service import set_task_signal
            set_task_signal(task_id, "stream_ended")
        except Exception as _e:
            log.warning(f"Exec agent: set stream_ended failed for {task_id}: {_e}")

    # 判定 Agent 是否有明确答复产出
    has_output, result_text = evaluate_agent_output(agent_type, tool_uses, final_text)
    if repo_gitcode_token and result_text:
        result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
    # 若 Agent 已显式 self_report（结构化结论），以其为权威（产物而非闸门）
    if task_id:
        try:
            from src.runtime.service import get_task
            _t = get_task(task_id) or {}
            if _t.get("self_reported") == 1 and _t.get("result_text"):
                import json as _json
                _stored = _t.get("result_text") or ""
                _sd = None
                try:
                    _sd = _json.loads(_stored)
                except Exception:
                    _sd = None
                if _sd and isinstance(_sd, dict) and (_sd.get("actions") or _sd.get("conclusion")):
                    result_text = _stored
        except Exception as _e:
            log.warning(f"Exec agent: self-reported result merge failed for {task_id}: {_e}")
    completed_at = datetime.utcnow().isoformat()
    duration = None
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(completed_at)
        duration = round((e - s).total_seconds(), 1)
    except Exception:
        pass

    # 有产出 → 更新该 event（写入配置 + 结果 + 结论 + 执行日志）
    # exit_code < 0 表示进程被信号终止（如 -15=SIGTERM, -9=SIGKILL），
    # 但 has_output=True 说明 Agent 已完成业务产出（PR/测试/评论/结构化 conclusion），
    # 信号终止常发生在引擎完成输出后的进程清理阶段，不应判为失败。
    if exit_code == 0 or (has_output and exit_code < 0):
        new_status = "completed"
        if exit_code < 0:
            log.info(f"Exec agent: seq={seq} exit_code={exit_code} but has_output=True, mark completed (signal death after work done)")
        # ── 假完成硬门槛（issue-P5，关卡B）──
        # 引擎 exit 0 不再是成功的充分条件：devops 类任务（issue/coding/qa/sre）还须有
        # 可信产出证据之一，否则降级 failed（防模型流挂死/空 end_turn 静默收场标绿）：
        #   证据1 self_reported==1 且有 result_text —— grape tasks complete 服务端盖章自报；
        #   证据2 执行日志（tool_uses，实际执行过的命令）中有真实写操作：
        #         POST .../comments（评论）/ POST .../pulls（PR）/ POST /api/a2a（A2A交棒）。
        # 注意只看执行证据，不看 agent 文字自述（思考里"打算交棒"不算做过）。
        # vod 等非 devops agent（智能推荐路径）不加门槛，避免误伤。
        if exit_code == 0 and agent_type in ("issue", "coding", "qa", "sre") and not has_output:
            _self_ok = False
            if task_id:
                try:
                    from src.runtime.service import get_task as _gt
                    _trow = _gt(task_id) or {}
                    _self_ok = (_trow.get("self_reported") == 1 and bool(_trow.get("result_text")))
                except Exception:
                    _self_ok = False
            _write_action = False
            if not _self_ok:
                for _tu in (tool_uses or []):
                    _cmd = _tu.get("cmd", "") if isinstance(_tu, dict) else str(_tu)
                    _cmd = _extract_tool_command(_tu) if (not isinstance(_cmd, str) or "POST" not in _cmd.upper()) else _cmd
                    _up = _cmd.upper()
                    # A2A 有两种形态：直接 curl POST /api/a2a，或 grape CLI 的 `grape a2a handoff`
                    # （CLI 内部才发 POST，命令字符串里没有 POST 关键字，须单独匹配）
                    if (("POST" in _up and any(k in _up for k in ("/COMMENTS", "/PULLS", "MERGE_REQUESTS")))
                            or ("A2A HANDOFF" in _up) or ("POST" in _up and "/API/A2A" in _up)):
                        _write_action = True
                        break
            if not (_self_ok or _write_action):
                new_status = "failed"
                log.warning(f"Exec agent: seq={seq} exit_code=0 but no verified output "
                            f"(self_reported={_self_ok}, write_action={_write_action}), "
                            f"demoting to failed (fake completion guard)")
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
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}\n")

    # Issue #242: coding agent 成功完成后，向关联仓库提交 PR（direct 模式）
    # 遍历 GRAPE_LINKED_WORKTREES，对每个有改动的关联仓库 worktree 执行 push + create_pr_for_repo
    if new_status == "completed" and agent_type == "coding" and env_extra.get("GRAPE_LINKED_WORKTREES"):
        try:
            _submit_direct_linked_prs(env_extra, repo_key, issue_id, repo_gitcode_token)
        except Exception as _le:
            log.warning(f"Exec agent: direct linked PR submit failed: {_le}")

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
    # PR 订阅的伪 issue_id（#pr_123_scene_id）不是真实 Issue，跳过 Issue 回帖
    if new_status == "completed" and result_text and issue_id and not evt.get("_pr_sub_pr_number"):
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            _post_result_to_issue(repository, _issue_num, result_text, repo_gitcode_token, agent_type, repo_key)
    # PR 订阅场景 → 回帖到 PR 评论
    if new_status == "completed" and result_text and evt.get("_pr_sub_pr_number"):
        _pr_num = str(evt["_pr_sub_pr_number"])
        if _pr_num and repo_gitcode_token:
            _post_result_to_pr(repository, _pr_num, result_text, repo_gitcode_token, agent_type, repo_key)

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

    # Issue #51: SRE（最后节点）完成后 → 设 stage=pending_review 等待开发者审核
    if new_status == "completed" and agent_type == "sre" and issue_id:
        try:
            from src.api.session_store import get_issue_stage, set_issue_stage
            _st = get_issue_stage(repo_key, issue_id)
            _cur_stage = (_st or {}).get("stage", "") if _st else ""
            if _cur_stage == "sre":
                set_issue_stage(repo_key, issue_id, "pending_review", "sre")
                log.info(f"SRE completed → pending_review (awaiting user review) issue={issue_id}")
        except Exception as _e:
            log.warning(f"SRE → pending_review failed: {_e}")

    # 子任务 SRE 完成后 → 登记完成情况到父 Issue
    if new_status == "completed" and agent_type == "sre" and issue_id:
        try:
            from src.api.session_store import get_parent_issue, get_subtask_issues
            _parent = get_parent_issue(repo_key, issue_id)
            if _parent and repo_gitcode_token:
                _parent_num = _issue_num_from(_parent)
                _sub_num = _issue_num_from(issue_id)
                if _parent_num and _sub_num:
                    _post_subtask_completion_to_parent(repository, _sub_num, _parent_num, result_text, repo_gitcode_token, repo_key=repo_key)
                    # 检查所有子任务是否全部完成
                    _subs = get_subtask_issues(repo_key, _parent)
                    _all_done = all(s.get("stage") == "done" or s.get("issue_id") == issue_id for s in _subs)
                    if _all_done and len(_subs) > 0:
                        _post_all_subtasks_done(repository, _parent_num, _subs, repo_gitcode_token, repo_key=repo_key)
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
                    _c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
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


def _post_result_to_issue(repository: str, issue_number, result_text: str, bot_token: str, agent_type: str,
                          repo_key: str = ""):
    """Agent 执行完成后，将结果作为评论发回 VCS Issue。

    根据 repo_key provider 分流 GitHub/GitCode。
    方案 B：先 GET Issue 评论列表，若已有 agent 发的评论则跳过系统回帖，
    避免与 agent 自己发的评论重复（"一鱼两吃"问题）。
    agent 忘了发评论时系统补发，保证 Issue 不会静悄悄。
    """
    if not repository or not issue_number or not bot_token:
        return
    # 跳过场景推荐结果（借 VOD 身份做的推荐，非真正工作产出，不应发到 Issue）
    try:
        import json as _json
        _parsed = _json.loads(result_text)
        if isinstance(_parsed, dict) and _parsed.get("scene_id") and _parsed.get("reason") and not _parsed.get("conclusion"):
            log.info(f"Skip posting scene recommendation result to issue {issue_number}")
            return
    except Exception:
        pass
    # 过滤：优先提取结构化 conclusion，避免思考过程/JSON 块泄漏到 Issue 评论
    _conclusion = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _parsed = parse_agent_output_json(result_text)
        if _parsed and _parsed.get("conclusion"):
            _conclusion = _parsed["conclusion"]
    except Exception:
        pass
    # conclusion 提取失败时用简洁默认消息，不发原始 result_text（含思考过程/JSON）
    if not _conclusion:
        _conclusion = f"任务已完成（{agent_type} agent）。"

    # 根据 repo_key provider 分流
    if _is_github_repo(repo_key):
        try:
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            # 先查 Issue 已有评论，若已有 agent 评论则跳过（方案 B）
            _existing = _github_list_issue_comments(_owner, _repo, str(issue_number), bot_token)
            _agent_tag_lower = agent_type.lower()
            for _c in _existing:
                _body = (_c.get("body") or "").lower()
                if _agent_tag_lower in _body and "agent]" in _body:
                    log.info(f"Exec agent: skip system comment, agent already posted to {repository}/issues/{issue_number}")
                    return
            # agent 没发评论，系统补发（只发 conclusion，不发原始 result_text）
            _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
            _comment += "\n<!-- grape-auto-comment -->"
            _github_post_issue_comment(_owner, _repo, str(issue_number), _comment, bot_token)
            log.info(f"Exec agent: posted system comment (agent did not post) to {repository}/issues/{issue_number}")
        except Exception as e:
            log.warning(f"Exec agent: failed to post result comment (GitHub): {e}")
        return

    # GitCode 原逻辑
    try:
        import urllib.request, json as _json
        # 先查 Issue 已有评论，若已有 agent 评论则跳过（方案 B）
        _list_url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments?per_page=100"
        _list_req = urllib.request.Request(_list_url, headers={
            "Authorization": f"Bearer {bot_token}",
        })
        _resp = urllib.request.urlopen(_list_req, timeout=15)
        _existing = _json.loads(_resp.read())
        _agent_tag_lower = agent_type.lower()
        for _c in _existing:
            _body = (_c.get("body") or "").lower()
            # 匹配评论前缀中包含 agent_type 的（如 [blog-vod Agent] 匹配 vod）
            if _agent_tag_lower in _body and "agent]" in _body:
                log.info(f"Exec agent: skip system comment, agent already posted to {repository}/issues/{issue_number}")
                return
        # agent 没发评论，系统补发（只发 conclusion，不发原始 result_text）
        _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
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
        log.info(f"Exec agent: posted system comment (agent did not post) to {repository}/issues/{issue_number}")
    except Exception as e:
        log.warning(f"Exec agent: failed to post result comment: {e}")



def _post_subtask_completion_to_parent(repository: str, sub_issue_num: str, parent_issue_num: str,
                                        result_text: str, bot_token: str, repo_key: str = ""):
    """子任务完成后，在父 Issue 评论登记完成情况。"""
    try:
        import urllib.request, json as _json
        from src.api.event_store import parse_agent_output_json
        _out = parse_agent_output_json(result_text)
        _summary = (_out.get("conclusion", "") if _out else "") or result_text[:200]
        _comment = f"### \u5b50\u4efb\u52a1 #{sub_issue_num} \u5df2\u5b8c\u6210\n\n{_summary}\n\n---\n\u5b50\u4efb\u52a1\u6d41\u6c34\u7ebf\u5df2\u5168\u90e8\u5b8c\u6210\u3002"
        _comment += "\n<!-- grape-auto-comment -->"
        if _is_github_repo(repo_key):
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            _github_post_issue_comment(_owner, _repo, parent_issue_num, _comment, bot_token)
        else:
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{parent_issue_num}/comments"
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(_req, timeout=15)
        log.info(f"Subtask #{sub_issue_num} completion posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post subtask completion failed: {e}")


def _post_all_subtasks_done(repository: str, parent_issue_num: str, subtasks: list, bot_token: str,
                            repo_key: str = ""):
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
        if _is_github_repo(repo_key):
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            _github_post_issue_comment(_owner, _repo, parent_issue_num, _comment, bot_token)
        else:
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{parent_issue_num}/comments"
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(_req, timeout=15)
        log.info(f"All subtasks done summary posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post all subtasks done failed: {e}")


def _post_result_to_pr(repository: str, pr_number: str, result_text: str, bot_token: str,
                        agent_type: str, repo_key: str = ""):
    """PR 订阅场景：Agent 执行完成后，将结果作为评论发回 PR。

    GitCode: POST /repos/{owner}/{repo}/pulls/{pr_number}/comments
    GitHub:  POST /repos/{owner}/{repo}/issues/{pr_number}/comments（PR 共用 Issue 评论 API）
    """
    if not repository or not pr_number or not bot_token:
        return
    # 提取 conclusion
    _conclusion = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _parsed = parse_agent_output_json(result_text)
        if _parsed and _parsed.get("conclusion"):
            _conclusion = _parsed["conclusion"]
    except Exception:
        pass
    if not _conclusion:
        _conclusion = f"PR 审查任务已完成（{agent_type} agent）。"
    _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
    _comment += "\n<!-- grape-auto-comment -->"
    try:
        import urllib.request, json as _json
        if _is_github_repo(repo_key):
            # GitHub: PR 共用 Issue 评论 API
            _url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "Grape-DevOps",
                         "Content-Type": "application/json"}, method="POST")
        else:
            # GitCode: PR 评论 API
            _url = f"https://gitcode.com/api/v5/repos/{repository}/pulls/{pr_number}/comments"
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}",
                         "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(_req, timeout=15)
        log.info(f"PR-sub: posted result comment to {repository}/pulls/{pr_number}")
    except Exception as e:
        log.warning(f"PR-sub: post result comment failed: {e}")


def _sync_issue_comments_to_conversation(repo_key: str, issue_number, bot_token: str,
                                         agent_type: str = "", repository: str = ""):
    """从 VCS Issue 拉取 agent 发的评论，去重后同步到任务会话。

    根据 repo_key provider 分流 GitHub/GitCode。
    VOD 等 agent 通过技能 curl 直接在 Issue 上发澄清追问评论，这些评论内容
    不会出现在 result_text（结构化输出摘要）中。此函数补拉 Issue 上的实际评论，
    与会话已有消息去重后写入，确保 Chat Agent「去对话」可见完整内容。

    agent_type 为空时同步所有 agent 前缀评论（用于打开会话时全量补拉）。
    """
    import re
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
            c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                      (rk, issue_number))
            row = c.fetchone()
        if not row or not row[0]:
            return
        conv_id = row[0]
    except Exception as e:
        log.warning(f"Sync issue comments: find conversation failed: {e}")
        return

    # 2) 从 VCS API 拉取 Issue 评论
    comments = []
    try:
        if _is_github_repo(rk):
            # GitHub API
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(rk)
            comments = _github_list_issue_comments(_owner, _repo, str(issue_number), bot_token)
        else:
            # GitCode API 原逻辑
            import urllib.request
            import json as _json
            if not repository:
                from src.repo_key import from_repo_key
                repository = from_repo_key(rk)
            api_url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments"
            req = urllib.request.Request(api_url)
            req.add_header("Authorization", f"Bearer {bot_token}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                comments = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"Sync issue comments: fetch from VCS failed: {e}")
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


def _redispatch_vod_after_recommend(repo_key: str, issue_id: str, vod_skill: str,
                                     evt: dict, runtime_id: int, repo_gitcode_token: str):
    """智能推荐完成后，重派 VOD 任务（用匹配场景的 vod 技能）。

    创建新 event（a2a_event 类型，豁免 dedup），构建 VOD prompt 并派发。
    """
    from src.api.event_store import append_event
    from src.runtime.engine_select import pick_engine, engine_name
    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT
    from src.api.event_pipeline import is_plugin_mode

    # 获取 Issue 标题
    _title = ""
    _issue_num = str(issue_id).lstrip("#")
    try:
        from src.api.issues_center import _get_issue_title
        _title = _get_issue_title(repo_key, int(_issue_num)) or ""
    except Exception:
        pass
    if not _title:
        _title = f"VOD redispatch: {vod_skill}"

    # 创建新 event
    seq = append_event(
        repo_key=repo_key,
        event_type="a2a_event",
        title=_title,
        issue_id=issue_id,
        source="a2a",
        agent_type="vod",
        actor="",
        summary=f"智能推荐完成 → VOD ({vod_skill})",
        payload={"repository": evt.get("repository", ""), "issue_number": _issue_num,
                 "context": "智能推荐完成，重派 VOD 执行完整工作流"},
        status="running",
        entity_type="issue",
    )
    log.info(f"智能推荐重派: event created, seq={seq}")

    # 构建场景解析
    from src.api.session_store import get_issue_scene
    _scene_id, _scene_uid = get_issue_scene(repo_key, issue_id)
    _scene_id = _scene_id or "default"
    if not _scene_uid:
        from src.runtime.engine_select import _repo_user_id
        _scene_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))

    # skill 解析
    from src.user.db import resolve_skill_id, get_skill_by_id, get_skill_path
    skill_name = vod_skill
    skill_id = resolve_skill_id(vod_skill, repo_key=repo_key, user_id=_scene_uid) or 0
    skill_path = vod_skill
    if skill_id:
        _sk = get_skill_by_id(skill_id)
        skill_path = (_sk.get("skill_path") or vod_skill) if _sk else vod_skill

    # 构建 evt 副本（_enqueue_pipeline_task 内部会构建 prompt）
    evt_copy = dict(evt)
    evt_copy["repo_key"] = repo_key
    evt_copy["_skill_path"] = skill_path
    evt_copy["issue_id"] = issue_id

    # 选引擎
    engine_id = pick_engine(repo_key, "vod", fallback_runtime_id=runtime_id or 0,
                            scene_id=_scene_id, user_id=_scene_uid)
    if engine_id is None:
        log.warning(f"智能推荐重派: no engine available, skip")
        _fail_event(seq, "无可用引擎（engine_unavailable）")
        return

    _be = engine_name(engine_id) or "opencode"

    # 入队或直接执行（_enqueue_pipeline_task 内部会构建 prompt）
    kind = get_execution_mode()
    if kind != EXEC_MODE_DIRECT and (engine_id > 0 or (engine_id == 0 and is_plugin_mode())):
        _pool_eids = ""
        if is_plugin_mode():
            from src.runtime.engine_select import get_scene_pool_engine_ids
            _pool_eids = get_scene_pool_engine_ids(_scene_id, "vod", repo_key=repo_key, user_id=_scene_uid)
        # Issue #259：None = 场景引擎全灭（方案A，不入队）
        if _pool_eids is None:
            _fail_scene_all_offline(seq, _scene_id, "vod", "智能推荐重派", repo_key=repo_key)
            return False
        _enqueue_pipeline_task(repo_key, seq, "vod", _be, vod_skill, evt_copy,
                               skill_id, skill_path, engine_id, issue_id,
                               pool_engine_ids=_pool_eids, user_id=_scene_uid,
                               is_scene_recommend=False)
        log.info(f"智能推荐重派: enqueued VOD task, seq={seq}, skill={vod_skill}")
    else:
        # direct 模式
        evt_copy["_skill_path"] = skill_path
        evt_copy["_skill_id"] = skill_id
        _direct_tid = _create_direct_task(repo_key, seq, "vod", _be, vod_skill,
                                          skill_path, engine_id, evt_copy, issue_id, skill_id=skill_id)
        start_direct_agent(_execute_agent, (
            repo_key, seq, "vod", _be, vod_skill, evt_copy, engine_id, _direct_tid,
        ), name=f"ag-vod-{seq}")
        log.info(f"智能推荐重派: started direct VOD, seq={seq}, skill={vod_skill}")
