"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid, threading
from datetime import datetime, timezone
from src.config import log, WORKSPACE_DIR
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import NON_ISSUE_EVENT_ROUTE, PR_DEFAULT_AGENT, ISSUE_EVENT_TYPES, PR_EVENT_TYPES
from src.api.event_store import get_issue_summary
from src.session_manager import db, db_lock


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
    if not agent_type:
        # 仓库级事件订阅兜底（agent-event-subscription-design.md §六）：主路由为空时，
        # 命中 devops_agent_session.subscribe_events（如 SRE 订阅 PR 合入 + 分支）才触发。
        _pr_branch = resolve_pr_target_branch(evt)
        if _pr_branch and not evt.get("branch"):
            evt["branch"] = _pr_branch  # 回填，供摘要/event 展示（PR 合入目标分支）
        _sub_agent = _match_repo_subscription(repo_key, event_type, _pr_branch)
        if _sub_agent:
            agent_type = _sub_agent
            log.info(f"Pipeline: repo subscription matched -> agent_type='{_sub_agent}'")
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
                backend = "opencode"
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
                _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                _default_skill = _mapping.get(agent_type, "")
                if _default_skill:
                    backend = "opencode"
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

    # 执行分派：auto（按绑定引擎宿主自动推导，见 execution-mode-auto-routing-design.md）
    # / direct（就地）/ queue_local·remote（入队 + RuntimeAgent，M2 见 implementation-plan §三）
    from src.runtime.service import runtime_execution_kind, enqueue_task, EXEC_MODE_DIRECT
    kind = runtime_execution_kind(runtime_id or 0)
    if kind != EXEC_MODE_DIRECT and backend and skill_name and seq > 0:
        _enqueue_pipeline_task(repo_key, seq, agent_type, backend, skill_name, evt,
                               skill_id, skill_path, runtime_id or 0, issue_id)
    elif backend and skill_name and seq > 0:
        evt["_skill_path"] = skill_path  # 供 prompt 构建使用（skill_id 解析出的真实路径）
        threading.Thread(target=_execute_agent, args=(
            repo_key, seq, agent_type, backend, skill_name, evt, runtime_id or 0,
        ), daemon=True, name=f"ag-{agent_type}-{seq}").start()
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
    # 1) 确定路由：绑定 runtime_id；引擎池模式（agent-cluster-design.md §4.1）→ runtime_id=0 + pool_agent_type
    #    该 Agent 配置了引擎池时，任务由池内任意空闲引擎领取（claim 池调度）；否则按默认 runtime 兜底
    pool_mode = False
    if not runtime_id:
        try:
            from src.runtime.service import pool_runtime_ids
            if pool_runtime_ids(agent_type):
                pool_mode = True
            else:
                from src.user.db import ensure_default_runtime
                runtime_id = ensure_default_runtime("opencode") or 0
        except Exception:
            runtime_id = 0
    if not runtime_id and not pool_mode:
        log.warning(f"Pipeline enqueue: no runtime_id for repo={repo_key} agent={agent_type}, task skipped")
        _fail_event(seq, "入队失败：无可用 runtime（未配置引擎且不在引擎池）")
        return
    # 引擎实例名（`{type}-{device}`，与 UI 同源）：runtime_id 反查，查不到回退类型名
    inst_name = runtime_instance_name(runtime_id) or backend or "opencode"
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
    session = get_or_create_agent_session(repo_key, agent_type, backend or "opencode")
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
                              skill_path=skill_path, runtime_id=runtime_id)
    except Exception as e:
        log.warning(f"Pipeline enqueue: prompt build failed: {e}")
        _fail_event(seq, f"入队失败：prompt 构建异常 {str(e)[:150]}")
        return

    env_extra = {"REPO_FULL_NAME": repository, "ACCOUNT": evt.get("account") or repository}
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
            pool_agent_type=(agent_type if pool_mode else ""),
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
        )
    except Exception as e:
        log.warning(f"Pipeline enqueue: enqueue_task failed for seq={seq}: {e}")
        _fail_event(seq, f"入队失败：enqueue_task 异常 {str(e)[:150]}")
        return
    log.info(f"Pipeline: enqueued task={tid} seq={seq} repo={repo_key} agent={agent_type} pool={agent_type if pool_mode else '-'} issue_session={issue_session_id or '-'} mode={get_execution_mode()}")


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"


def _execute_agent(repo_key: str, seq: int, agent_type: str,
                   backend: str, skill_name: str, evt: dict, runtime_id: int = 0):
    """后台执行 Agent：构建 prompt → stream_agent → 更新 event（result/status/agent_log）"""
    from src.runtime.executor import stream_agent
    from src.prompt_builder import build_prompt
    from src.api.event_store import (
        update_event_config, update_event_status, update_event_complete, evaluate_agent_output,
        append_skipped_event,
    )
    from src.runtime.instances import runtime_instance_name

    # 引擎实例名（`{type}-{device}`，与 UI 同源）；查不到回退类型名
    inst_name = runtime_instance_name(runtime_id) or backend or "opencode"

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
                              skill_path=evt.get("_skill_path", ""), runtime_id=runtime_id)
        # 直接模式（direct）：project-info 服务端本地可达，替换占位符为权威路径
        try:
            from src.repo_key import split_owner_repo
            _o, _r = split_owner_repo(repo_name)
            _pi_path = os.path.join(WORKSPACE_DIR, _o, _r, "project-info")
            prompt = prompt.replace("{{PROJECT_INFO_DIR}}", _pi_path)
        except Exception:
            pass
        # 直接模式（direct）：技能路径占位符按真实 skill 目录替换
        # （官方技能在 SKILLS_DIR 下，executor 会兜底替换；repo/个人技能用真实路径）
        try:
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
        update_event_status(seq, "failed")
        update_event_complete(seq, "failed", exit_code=exit_code, duration=duration,
                              agent_logs=agent_logs, completed_at=completed_at, result=result_text)
        return

    # 有产出 → 更新该 event（写入配置 + 结果 + 结论 + 执行日志）
    new_status = "completed" if exit_code == 0 else "failed"
    update_event_config(seq, inst_name, skill_name, started_at)
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at, result=result_text)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}")
