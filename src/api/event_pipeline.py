"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid
from datetime import datetime
from src.config import log
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

    # 4. 查找 Agent 配置（backend + skill）
    backend = "opencode"
    skill_name = ""
    repo_name = repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend, skill FROM agent_sessions WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row:
                backend = row[0] or backend
                skill_name = row[1] or ""
                log.info(f"Pipeline: agent session found, backend={backend}, skill={skill_name or '(default)'}")
            else:
                log.info(f"Pipeline: no agent session for repo={repo_name}, agent={agent_type}, using defaults")
    except Exception as e:
        log.warning(f"Pipeline: agent session lookup failed: {e}")

    # 5. 提取 Issue ID
    payload = evt.get("payload", {})
    issue_id = resolve_issue_id(payload) or evt.get("issue_id", "")
    title = evt.get("title", "") or event_type
    log.info(f"Pipeline: issue_id={issue_id or '(none)'}, title={title}")

    # 6. 存储事件
    log.info(f"Pipeline: storing event, repo_key={repo_key}, agent={agent_type}")
    seq = append_event(
        repo_key=repo_key,
        event_type=event_type,
        title=title,
        issue_id=issue_id,
        source=source,
        agent_type=agent_type,
        payload=payload,
        status="running",
    )

    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": repo_key, "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})
    log.info(f"Pipeline: done, seq={seq}, ok=True, final_agent={agent_type}")
    return result


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"
