"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid, threading
from datetime import datetime
from src.config import log, WORKSPACE_DIR
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import get_domain_agent
from src.api.event_store import get_issue_summary
from src.session_manager import db, db_lock


def resolve_issue_id(payload: dict) -> str:
    """从事件 payload 提取 Issue/PR 编号"""
    if not isinstance(payload, dict):
        return ""
    # A2A payload 用 issue_number 字段
    num = payload.get("issue_number", "")
    if num:
        return f"#{num}"
    for key in ("issue", "pull_request", "merge_request"):
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            num = obj.get("number", obj.get("iid", ""))
            if num:
                return f"#{num}"
    obj = payload.get("object_attributes", {})
    if isinstance(obj, dict):
        num = obj.get("number", obj.get("iid", obj.get("noteable_iid", obj.get("id", ""))))
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


def run_pipeline(raw_body: bytes, headers: dict, account: str = "",
                 repo_key_override: str = "") -> dict:
    """事件处理管道入口：归一化 → 路由 → 存储 → 返回结果"""
    result = {"seq": 0, "event_type": "", "skill": "", "repo_key": "", "ok": False}

    # 1. 归一化
    event_header, source = get_event_header(headers)
    evt = normalize_event(raw_body, event_header, source)
    evt["account"] = account

    repo_key = repo_key_override or f"{source}:{evt['repository']}"
    # 统一 repo_key 格式：去掉 "source:" 前缀，所有查询都用 "owner/repo"
    if ":" in repo_key:
        repo_key = repo_key.split(":", 1)[1]
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

    # 3. 路由判定
    agent_type = get_domain_agent(event_type)
    log.info(f"Pipeline: routing result -> agent_type='{agent_type}'")
    if not agent_type:
        log.info(f"Pipeline: no route for type={event_type}, skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "no_route")
        return result

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    backend = None
    skill_name = ""
    repo_name = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend_id, skill FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row:
                backend = "opencode"
                if row[0]:
                    c.execute("SELECT name FROM backend_agents WHERE id=?", (row[0],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend = brow[0]
                skill_name = row[1] or ""
                log.info(f"Pipeline: agent session found, backend={backend}, skill={skill_name or '(default)'}")
    except Exception as e:
        log.warning(f"Pipeline: agent session lookup failed: {e}")

    if not backend:
        # 从默认映射配置文件加载 skill
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
    issue_id = resolve_issue_id(payload) or evt.get("issue_id", "")
    # PR 类事件：尝试解析关联的 Issue（title/branch 提取，必要时同步调 PR 详情 API）
    if event_type in ("note_hook_pr", "pull_request_opened", "pull_request_updated", "pull_request_merged"):
        try:
            from src.user.db import get_agent_bot_token
            _pr_api_token = get_agent_bot_token(repo_key) or ""
            _pr_issue = resolve_pr_issue_id(payload, _pr_api_token)
            if _pr_issue:
                issue_id = _pr_issue
                log.info(f"Pipeline: PR 关联 Issue -> {issue_id}")
        except Exception as e:
            log.warning(f"Pipeline: resolve PR issue failed: {e}")
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

    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": repo_key, "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})

    # 后台异步执行 Agent（执行完更新该 seq 的 result/status）
    if backend and skill_name and seq > 0:
        threading.Thread(target=_execute_agent, args=(
            repo_key, seq, agent_type, backend, skill_name, evt,
        ), daemon=True, name=f"ag-{agent_type}-{seq}").start()
        log.info(f"Pipeline: started agent execution, seq={seq}, repo={repo_key}, agent={agent_type}")

    return result


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"


def _execute_agent(repo_key: str, seq: int, agent_type: str,
                   backend: str, skill_name: str, evt: dict):
    """后台执行 Agent：构建 prompt → stream_agent → 更新 event（result/status/agent_log）"""
    from src.agent.executor import stream_agent
    from src.prompt_builder import build_prompt
    from src.api.event_store import (
        update_event_config, update_event_status, update_event_complete, evaluate_agent_output,
    )

    repository = evt.get("repository", repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key)
    ws = os.path.join(WORKSPACE_DIR, repository, f"{agent_type}-agent")
    os.makedirs(ws, exist_ok=True)

    # 读取上次的 oc_session_id（用于延续对话上下文）
    repo_name = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    oc_session_id = ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend_session_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row and row[0]:
                oc_session_id = row[0]
    except Exception:
        pass

    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] type={agent_type}, backend={backend}, skill={skill_name}, oc_session={oc_session_id[:20] if oc_session_id else 'none'}")

    # 获取 bot token（统一走 get_gitcode_token：devops_agent_session → managed_repos → 全局）
    repo_gitcode_token = ""
    try:
        from src.user.db import get_gitcode_token
        repo_gitcode_token = get_gitcode_token(repo_key=repo_name) or ""
    except Exception as e:
        log.warning(f"Exec agent: get bot token failed: {e}")
    log.info(f"Exec agent: bot_token={'set' if repo_gitcode_token else 'none'}")

    try:
        prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type])
    except Exception as e:
        log.warning(f"Exec agent: prompt build failed: {e}")
        append_skipped_event(repo_key, evt.get("type", ""), "prompt_build_failed", str(e))
        return

    parts = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    owner_repo = parts.split("/", 1)
    exec_account = evt.get("account") or repo_name
    if not repo_gitcode_token:
        log.warning(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] no bot token available, agent will fail on API calls")

    # 记录执行配置（执行完成有产出后统一写入 event）
    started_at = datetime.utcnow().isoformat()
    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] spawning {backend}")

    exit_code = -1
    final_text = ""
    new_oc_session_id = ""
    tool_uses = []
    agent_logs = []
    try:
        for evt_type, data in stream_agent(
            prompt, ws, session_id=oc_session_id, event_type=evt.get("type", ""),
            backend_name=backend, account=exec_account, bot_token=repo_gitcode_token,
            agent_label=f"[Agent:{agent_type}|repo:{repo_name}]",
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

    # 更新 oc_session_id（持久化对话上下文）
    if new_oc_session_id and new_oc_session_id != oc_session_id:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE devops_agent_session SET backend_session_id=?, last_active_at=datetime('now') WHERE repo_key=? AND agent_type=?",
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
    update_event_config(seq, backend, skill_name, started_at)
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at, result=result_text)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}")
