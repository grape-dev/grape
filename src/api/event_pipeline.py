"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, time, uuid
from datetime import datetime
from src.config import log, BOT_USERNAMES
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import get_domain_skill
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

    log.info(f"Pipeline: type={event_type}, repo={evt['repository']}, source={source}")

    if not evt["repository"] or event_type == "unknown":
        log.info(f"Pipeline skipped: malformed event")
        return result

    # 2. 自循环检测
    actor = resolve_actor(evt.get("payload", {}), evt)
    if actor in BOT_USERNAMES and event_type in ("issue_comment", "note_hook_pr"):
        log.info(f"Pipeline skip: self-comment from {actor}")
        return result

    # 3. 路由判定
    skill = get_domain_skill(event_type, f"{source}:{evt['repository']}")
    if not skill:
        log.info(f"Pipeline: no route for type={event_type}")
        return result

    # 4. 提取 Issue ID
    payload = evt.get("payload", {})
    issue_id = resolve_issue_id(payload) or evt.get("issue_id", "")
    title = evt.get("title", "") or event_type

    # 5. 存储事件
    seq = append_event(
        repo_key=repo_key,
        event_type=event_type,
        title=title,
        issue_id=issue_id,
        source=source,
        agent_type=skill,
        payload=payload,
        status="running",
    )

    result.update({"seq": seq, "event_type": event_type, "skill": skill,
                   "repo_key": repo_key, "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})
    return result


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"
