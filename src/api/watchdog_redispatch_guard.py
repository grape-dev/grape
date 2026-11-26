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
from src.kernel.issue_ident import is_native_issue_ident, normalize_issue_ident
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

# watchdog 专属来源标记：人工重派（events.py ``mark_event_redispatch_of``）只写
# ``redispatch_of``（整数源 seq），不得计入 watchdog 频次上限，也不得触发
# watchdog 终态/context 守卫。来源区分见 ``is_watchdog_redispatch_payload``。
WATCHDOG_REDISPATCH_MODES = ("watchdog", "auto_redispatch", "patrol")
WATCHDOG_MARKER_KEYS = ("watchdog_redispatch", "_reissue")

# Policy bound (B2 remediation): limit=3 attempts / window=1800s is a
# conservative operational cap for automated watchdog redispatch storms.
# <undecided: needs measured watchdog redispatch frequency distribution and
# human response latency before final tuning — registered on the pending
# decision list for PR !2290>; both values stay env-overridable via
# WATCHDOG_REDISPATCH_LIMIT / WATCHDOG_REDISPATCH_WINDOW_SECONDS.
DEFAULT_REDISPATCH_LIMIT = int(os.environ.get("WATCHDOG_REDISPATCH_LIMIT", "3"))
DEFAULT_REDISPATCH_WINDOW_SECONDS = int(
    os.environ.get("WATCHDOG_REDISPATCH_WINDOW_SECONDS", "1800"))


# Bounded recent-payload scan (B2 remediation): the caller applies the
# 1800s window filter to the returned rows, so the 50 most recent dispatch
# payloads per issue leave ample margin for a 3-attempts-per-window check
# while capping the worst-case query cost.
_REDISPATCH_PAYLOAD_QUERY_LIMIT = 50

_TERMINAL_STATUSES = set(TERMINAL_TASK_STATUSES) | set(PAUSED_ISSUE_STATUSES)
_TERMINAL_STAGES = set(TERMINAL_ISSUE_STAGES) | set(PAUSED_ISSUE_STATUSES)


def _norm_marker(raw) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def _marker_hit(value) -> bool:
    norm = _norm_marker(value)
    return bool(norm) and norm in {_norm_marker(m) for m in EXTERNAL_BLOCK_MARKERS}


def _labels_external_block(labels) -> str:
    """标签里是否带 external-block；返回命中的归一标签（未命中返回空串）。

    复用 ``a2a_terminal_guard.normalize_labels``，避免与 #2816 终态门禁的标签
    归一逻辑各写一套（QA 复审 Standards：重复代码）。
    """
    if not labels:
        return ""
    from src.api.a2a_terminal_guard import normalize_labels
    for item in normalize_labels(labels):
        if _marker_hit(item):
            return item
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
    """payload/context 是否声明为重派（宽口径，含人工重派标记）。"""
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


def is_watchdog_redispatch_payload(payload) -> bool:
    """payload/context 是否来自 watchdog/巡检自动重派（严格口径）。

    ``events.py`` 的人工重派（``mark_event_redispatch_of``）只写 ``redispatch_of``，
    因此这里不把单独的 ``redispatch_of`` 视为 watchdog 来源——否则人工重派会被
    计入 watchdog 频次上限并触发重派守卫（QA 复审 Spec 口径偏差）。
    """
    if not isinstance(payload, dict):
        return False
    mode = _norm_marker(payload.get("redispatch_mode"))
    if mode:
        return mode in {_norm_marker(m) for m in WATCHDOG_REDISPATCH_MODES}
    if any(payload.get(key) for key in WATCHDOG_MARKER_KEYS):
        return True
    compensated_by = str(payload.get("compensated_by") or "").lower()
    if "patrol" in compensated_by or "watchdog" in compensated_by:
        return True
    context = str(payload.get("context") or "").lower()
    return any(marker in context for marker in REDISPATCH_CONTEXT_MARKERS)


def _parse_ts(value):
    """复用 a2a_dispatch_guard._parse_ts（存储层时间口径唯一实现）。"""
    from src.api.a2a_dispatch_guard import _parse_ts as _shared_parse_ts
    return _shared_parse_ts(value)


def _event_issue_ref(issue_number) -> str:
    """归一为 ``#N`` 事件 issue_id（与 qa_gate / a2a 历史落库形态对齐）。"""
    num = normalize_issue_ident(issue_number)
    return f"#{num}" if num else ""


