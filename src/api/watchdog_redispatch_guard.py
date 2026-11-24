"""watchdog 重派守卫（Issue #4334）。

背景：#3888 在 grape-skills 已完成实现并经 qa 双轮通过（pipeline seq
84098 / 84137），watchdog 仍以空 context 重复派发 coding 三次。根因是重派
决策链路只判「任务是否有交棒证据」，没有回读目标 Issue 的流水线终态与外部
阻塞标记，也没有对同一 Issue 的自动重派做频次上限。

本模块把三类保护收敛到一处，供 A2A 终态门禁（``a2a_terminal_guard``）与
巡检补偿器（``patrol_compensate``）复用：

1. 终态预检：目标 Issue 的最新 pipeline 终态（completed / qa-passed）或
   external-block 标记命中 → 跳过本次 coding 重派，并记录判据来源；
2. 频次上限：同一 Issue 的 watchdog 自动重派在时间窗内超过上限 → 拦截留痕；
3. context 保护：重派 payload 必须携带原 context，空 context 先回填原事件
   结论，仍为空则拦截，禁止空 context 覆盖既有产物。

所有判定数据源均 best-effort：查询异常/无记录一律按「未命中」放行，保持
既有派发行为（保守放行优于误杀正常单据）。跳过/拦截统一写入
``event_skipped`` 结构化审计（reason + detail 含判据来源），并有对应日志。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from src.kernel.config import log
from src.kernel.issue_ident import is_native_issue_ident, native_issue_number
from src.api.pipeline_constants import (
    PAUSED_ISSUE_STATUSES,
    TERMINAL_ISSUE_STAGES,
    TERMINAL_TASK_STATUSES,
)

PIPELINE_TERMINAL_REASON = "watchdog_redispatch_terminal"
REDISPATCH_LIMIT_REASON = "watchdog_redispatch_limit"
CONTEXT_EMPTY_REASON = "redispatch_context_empty"

# external-block 标记的可读写法（标签或 payload 标记值均按归一化匹配）。
EXTERNAL_BLOCK_MARKERS = ("external-block", "external_block", "externalblock")

# 判定「这是 watchdog/自动重派」的 payload/context 标记。
REDISPATCH_MARKERS = ("redispatch", "redispatch_of", "redispatch_mode", "_reissue",
                      "watchdog_redispatch")
REDISPATCH_CONTEXT_MARKERS = ("compensated_by=patrol", "watchdog_redispatch")

DEFAULT_REDISPATCH_LIMIT = int(os.environ.get("WATCHDOG_REDISPATCH_LIMIT", "3"))
DEFAULT_REDISPATCH_WINDOW_SECONDS = int(
    os.environ.get("WATCHDOG_REDISPATCH_WINDOW_SECONDS", "1800"))

_TERMINAL_STATUSES = set(TERMINAL_TASK_STATUSES) | set(PAUSED_ISSUE_STATUSES)
_TERMINAL_STAGES = set(TERMINAL_ISSUE_STAGES) | set(PAUSED_ISSUE_STATUSES)


def _norm_marker(raw) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def _marker_hit(value) -> bool:
    norm = _norm_marker(value)
    return bool(norm) and norm in {_norm_marker(m) for m in EXTERNAL_BLOCK_MARKERS}


def _labels_external_block(labels) -> str:
    """标签里是否带 external-block；返回命中的原始标签（未命中返回空串）。"""
    if not labels:
        return ""
    items = labels
    if isinstance(labels, str):
        items = labels.split(",")
    elif isinstance(labels, dict):
        items = [labels]
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or ""
        if _marker_hit(item):
            return str(item).strip()
    return ""


def detect_external_block(payload=None, carried_context=None, labels=()) -> dict:
    """从标签 / payload / carried_context 识别外部阻塞标记。

    返回 ``{"hit": bool, "source": str, "marker": str}``。``source`` 指向命中
    判据来源（``issue.label`` / ``payload.*`` / ``carried_context.*``），供审计。
    """
    label = _labels_external_block(labels)
    if label:
        return {"hit": True, "source": "issue.label", "marker": label}
    for container, source in ((payload, "payload"), (carried_context, "carried_context")):
        if not isinstance(container, dict):
            continue
        for key in ("external_block", "external-block", "blocked_by_external",
                    "external_status", "external_blocked"):
            if key not in container:
                continue
            raw = container.get(key)
            if raw is True:
                return {"hit": True, "source": f"{source}.{key}", "marker": "true"}
            if _marker_hit(raw) or str(raw or "").strip().lower() in (
                    "1", "true", "yes", "on", "blocked", "external-block"):
                return {"hit": True, "source": f"{source}.{key}",
                        "marker": str(raw).strip()}
    return {"hit": False, "source": "", "marker": ""}


def is_redispatch_payload(payload) -> bool:
    """payload/context 是否声明为 watchdog/自动重派。"""
    if not isinstance(payload, dict):
        return False
    for key in REDISPATCH_MARKERS:
        if payload.get(key):
            return True
    # patrol 补偿器以 compensated_by 标记重派（值形如 ``patrol:<task>:<ts>``）。
    compensated_by = str(payload.get("compensated_by") or "").lower()
    if "patrol" in compensated_by or "watchdog" in compensated_by:
        return True
    context = str(payload.get("context") or "").lower()
    return any(marker in context for marker in REDISPATCH_CONTEXT_MARKERS)


def _parse_ts(value):
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


def _event_issue_ref(issue_number) -> str:
    return f"#{str(issue_number).lstrip('#')}" if issue_number else ""


def _query_segment_rows(repo_key: str, issue_number) -> list[tuple]:
    """同 Issue 的 a2a_event 段（seq, agent_type, status, created_at），按 seq 升序。"""
    if not repo_key or not is_native_issue_ident(issue_number):
        return []
    try:
        from src.domains.runtime import public as _runtime_public
        return _runtime_public.query_events(
            "seq, agent_type, status, created_at",
            where="repo_key=? AND issue_id=? AND event_type=?",
            params=(repo_key, _event_issue_ref(issue_number), "a2a_event"),
            order_by="seq ASC")
    except Exception as exc:
        log.warning(f"[watchdog-redispatch] segment query failed "
                    f"repo={repo_key} issue={issue_number}: {exc}")
        return []


def load_issue_terminal_inputs(repo_key: str, issue_number) -> dict:
    """读取 Issue status/stage（best-effort，查询失败按空串）。"""
    status = stage = ""
    try:
        from src.api.session_store import get_issue_status
        status = (get_issue_status(repo_key, _event_issue_ref(issue_number)) or "").strip()
    except Exception as exc:
        log.debug(f"[watchdog-redispatch] issue status read failed: {exc}")
    try:
        from src.api.session_store import get_issue_stage
        stage = str((get_issue_stage(repo_key, _event_issue_ref(issue_number)) or {})
                    .get("stage") or "").strip()
    except Exception as exc:
        log.debug(f"[watchdog-redispatch] issue stage read failed: {exc}")
    return {"status": status, "stage": stage}


def evaluate_pipeline_terminal(status: str = "", stage: str = "",
                               segments=None, external_block: dict | None = None) -> dict:
    """纯判定：目标 Issue 是否已完成（流水线终态 / qa 通过 / 外部阻塞）。

    ``segments`` 为按 seq 升序的 ``(seq, agent_type, status, created_at)`` 段列表。
    ``qa-passed`` 判据：最新流水线段由 qa 产出且状态 completed（对应 #3888 的
    qa 双轮通过后不应再回派 coding）。``status``/``stage`` 覆盖用户已收敛的单据。
    """
    block = external_block if isinstance(external_block, dict) else {}
    if block.get("hit"):
        return {"terminal": True, "reason": "external_block",
                "criteria_source": block.get("source") or "external_block",
                "external_block": True, "qa_passed": False,
                "matched_marker": block.get("marker") or ""}

    status = str(status or "").strip()
    stage = str(stage or "").strip()
    if status in _TERMINAL_STATUSES:
        return {"terminal": True, "reason": "completed",
                "criteria_source": "issue.status", "external_block": False,
                "qa_passed": False, "matched_marker": status}
    if stage in _TERMINAL_STAGES:
        return {"terminal": True, "reason": "completed",
                "criteria_source": "issue.stage", "external_block": False,
                "qa_passed": False, "matched_marker": stage}

    rows = [r for r in (segments or []) if r]
    if rows:
        last = rows[-1]
        last_agent = str(last[1] or "").strip().lower()
        last_status = str(last[2] or "").strip().lower()
        if last_agent == "qa" and last_status == "completed":
            return {"terminal": True, "reason": "qa_passed",
                    "criteria_source": f"pipeline.segment.seq={last[0]}",
                    "external_block": False, "qa_passed": True,
                    "matched_marker": f"qa:{last_status}"}
    return {"terminal": False, "reason": "", "criteria_source": "",
            "external_block": False, "qa_passed": False, "matched_marker": ""}


def resolve_pipeline_terminal_state(repo_key: str, issue_number, payload=None,
                                    labels=()) -> dict:
    """读取并判定目标 Issue 的终态（labels/payload 参与 external-block 判定）。"""
    inputs = load_issue_terminal_inputs(repo_key, issue_number)
    carried = payload.get("carried_context") if isinstance(payload, dict) else None
    block = detect_external_block(payload=payload, carried_context=carried, labels=labels)
    segments = _query_segment_rows(repo_key, issue_number)
    return evaluate_pipeline_terminal(
        status=inputs.get("status"), stage=inputs.get("stage"),
        segments=segments, external_block=block)


def _query_recent_dispatch_payloads(repo_key: str, issue_number,
                                    target_agent: str) -> list[tuple]:
    if not repo_key or not is_native_issue_ident(issue_number):
        return []
    try:
        from src.domains.runtime import public as _runtime_public
        return _runtime_public.query_events(
            "seq, created_at, payload",
            where="repo_key=? AND issue_id=? AND event_type=? AND agent_type=?",
            params=(repo_key, _event_issue_ref(issue_number), "a2a_event",
                    str(target_agent or "").strip()),
            order_by="seq DESC", limit=50)
    except Exception as exc:
        log.warning(f"[watchdog-redispatch] dispatch payload query failed "
                    f"repo={repo_key} issue={issue_number}: {exc}")
        return []


def _payload_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def count_recent_redispatches(repo_key: str, issue_number, target_agent: str,
                              window_seconds: int | None = None) -> int:
    """时间窗内同 Issue + 同目标 agent 的 watchdog 自动重派次数。"""
    window = (DEFAULT_REDISPATCH_WINDOW_SECONDS if window_seconds is None
              else int(window_seconds))
    if window <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    count = 0
    for _seq, created_at, payload_raw in _query_recent_dispatch_payloads(
            repo_key, issue_number, target_agent):
        if not is_redispatch_payload(_payload_dict(payload_raw)):
            continue
        dt = _parse_ts(created_at)
        if dt is None or dt < cutoff:
            continue
        count += 1
    return count


def redispatch_limit_state(repo_key: str, issue_number, target_agent: str,
                           limit: int | None = None,
                           window_seconds: int | None = None) -> dict:
    """同 Issue 重派上限判定。超限返回 ``{"hit": True, ...}``，否则 ``{"hit": False}``。"""
    cap = DEFAULT_REDISPATCH_LIMIT if limit is None else int(limit)
    if cap <= 0:
        return {"hit": False, "count": 0, "limit": cap, "window": 0}
    window = (DEFAULT_REDISPATCH_WINDOW_SECONDS if window_seconds is None
              else int(window_seconds))
    count = count_recent_redispatches(repo_key, issue_number, target_agent, window)
    return {"hit": count >= cap, "count": count, "limit": cap, "window": window}


def resolve_original_context(repo_key: str, issue_number, target_agent: str,
                             payload=None) -> str:
    """回取重派原始 context：优先源事件 seq，其次同 Issue 同目标最近一条非空 context。"""
    seqs: list[int] = []
    if isinstance(payload, dict):
        for key in ("source_event_seq", "source_seq", "ref_seq", "redispatch_of",
                    "original_event_seq", "exclude_event_seq"):
            raw = payload.get(key)
            try:
                num = int(raw or 0)
            except (TypeError, ValueError):
                num = 0
            if num > 0 and num not in seqs:
                seqs.append(num)
    if seqs:
        try:
            from src.domains.runtime import public as _runtime_public
            for seq in seqs:
                ctx = str((_runtime_public.event_payload(seq) or {}).get("context") or "").strip()
                if ctx:
                    return ctx
        except Exception as exc:
            log.debug(f"[watchdog-redispatch] original context read failed: {exc}")
    for _seq, _created_at, payload_raw in _query_recent_dispatch_payloads(
            repo_key, issue_number, target_agent):
        ctx = str(_payload_dict(payload_raw).get("context") or "").strip()
        if ctx:
            return ctx
    return ""


def record_redispatch_skip(repo_key: str, issue_number, target_agent: str,
                           reason: str, detail: str = "") -> str:
    """结构化留痕（event_skipped + 日志），返回 detail 供调用方回显。"""
    try:
        from src.api.a2a_dispatch_guard import record_skip
        record_skip(repo_key, issue_number, target_agent, reason, detail=detail)
    except Exception as exc:
        log.warning(f"[watchdog-redispatch] record skip failed: {exc}")
    log.info(f"[watchdog-redispatch] skip repo={repo_key} issue=#{issue_number} "
             f"target={target_agent} reason={reason} {detail}")
    return detail


def guard_watchdog_redispatch(repo_key: str, issue_number, target_agent: str,
                              payload=None, labels=()) -> dict:
    """重派前综合守卫：终态预检 → 频次上限 → context 非空保护。

    返回 ``{}`` 表示放行；否则返回含 ``reason``/``detail``/``terminal`` 的判定
    结果，调用方据此短路本次派发。仅作用于 coding 目标（Spec 范围）。
    """
    if not repo_key or not issue_number:
        return {}
    if str(target_agent or "").strip().lower() != "coding":
        return {}
    payload = payload if isinstance(payload, dict) else {}

    terminal = resolve_pipeline_terminal_state(repo_key, issue_number, payload, labels)
    if terminal.get("terminal"):
        detail = (f"target=coding criteria={terminal.get('criteria_source')} "
                  f"reason={terminal.get('reason')} "
                  f"marker={terminal.get('matched_marker') or ''}")
        record_redispatch_skip(repo_key, issue_number, target_agent,
                               PIPELINE_TERMINAL_REASON, detail)
        out = dict(terminal)
        out.update({"reason": PIPELINE_TERMINAL_REASON, "detail": detail})
        return out

    if is_redispatch_payload(payload):
        if not str(payload.get("context") or "").strip():
            original = resolve_original_context(repo_key, issue_number, target_agent, payload)
            if original:
                payload["context"] = original
                log.info(f"[watchdog-redispatch] backfilled original context "
                         f"repo={repo_key} issue=#{issue_number} len={len(original)}")
            else:
                detail = "target=coding criteria=payload.context empty and no original context"
                record_redispatch_skip(repo_key, issue_number, target_agent,
                                       CONTEXT_EMPTY_REASON, detail)
                return {"terminal": False, "reason": CONTEXT_EMPTY_REASON,
                        "detail": detail, "criteria_source": "payload.context",
                        "external_block": False, "qa_passed": False}
        limit = redispatch_limit_state(repo_key, issue_number, target_agent)
        if limit.get("hit"):
            detail = (f"target=coding count={limit['count']} limit={limit['limit']} "
                      f"window={limit['window']}s")
            record_redispatch_skip(repo_key, issue_number, target_agent,
                                   REDISPATCH_LIMIT_REASON, detail)
            return {"terminal": False, "reason": REDISPATCH_LIMIT_REASON,
                    "detail": detail, "criteria_source": "redispatch.count",
                    "external_block": False, "qa_passed": False,
                    "count": limit["count"], "limit": limit["limit"]}
    return {}