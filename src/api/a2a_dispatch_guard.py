"""A2A 派发门禁（Issue #1770）。

背景：同一 Issue 在下游（qa）审查 running 期间仍被重复派发上游（coding），
上游会话只能幂等空跑，浪费执行槽位（#2955 实证）。

本模块在 A2A 派发入口提供三段门禁：
1. 下游 running 判定：目标 agent 的上游被派发时，若同 Issue 同场景存在
   running/dispatched 的下游事件 → 跳过本次派发；
2. 幂等键：issue + scene + target agent + 时间窗，窗口内重复派发丢弃；
3. 观测：同 Issue 同目标 agent 短时间窗内多次派发输出告警日志。

跳过均通过 ``event_skipped`` 审计表留痕（reason 可读），并返回结构化响应。
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from src.config import log

A2A_IDEMPOTENCY_WINDOW_SECONDS = int(os.getenv("A2A_IDEMPOTENCY_WINDOW_SECONDS", "120"))
A2A_DISPATCH_STORM_WINDOW_SECONDS = int(os.getenv("A2A_DISPATCH_STORM_WINDOW_SECONDS", "600"))
A2A_DISPATCH_STORM_THRESHOLD = int(os.getenv("A2A_DISPATCH_STORM_THRESHOLD", "2"))

_ACTIVE_STATUS = ("running", "dispatched")


def _parse_ts(value: str):
    """解析 events.created_at（naive UTC 空格格式）与 ISO aware 两种形态。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except Exception:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utc_now():
    return datetime.now(timezone.utc)


def resolve_scene(repo_key: str, issue_number) -> tuple:
    """返回 Issue 当前场景 (scene_id, nodes)。解析失败返回 ("default", [])。"""
    scene_id = "default"
    try:
        from src.api.session_store import get_issue_scene
        _sid, _ = get_issue_scene(repo_key, f"#{issue_number}")
        scene_id = _sid or "default"
    except Exception as _e:
        log.warning(f"A2A guard: resolve issue scene failed: {_e}")
    nodes = []
    try:
        from src.api.pipeline_config import get_pipeline_config
        _cfg = get_pipeline_config(scene_id, repo_key=repo_key) or {}
        nodes = _cfg.get("nodes") or []
    except Exception as _e:
        log.warning(f"A2A guard: resolve pipeline nodes failed: {_e}")
    return scene_id, nodes


def pipeline_order(nodes: list) -> list:
    """按 nodes 顺序（兼容 next 链）返回节点 id 顺序。"""
    ids = [str(n.get("id") or "").strip() for n in nodes if str(n.get("id") or "").strip()]
    if not ids:
        return []
    nxt = {str(n.get("id") or "").strip(): str(n.get("next") or "").strip()
           for n in nodes if str(n.get("id") or "").strip()}
    # 优先从无前驱节点沿 next 链走，得到真实顺序；链不完整时退回声明顺序
    targets = set(nxt.values())
    heads = [i for i in ids if i not in targets]
    if heads:
        order, seen = [], set()
        cur = heads[0]
        while cur and cur in nxt and cur not in seen:
            seen.add(cur)
            order.append(cur)
            cur = nxt.get(cur, "")
        if len(order) == len(ids):
            return order
    return ids


def _scene_filter(scene_id: str):
    """同场景口径：显式场景行 + 历史无场景行（普通 A2A 事件 scene_id=''）。"""
    return scene_id or ""


def _query_active_events(repo_key: str, issue_id: str):
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, agent_type, status, created_at, scene_id FROM events "
                "WHERE repo_key=? AND issue_id=? AND event_type='a2a_event' "
                "AND status IN ('running','dispatched') ORDER BY seq DESC LIMIT 50",
                (repo_key, issue_id))
            return c.fetchall() or []
    except Exception as _e:
        log.warning(f"A2A guard: active event query failed: {_e}")
        return []


def find_running_downstream(repo_key: str, issue_number, target_agent: str,
                            scene_id: str, nodes: list) -> dict:
    """目标 agent 为上游且同 Issue 同场景存在 running 下游事件时返回该事件。

    返回 {"seq", "agent"}；无命中返回 {}。
    """
    order = pipeline_order(nodes)
    if target_agent not in order:
        return {}
    target_idx = order.index(target_agent)
    scene_id = _scene_filter(scene_id)
    for seq, agent, status, created_at, row_scene in _query_active_events(
            repo_key, f"#{issue_number}"):
        agent = str(agent or "").strip()
        if agent == target_agent or agent not in order:
            continue
        _row_scene = str(row_scene or "").strip()
        if _row_scene and scene_id and _row_scene != scene_id:
            continue
        if order.index(agent) > target_idx:
            return {"seq": seq, "agent": agent, "status": status,
                    "created_at": created_at}
    return {}


