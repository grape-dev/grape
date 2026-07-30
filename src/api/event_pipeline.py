"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid, threading
from datetime import datetime
from src.config import log, WORKSPACE_DIR
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import get_domain_agent
from src.api.event_store import append_event, update_event_status, get_issue_summary
from src.session_manager import db, db_lock


def resolve_issue_id(payload: dict) -> str:
    """从事件 payload 提取 Issue/PR 编号"""
    if not isinstance(payload, dict):
        return ""
    for key in ("issue", "pull_request", "merge_request"):
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            num = obj.get("number", obj.get("iid", ""))
            if num:
                return f"#{num}"
    obj = payload.get("object_attributes", {})
    if isinstance(obj, dict):
        num = obj.get("number", obj.get("iid", obj.get("id", "")))
        if num:
            return f"#{num}"
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


def run_pipeline(raw_body: bytes, headers: dict, account: str = "default",
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
    event_type = evt.get("type", "unknown")

    log.info(f"Pipeline: start, raw_type={event_header}, source={source}, repo={evt['repository']}")

    if not evt["repository"] or event_type == "unknown":
        log.info(f"Pipeline skipped: repository='{evt['repository']}' or event_type='{event_type}' not recognized")
        return result

    # 2. 自循环检测
    actor = resolve_actor(evt.get("payload", {}), evt)
    log.info(f"Pipeline: actor={actor}, event_type={event_type}")
    if actor and event_type in ("issue_comment", "note_hook_pr"):
        from src.user.db import get_all_bot_usernames
        bot_usernames = get_all_bot_usernames()
        if actor in bot_usernames:
            log.info(f"Pipeline skip: self-comment from bot={actor}")
            return result
        log.info(f"Pipeline: comment from {actor}, not a bot ({bot_usernames})")

    # 3. 路由判定
    agent_type = get_domain_agent(event_type)
    log.info(f"Pipeline: routing result -> agent_type='{agent_type}'")
    if not agent_type:
        log.info(f"Pipeline: no route for type={event_type}, skip")
        return result

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    backend = None
    skill_name = ""
    repo_name = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend, skill FROM agent_sessions WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row:
                backend = row[0] or "opencode"
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
    title = evt.get("title", "") or event_type
    log.info(f"Pipeline: issue_id={issue_id or '(none)'}, title={title}")

    # 6. 存储事件
    log.info(f"Pipeline: storing event, repo_key={repo_key}, agent={agent_type}")
    actor = evt.get("actor", "")
    seq = append_event(
        repo_key=repo_key,
        event_type=event_type,
        title=title,
        issue_id=issue_id,
        source=source,
        agent_type=agent_type,
        actor=actor,
        payload=payload,
        status="running",
    )

    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": repo_key, "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})
    log.info(f"Pipeline: done, seq={seq}, ok=True, final_agent={agent_type}")

    # 7. 后台异步执行 Agent
    if seq > 0 and backend and skill_name:
        threading.Thread(target=_execute_agent, args=(
            repo_key, seq, agent_type, backend, skill_name, evt,
        ), daemon=True, name=f"ag-{agent_type}-{seq}").start()
        log.info(f"Pipeline: started agent execution for seq={seq}")

    return result


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"


def _execute_agent(repo_key: str, seq: int, agent_type: str,
                   backend: str, skill_name: str, evt: dict):
    """后台执行 Agent：构建 prompt → stream_agent → 更新事件状态"""
    from src.agent.executor import stream_agent
    from src.prompt_builder import build_prompt
    from src.api.event_store import update_event_config, update_event_complete

    repository = evt.get("repository", repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key)
    ws = os.path.join(WORKSPACE_DIR, repository, f"{agent_type}-agent")
    os.makedirs(ws, exist_ok=True)

    # 读取上次的 oc_session_id（用于延续对话上下文）
    repo_name = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    oc_session_id = ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT oc_session_id FROM agent_sessions WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row and row[0]:
                oc_session_id = row[0]
    except Exception:
        pass

    log.info(f"Exec agent: type={agent_type}, backend={backend}, skill={skill_name}, seq={seq}, oc_session={oc_session_id[:20] if oc_session_id else 'none'}")

    try:
        prompt = build_prompt(evt, [], "", [skill_name] if skill_name else [agent_type])
    except Exception as e:
        log.warning(f"Exec agent: prompt build failed: {e}")
        update_event_status(seq, "failed")
        return

    parts = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    owner_repo = parts.split("/", 1)
    repo_gitcode_token = ""
    if len(owner_repo) == 2:
        from src.user.db import get_bot_token_by_repo
        repo_gitcode_token = get_bot_token_by_repo(owner_repo[0], owner_repo[1]) or ""

    # 写入执行配置
    started_at = datetime.now().isoformat()
    update_event_config(seq, backend, skill_name, started_at)
    update_event_status(seq, "running")
    log.info(f"Exec agent: spawning {backend} for seq={seq}")

    exit_code = -1
    final_text = ""
    new_oc_session_id = ""
    agent_logs = []
    try:
        for evt_type, data in stream_agent(
            prompt, ws, session_id=oc_session_id, event_type=evt.get("type", ""),
            backend_name=backend, account="default",
            repo_gitcode_token=repo_gitcode_token,
        ):
            agent_logs.append({"time": datetime.now().isoformat(), "type": evt_type, "msg": str(data)[:200]})
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                new_oc_session_id = d.get("oc_session_id", "")
                log.info(f"Agent done: exit={exit_code}, text_len={len(final_text)}, new_oc_session={new_oc_session_id[:20] if new_oc_session_id else 'none'}")
            elif evt_type == "error":
                log.warning(f"Agent error: {data}")
    except Exception as e:
        log.warning(f"Exec agent failed: {e}")

    # 更新 oc_session_id（持久化对话上下文）
    if new_oc_session_id and new_oc_session_id != oc_session_id:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE agent_sessions SET oc_session_id=?, last_active_at=datetime('now') WHERE repo_key=? AND agent_type=?",
                          (new_oc_session_id, repo_name, agent_type))
                db.commit()
        except Exception as e:
            log.warning(f"Exec agent: failed to update oc_session_id: {e}")

    # 写入执行结果
    completed_at = datetime.now().isoformat()
    duration = None
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(completed_at)
        duration = round((e - s).total_seconds(), 1)
    except Exception:
        pass
    new_status = "completed" if exit_code == 0 else "failed"
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, log_count={len(agent_logs)}")