def _resolve_scene_key(repo_key: str, issue_number):
    """Issue 当前流水线场景键；解析异常返回 None（调用方按放行处理）。

    复用 ``qa_gate._issue_scene_key``，与 ``latest_qa_event`` 的场景口径保持一致。
    """
    num = normalize_issue_ident(issue_number)
    if not repo_key or not num:
        return None
    try:
        from src.api.qa_gate import _issue_scene_key
        return _issue_scene_key(repo_key, num)
    except Exception as exc:
        log.debug(f"[watchdog-redispatch] scene resolve failed "
                  f"repo={repo_key} issue=#{num}: {exc}")
        return None


def _query_segment_rows(repo_key: str, issue_number, scene_id=None,
                        default_scene: bool = False) -> list[tuple]:
    """同 Issue 同一流水线（scene_id）的 a2a_event 段，按 seq 升序。

    列：``seq, agent_type, status, result, structured_output, created_at``。
    ``scene_id`` 为空/``default`` 时并入历史空场景事件（与
    ``qa_gate.latest_qa_event`` 的 default_scene 口径一致），避免跨流水线旧段
    被当成本次终态（QA 复审 Spec：终态查询须按 pipeline/scene 收敛）。
    """
    num = normalize_issue_ident(issue_number)
    if not repo_key or not num:
        return []
    where = "repo_key=? AND issue_id IN (?, ?) AND event_type=?"
    params: list = [repo_key, f"#{num}", num, "a2a_event"]
    if scene_id is not None:
        if default_scene:
            where += " AND (scene_id=? OR scene_id='' OR scene_id IS NULL)"
        else:
            where += " AND scene_id=?"
        params.append(scene_id)
    _select = "seq, agent_type, status, result, structured_output, created_at"
    try:
        from src.domains.runtime import public as _runtime_public
        try:
            return _runtime_public.query_events_any(
                _select, where=where, params=tuple(params), order_by="seq ASC")
        except Exception:
            # 旧库无 structured_output 列：退回纯文本列，结论仍按文本判定。
            return _runtime_public.query_events_any(
                "seq, agent_type, status, result, created_at",
                where=where, params=tuple(params), order_by="seq ASC")
    except Exception as exc:
        log.warning(f"[watchdog-redispatch] segment query failed "
                    f"repo={repo_key} issue=#{num} scene={scene_id}: {exc}")
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

    ``segments`` 为按 seq 升序的
    ``(seq, agent_type, status, result, structured_output, created_at)`` 段列表。
    ``qa-passed`` 判据：最新流水线段由 qa 产出，且复用
    ``qa_gate.classify_qa_conclusion`` 判定为 pass。仅 status=completed 不足以
    判通过——QA 打回（文本「不通过」或 ``next_step=a2a_coding``）同样是
    completed，必须继续允许 coding 返工重派（QA 复审 Spec 高优）。
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
        last_status = str(last[2] or "").strip()
        if last_agent == "qa":
            last_result = last[3] if len(last) > 3 else ""
            last_structured = _payload_dict(last[4]) if len(last) > 4 else None
            verdict = "unknown"
            try:
                from src.api.qa_gate import classify_qa_conclusion
                verdict = classify_qa_conclusion(
                    last_status, last_result or "", structured=last_structured)
            except Exception as exc:
                log.warning(f"[watchdog-redispatch] qa conclusion classify failed "
                            f"seq={last[0]}: {exc}")
            if verdict == "pass":
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
    scene_key = _resolve_scene_key(repo_key, issue_number)
    if scene_key is None:
        segments: list = []
    else:
        segments = _query_segment_rows(
            repo_key, issue_number, scene_key,
            default_scene=(scene_key in ("", "default")))
    return evaluate_pipeline_terminal(
        status=inputs.get("status"), stage=inputs.get("stage"),
        segments=segments, external_block=block)


def load_issue_labels(repo_key: str, issue_number) -> list[str]:
    """best-effort 读取 Issue 标签（供巡检路径校验 external-block）。

    VCS 不可用/无 token/查询异常一律返回空列表（按未命中放行）。
    """
    num = normalize_issue_ident(issue_number)
    if not repo_key or not num:
        return []
    try:
        from src.api.a2a_terminal_guard import (
            fetch_issue_for_guard, normalize_labels, _FETCH_BUDGET_SECONDS)
        # Single timeout口径 (B2 remediation): share the terminal guard's
        # A2A_TERMINAL_GUARD_BUDGET (default 20s) instead of a second 10s scale.
        issue = fetch_issue_for_guard(
            repo_key, num, timeout=_FETCH_BUDGET_SECONDS)
        if not issue:
            return []
        return normalize_labels(issue.get("labels"))
    except Exception as exc:
        log.debug(f"[watchdog-redispatch] issue labels read failed "
                  f"repo={repo_key} issue=#{num}: {exc}")
        return []