def find_window_duplicate(repo_key: str, issue_number, target_agent: str,
                          scene_id: str, window_seconds: int | None = None) -> dict:
    """同 Issue + 同场景 + 同目标 agent 在时间窗内已有派发记录时返回该记录。

    仅统计已终态的历史派发：running/dispatched/queued 由既有 A2A 活跃去重与
    排队语义处理（Issue #1667 允许活跃事件存在时按不同 context 另派），此处不重复拦截。

    返回 {"seq", "created_at", "status"}；无命中返回 {}。
    """
    window = A2A_IDEMPOTENCY_WINDOW_SECONDS if window_seconds is None else window_seconds
    if window <= 0:
        return {}
    cutoff = _utc_now() - timedelta(seconds=window)
    scene_id = _scene_filter(scene_id)
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, created_at, status, scene_id FROM events "
                "WHERE repo_key=? AND issue_id=? AND event_type='a2a_event' "
                "AND agent_type=? ORDER BY seq DESC LIMIT 20",
                (repo_key, f"#{issue_number}", target_agent))
            rows = c.fetchall() or []
    except Exception as _e:
        log.warning(f"A2A guard: window duplicate query failed: {_e}")
        return {}
    for seq, created_at, status, row_scene in rows:
        if str(status or "").strip() in ("running", "dispatched", "queued"):
            continue
        _row_scene = str(row_scene or "").strip()
        if _row_scene and scene_id and _row_scene != scene_id:
            continue
        dt = _parse_ts(created_at)
        if dt is None or dt < cutoff:
            continue
        return {"seq": seq, "created_at": created_at, "status": status}
    return {}


def idempotency_key(issue_number, scene_id: str, target_agent: str,
                    window_seconds: int | None = None) -> str:
    """幂等键 = issue + scene + target agent + 时间窗分桶（可读前缀便于排查）。"""
    window = A2A_IDEMPOTENCY_WINDOW_SECONDS if window_seconds is None else window_seconds
    bucket = int(_utc_now().timestamp() // max(window, 1))
    raw = f"{str(issue_number).lstrip('#')}|{scene_id or ''}|{target_agent}|{window}|{bucket}"
    return "a2a:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def detect_dispatch_storm(repo_key: str, issue_number, target_agent: str,
                          upcoming: int = 1, window_seconds: int | None = None) -> dict:
    """短时间窗内同 Issue + 同目标 agent 多次派发 → 告警日志（可观测）。

    返回 {"count", "alerted", "window"}；count 含本次即将发生的派发（upcoming）。
    """
    window = A2A_DISPATCH_STORM_WINDOW_SECONDS if window_seconds is None else window_seconds
    cutoff = _utc_now() - timedelta(seconds=max(window, 1))
    count = 0
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_at FROM events WHERE repo_key=? AND issue_id=? "
                "AND event_type='a2a_event' AND agent_type=? ORDER BY seq DESC LIMIT 50",
                (repo_key, f"#{issue_number}", target_agent))
            rows = c.fetchall() or []
        for (created_at,) in rows:
            dt = _parse_ts(created_at)
            if dt is not None and dt >= cutoff:
                count += 1
    except Exception as _e:
        log.warning(f"A2A guard: storm detection query failed: {_e}")
    count += max(int(upcoming or 0), 0)
    alerted = count >= A2A_DISPATCH_STORM_THRESHOLD
    if alerted:
        log.warning(
            "A2A_DISPATCH_ALERT: repeated a2a_event dispatch within %ss "
            "repo=%s issue=#%s agent=%s count=%s threshold=%s (编排抖动排查)",
            window, repo_key, issue_number, target_agent, count,
            A2A_DISPATCH_STORM_THRESHOLD)
    return {"count": count, "alerted": alerted, "window": window}


def record_skip(repo_key: str, issue_number, target_agent: str, reason: str,
                detail: str = ""):
    """跳过审计留痕（event_skipped，reason 可读，供流水线页/排查查询）。"""
    try:
        from src.api.event_store import append_skipped_event
        append_skipped_event(
            repo_key, "a2a_event", reason, detail,
            issue_id=f"#{issue_number}" if issue_number else "")
    except Exception as _e:
        log.warning(f"A2A guard: record skip failed: {_e}")