"""A2A 派发门禁（Issue #1770）。

背景：同一 Issue 在下游（qa）审查 running 期间仍被重复派发上游（coding），
上游会话只能幂等空跑，浪费执行槽位（#2955 实证）。

本模块在 A2A 派发入口提供三段拦截 + 一段观测：
1. 下游 running 判定：目标 agent 的上游被派发时，若同 Issue 同场景存在
   running/dispatched 的下游事件 → 跳过本次派发；
2. 时间窗去重：issue + scene + target agent 在时间窗内已有 completed 派发
   → 重复派发丢弃；failed/cancelled/timeout 允许立即重试；
3. 活跃重复判定：同 Issue + 同场景 + 同目标 agent 在短窗内已存在
   running/dispatched 事件且上下文不同 → 跳过（force 可越过）；
4. 观测：同 Issue 同目标 agent 短时间窗内多次派发/跳过输出告警日志。

跳过均通过 ``event_skipped`` 审计表留痕（reason 可读），并返回结构化响应。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

from src.kernel.config import log
from src.api.a2a_context import a2a_context_key

A2A_IDEMPOTENCY_WINDOW_SECONDS = int(os.getenv("A2A_IDEMPOTENCY_WINDOW_SECONDS", "120"))
A2A_DISPATCH_STORM_WINDOW_SECONDS = int(os.getenv("A2A_DISPATCH_STORM_WINDOW_SECONDS", "600"))
A2A_DISPATCH_STORM_THRESHOLD = int(os.getenv("A2A_DISPATCH_STORM_THRESHOLD", "2"))

# running/dispatched 视为活跃；queued 走 #1667 排队语义，不计入下游 running 判定。
_ACTIVE_STATUS = ("running", "dispatched")
# 仅 completed 计入时间窗去重；failed/cancelled/timeout 属失败/取消，应允许立即重试。
_WINDOW_DEDUP_STATUS = ("completed",)


def _parse_ts(value):
    """解析 events.created_at，统一为 UTC aware datetime（失败返回 None）。

    存储层时间口径（MySQL CST 墙钟 / SQLite UTC）由
    ``time_utils.normalize_event_time`` 按 ``_db_backend()`` 分流承担——禁止在
    本模块内写死 ``+8h``（Issue #512 / #1487 同构回归）。已带时区的输入交给
    ``normalize_event_time`` 后原样转 UTC，不二次偏移。
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc)
        raw = value.isoformat()
    else:
        raw = str(value or "").strip()
    if not raw:
        return None
    from src.api.time_utils import normalize_event_time
    try:
        dt = datetime.fromisoformat(str(normalize_event_time(raw)).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_now():
    return datetime.now(timezone.utc)


def _is_excluded(seq, exclude_seq: int) -> bool:
    """``seq`` 是否即被排除的源事件（精确等值，0/空表示不过滤）。"""
    if not exclude_seq:
        return False
    try:
        return int(seq or 0) == int(exclude_seq)
    except (TypeError, ValueError):
        return False


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


def _normalize_scene(scene_id: str) -> str:
    """归一化 scene_id：None/空白统一为 ''（'' 兼容历史无场景事件行）。"""
    return str(scene_id or "").strip()


def _query_active_events(repo_key: str, issue_id: str,
                         statuses=_ACTIVE_STATUS):
    """同 Issue 的活跃 a2a_event 行（events 归 runtime 域，语句在 owner public）。"""
    _statuses = tuple(statuses or _ACTIVE_STATUS)
    try:
        from src.domains.runtime import public as _runtime_public
        return _runtime_public.a2a_active_event_rows(repo_key, issue_id, _statuses)
    except Exception as _e:
        log.warning(f"A2A guard: active event query failed: {_e}")
        return []


def _collect_downstream(repo_key: str, issue_number, target_agent: str,
                        scene_id: str, order: list, target_idx: int,
                        exclude_seq: int, statuses) -> list:
    """收集同 Issue 同场景、流水线顺序在目标 agent 之后的事件行。"""
    _downstream = []
    for seq, agent, status, created_at, row_scene in _query_active_events(
            repo_key, f"#{issue_number}", statuses):
        if _is_excluded(seq, exclude_seq):
            continue
        agent = str(agent or "").strip()
        if agent == target_agent or agent not in order:
            continue
        _row_scene = _normalize_scene(row_scene)
        if _row_scene and scene_id and _row_scene != scene_id:
            continue
        if order.index(agent) > target_idx:
            _downstream.append({"seq": seq, "agent": agent, "status": status,
                                "created_at": created_at})
    return _downstream


def find_running_downstream(repo_key: str, issue_number, target_agent: str,
                            scene_id: str, nodes: list,
                            exclude_seq: int = 0) -> dict:
    """目标 agent 为上游且同 Issue 同场景存在 running 下游事件时返回该事件。

    返回 ``{"seq", "agent", "status", "created_at", "blocking_seqs"}``；无命中
    返回 ``{}``。``blocking_seqs`` 为排队补派须等待的全部前序：既含 running/
    dispatched 下游，也含同 Issue 同场景已在排队的下游（与 drain 的活跃语义对齐，
    避免同一 drain 轮次把排队下游与新入队事件一起补派，Issue #1973 复审残留 1）。
    ``nodes`` 为空（场景未配置/解析失败）时门禁静默失效——此处显式告警，
    便于排查「配置缺失导致误放行」而非无痕。

    Issue #1948 / #1973：``exclude_seq`` 为本次调用的发起方自身事件 seq。调用方
    （如下游 qa 打回上游 coding）自身 running 事件不应被判定为「下游 running」，
    否则反向派发会被自锁跳过。仅按该 seq 精确排除，其余下游 running 仍照常拦截
    （#1770 防抖语义不退化）。
    """
    order = pipeline_order(nodes)
    if not order:
        log.warning(
            "A2A guard: pipeline nodes empty — downstream-running gate disabled "
            "repo=%s issue=#%s target=%s (scene config missing?)",
            repo_key, issue_number, target_agent)
        return {}
    if target_agent not in order:
        return {}
    target_idx = order.index(target_agent)
    scene_id = _normalize_scene(scene_id)
    try:
        _exclude_seq = int(exclude_seq or 0)
    except Exception as _ee:
        log.warning(
            "A2A guard: invalid exclude_seq=%r, fallback to 0 "
            "(self event may self-block) repo=%s issue=#%s target=%s: %s",
            exclude_seq, repo_key, issue_number, target_agent, _ee)
        _exclude_seq = 0
    _blocking = _collect_downstream(
        repo_key, issue_number, target_agent, scene_id, order, target_idx,
        _exclude_seq, _ACTIVE_STATUS)
    if not _blocking:
        return {}
    # Issue #1973 复审残留 1：同 Issue 同场景已在排队的下游也必须纳入阻塞集合，
    # 否则它既不在 running 门禁里，也不会被 drain 与新入队事件区分开。
    _queued_blocking = _collect_downstream(
        repo_key, issue_number, target_agent, scene_id, order, target_idx,
        _exclude_seq, ("queued",))
    _blocking_seqs = sorted({int(_b["seq"] or 0)
                             for _b in _blocking + _queued_blocking} - {0})
    # 同一 Issue 可能存在多个活跃下游事件：排队必须等待全部终态，只等最新一个
    # 会在更早的下游仍 running 时提前补派（#1973 复审 a）。seq 仍返回最新一条，
    # 兼容既有响应字段；blocking_seqs 供 drain/reconcile 全量核对。
    return dict(_blocking[0], blocking_seqs=_blocking_seqs)


def find_window_duplicate(repo_key: str, issue_number, target_agent: str,
                          scene_id: str, window_seconds: int | None = None,
                          exclude_seq: int = 0) -> dict:
    """同 Issue + 同场景 + 同目标 agent 在时间窗内已有 completed 派发时返回该记录。

    仅 ``completed`` 计入窗口去重：``failed`` / ``cancelled`` / ``timeout`` 属失败或
    取消，应允许立即重试（Issue #1770 审查 P2）。running/dispatched/queued 由活跃
    门禁与 #1667 排队语义处理，不在此重复拦截。

    ``exclude_seq`` 指定一行的 seq 精确跳过（手动补发排除源事件自身，默认 0 不过滤；
    异源候选仍照常拦截）。

    返回 ``{"seq", "created_at", "status"}``；无命中返回 ``{}``。
    """
    window = A2A_IDEMPOTENCY_WINDOW_SECONDS if window_seconds is None else window_seconds
    if window <= 0:
        return {}
    cutoff = _utc_now() - timedelta(seconds=window)
    scene_id = _normalize_scene(scene_id)
    try:
        from src.domains.runtime import public as _runtime_public
        rows = _runtime_public.a2a_window_dedup_rows(
            repo_key, f"#{issue_number}", target_agent)
    except Exception as _e:
        log.warning(f"A2A guard: window duplicate query failed: {_e}")
        return {}
    for seq, created_at, status, row_scene in rows:
        if _is_excluded(seq, exclude_seq):
            continue
        if str(status or "").strip() not in _WINDOW_DEDUP_STATUS:
            continue
        _row_scene = _normalize_scene(row_scene)
        if _row_scene and scene_id and _row_scene != scene_id:
            continue
        dt = _parse_ts(created_at)
        if dt is None or dt < cutoff:
            continue
        return {"seq": seq, "created_at": created_at, "status": status}
    return {}


def find_active_duplicate(repo_key: str, issue_number, target_agent: str,
                          scene_id: str, context_key: str = "",
                          window_seconds: int | None = None,
                          include_same_context: bool = False,
                          exclude_seq: int = 0) -> dict:
    """同 Issue + 同场景 + 同目标 agent 在短窗内存在活跃事件时返回该事件。

    默认仅命中「请求上下文键非空且与活跃事件上下文不同」的场景（Issue #1770
    审查 P3：同 issue+scene+agent 活跃时不再另建第二个事件空跑）；同上下文重复
    仍交由 #1667 ``duplicate_a2a`` 处理以保留其 ``original_event`` 响应。
    ``include_same_context=True`` 供原子落库复查使用（并发同键只允许落一行）。
    ``exclude_seq`` 指定一行的 seq 精确跳过（手动补发排除源事件自身，默认 0 不过滤；
    异源候选仍照常拦截）。

    返回 ``{"seq", "agent", "status", "created_at"}``；无命中返回 ``{}``。
    """
    if not include_same_context and not context_key:
        return {}
    window = A2A_IDEMPOTENCY_WINDOW_SECONDS if window_seconds is None else window_seconds
    if window <= 0:
        return {}
    cutoff = _utc_now() - timedelta(seconds=window)
    scene_id = _normalize_scene(scene_id)
    try:
        from src.domains.runtime import public as _runtime_public
        rows = _runtime_public.a2a_active_dup_rows(
            repo_key, f"#{issue_number}", target_agent, _ACTIVE_STATUS)
    except Exception as _e:
        log.warning(f"A2A guard: active duplicate query failed: {_e}")
        return {}
    for seq, created_at, status, row_scene, payload_raw in rows:
        if _is_excluded(seq, exclude_seq):
            continue
        dt = _parse_ts(created_at)
        if dt is None or dt < cutoff:
            continue
        _row_scene = _normalize_scene(row_scene)
        if _row_scene and scene_id and _row_scene != scene_id:
            continue
        _row_idem, _row_ctx = "", ""
        try:
            _p = json.loads(payload_raw or "{}")
            if isinstance(_p, dict):
                _row_idem = str(_p.get("idempotency_key") or "")
                _row_ctx = str(_p.get("context") or "")
        except Exception:
            pass
        _row_key = a2a_context_key(_row_idem, _row_ctx)
        if not include_same_context and (not _row_key or _row_key == context_key):
            continue
        return {"seq": seq, "agent": target_agent, "status": status,
                "created_at": created_at}
    return {}


def window_key(issue_number, scene_id: str, target_agent: str,
               window_seconds: int | None = None) -> str:
    """时间窗键 = issue + scene + target agent + 时间窗分桶（可读前缀便于排查）。

    仅用于观测/排查；实际去重以 ``find_window_duplicate`` 的 created_at 时间窗为准，
    因此响应字段命名为 ``window_key`` 而非 ``idempotency_key``（Issue #1770 审查 P1）。
    """
    window = A2A_IDEMPOTENCY_WINDOW_SECONDS if window_seconds is None else window_seconds
    bucket = int(_utc_now().timestamp() // max(window, 1))
    raw = f"{str(issue_number).lstrip('#')}|{scene_id or ''}|{target_agent}|{window}|{bucket}"
    return "a2a:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def detect_dispatch_storm(repo_key: str, issue_number, target_agent: str,
                          upcoming: int = 1, window_seconds: int | None = None) -> dict:
    """短时间窗内同 Issue + 同目标 agent 多次派发/跳过 → 告警日志（可观测）。

    ``count`` 含本次即将发生的派发（upcoming）与窗口内 ``event_skipped`` 跳过
    次数——跳过路径不落 events，若不纳入跳过计数则反复跳过永不告警
    （Issue #1770 审查 P5）。返回 ``{"count", "alerted", "window"}``。
    """
    window = A2A_DISPATCH_STORM_WINDOW_SECONDS if window_seconds is None else window_seconds
    cutoff = _utc_now() - timedelta(seconds=max(window, 1))
    count = 0
    try:
        from src.domains.runtime import public as _runtime_public
        # 两条独立统计（events 派发数 / event_skipped 跳过数）：失败一起归零，
        # 与原「同一 try 包两条语句」的降级口径一致，不出现半份计数。
        rows = _runtime_public.a2a_storm_event_times(
            repo_key, f"#{issue_number}", target_agent)
        # 跳过路径不写 events：把 event_skipped 同窗口跳过次数纳入统计。
        skip_rows = _runtime_public.a2a_storm_skip_rows(
            repo_key, f"#{issue_number}")
    except Exception as _e:
        log.warning(f"A2A guard: storm detection query failed: {_e}")
        rows, skip_rows = [], []
    for (created_at,) in rows:
        dt = _parse_ts(created_at)
        if dt is not None and dt >= cutoff:
            count += 1
    _marker = f"target={target_agent} "
    for created_at, detail in skip_rows:
        _detail = str(detail or "")
        if _detail != f"target={target_agent}" and not _detail.startswith(_marker):
            continue
        dt = _parse_ts(created_at)
        if dt is not None and dt >= cutoff:
            count += 1
    count += max(int(upcoming or 0), 0)
    alerted = count >= A2A_DISPATCH_STORM_THRESHOLD
    if alerted:
        log.warning(
            "A2A_DISPATCH_ALERT: repeated a2a_event dispatch within %ss "
            "repo=%s issue=#%s agent=%s count=%s threshold=%s (编排抖动排查)",
            window, repo_key, issue_number, target_agent, count,
            A2A_DISPATCH_STORM_THRESHOLD)
    return {"count": count, "alerted": alerted, "window": window}


def append_guarded_a2a_event(repo_key: str, issue_number, target_agent: str,
                             scene_id: str, *, title: str, summary: str,
                             payload: dict, actor: str = "", status: str = "running",
                             context_key: str = "", idempotency_key: str = "",
                             window_seconds: int | None = None,
                             exclude_seq: int = 0):
    """原子「门禁复查 + 事件落库」（Issue #1770 审查 P4）。

    在同一 ``db_lock``（RLock）内先复查时间窗/活跃重复，再调用 ``append_event``
    落库，消除「门禁查询」与「事件插入」分属不同锁段导致的并发同键双落库。
    ``exclude_seq`` 透传到三次复查，确保手动补发（a2a-redo）在原子阶段仍能
    排除源事件自身（Issue #1962）。
    返回 ``(kind, value)``：

    - ``("ok", seq)``：已落库；
    - ``("duplicate_key", {...})``：时间窗内已有 completed 或同上下文活跃事件；
    - ``("active_dispatch", {...})``：短窗内已有不同上下文的活跃事件。
    """
    from src.kernel.session_manager import db_lock
    from src.api.event_store import append_event
    with db_lock:
        _dup = find_window_duplicate(
            repo_key, issue_number, target_agent, scene_id, window_seconds,
            exclude_seq=exclude_seq)
        if _dup:
            return "duplicate_key", _dup
        _active = find_active_duplicate(
            repo_key, issue_number, target_agent, scene_id, context_key,
            window_seconds, exclude_seq=exclude_seq)
        if _active:
            return "active_dispatch", _active
        # 并发复查：同上下文活跃事件已存在 → 按重复处理，禁止第二次落库。
        _same = find_active_duplicate(
            repo_key, issue_number, target_agent, scene_id, context_key,
            window_seconds, include_same_context=True, exclude_seq=exclude_seq)
        if _same:
            return "duplicate_key", _same
        seq = append_event(
            repo_key=repo_key,
            event_type="a2a_event",
            title=title,
            issue_id=f"#{issue_number}" if issue_number else "#0",
            source="a2a",
            agent_type=target_agent,
            actor=actor,
            summary=summary,
            payload=payload,
            status=status,
            scene_id=scene_id,
            idempotency_key=idempotency_key,
        )
        return "ok", seq


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