def _query_recent_dispatch_payloads(repo_key: str, issue_number,
                                    target_agent: str,
                                    exclude_seq: int = 0) -> list[tuple]:
    if not repo_key or not is_native_issue_ident(issue_number):
        return []
    where = "repo_key=? AND issue_id=? AND event_type=? AND agent_type=?"
    params: list = [repo_key, _event_issue_ref(issue_number), "a2a_event",
                    str(target_agent or "").strip()]
    if int(exclude_seq or 0) > 0:
        # 排队事件在 drain 阶段复查守卫时排除自身，避免把本次派发计入上限而自锁。
        where += " AND seq<>?"
        params.append(int(exclude_seq))
    try:
        from src.domains.runtime import public as _runtime_public
        return _runtime_public.query_events_any(
            "seq, created_at, payload", where=where, params=tuple(params),
            order_by="seq DESC", limit=_REDISPATCH_PAYLOAD_QUERY_LIMIT)
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
                              window_seconds: int | None = None,
                              exclude_seq: int = 0) -> int:
    """时间窗内同 Issue + 同目标 agent 的 watchdog 自动重派次数。

    ``exclude_seq`` 用于排队事件 drain 阶段复查时排除自身（否则本次事件已落库，
    带上重派标记会被计入上限而自我拦截）。
    """
    window = (DEFAULT_REDISPATCH_WINDOW_SECONDS if window_seconds is None
              else int(window_seconds))
    if window <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    count = 0
    for _seq, created_at, payload_raw in _query_recent_dispatch_payloads(
            repo_key, issue_number, target_agent, exclude_seq=exclude_seq):
        if not is_watchdog_redispatch_payload(_payload_dict(payload_raw)):
            continue
        dt = _parse_ts(created_at)
        if dt is None or dt < cutoff:
            continue
        count += 1
    return count


def redispatch_limit_state(repo_key: str, issue_number, target_agent: str,
                           limit: int | None = None,
                           window_seconds: int | None = None,
                           exclude_seq: int = 0) -> dict:
    """同 Issue 重派上限判定。超限返回 ``{"hit": True, ...}``，否则 ``{"hit": False}``。"""
    cap = DEFAULT_REDISPATCH_LIMIT if limit is None else int(limit)
    if cap <= 0:
        return {"hit": False, "count": 0, "limit": cap, "window": 0}
    window = (DEFAULT_REDISPATCH_WINDOW_SECONDS if window_seconds is None
              else int(window_seconds))
    count = count_recent_redispatches(
        repo_key, issue_number, target_agent, window, exclude_seq=exclude_seq)
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
    """结构化留痕（event_skipped + 日志），返回 detail 供调用方回显。

    ``issue_number`` 先归一为不带 ``#`` 的编号再交给 ``record_skip``——后者会
    自行补 ``#``，原样传 ``#4334`` 会落成 ``##4334``（QA 复审 Standards）。
    """
    num = normalize_issue_ident(issue_number)
    try:
        from src.api.a2a_dispatch_guard import record_skip
        record_skip(repo_key, num, target_agent, reason, detail=detail)
    except Exception as exc:
        log.warning(f"[watchdog-redispatch] record skip failed: {exc}")
    log.info(f"[watchdog-redispatch] skip repo={repo_key} issue=#{num} "
             f"target={target_agent} reason={reason} {detail}")
    return detail


def guard_watchdog_redispatch(repo_key: str, issue_number, target_agent: str,
                              payload=None, labels=(), exclude_seq: int = 0) -> dict:
    """重派前综合守卫：终态预检 → 频次上限 → context 非空保护。

    返回 ``{}`` 表示放行；否则返回含 ``reason``/``detail``/``terminal`` 的判定
    结果，调用方据此短路本次派发。仅作用于 coding 目标、且仅为该派发声明为
    watchdog/自动重派的 payload（Spec 范围；普通 coding 派发不进入终态判定，
    避免误拦正常/人工派发——QA 复审 Spec）。
    """
    if not repo_key or not issue_number:
        return {}
    if str(target_agent or "").strip().lower() != "coding":
        return {}
    payload = payload if isinstance(payload, dict) else {}
    if not is_watchdog_redispatch_payload(payload):
        return {}

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
    limit = redispatch_limit_state(
        repo_key, issue_number, target_agent, exclude_seq=exclude_seq)
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
