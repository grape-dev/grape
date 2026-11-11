"""Issue #1938: 事件级联收敛补偿的归因模型（单一真源）。

背景（#587 引入）：任务进入终态、但关联事件仍停在 pending/running/dispatched（回报丢失）时，
服务端会把事件直接收敛到终态并写一句 result 文案。原实现把固定文案
「任务已完成：级联收敛（回报丢失补偿）」写进 `events.result`，而该位置在事件列表 / Issue
处理记录里就是「Agent 答复结论」——补偿伪装成 Agent 结论，既不可归因也无法与真实答复区分。

本模块把补偿结论拆成**结构化字段**（`cascade_reason` / `report_missing` / `triggered_by` /
`cascade_task_status` / `cascade_prev_event_status`），文案由字段渲染；同时提供展示层判定
（`is_agent_conclusion`）与指标上报。写入点仍是 `service._cascade_event_terminal` 唯一一处。

不变式：
- 补偿文案恒以 :data:`CASCADE_NOTICE` 开头，显式自述「系统补偿，原始回报丢失」。
- `report_missing` 恒为 1（补偿发生即原始回报确实丢失）；是否取到真实结论由 `cascade_reason` 区分。
- `timeout_failed` 任务的事件终态映射为 `failed` + 非零 exit_code（绝不写 completed/0）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ===== 补偿路径（triggered_by）=====
TRIGGER_UPDATE_TASK_STATUS = "update_task_status"   # 任务状态机事务内兜底
TRIGGER_SWEEPER = "sweeper"                         # 定期巡检兜底（scheduler）
TRIGGER_CONVERGE_EVENT = "converge_event"           # 管理端补偿端点
CASCADE_TRIGGERS = (TRIGGER_UPDATE_TASK_STATUS, TRIGGER_SWEEPER, TRIGGER_CONVERGE_EVENT)

# ===== 补偿结论来源（cascade_reason）=====
REASON_SYNTHESIZED = "report_missing_synthesized"   # 原始回报丢失且无真实结论 → 写补偿文案
REASON_BACKFILLED = "report_missing_backfilled"     # 原始回报丢失，但已回填真实结论
CASCADE_REASONS = (REASON_SYNTHESIZED, REASON_BACKFILLED)

#: 补偿文案统一前缀——展示层据此标注/过滤（历史文案亦在 :func:`is_cascade_result_text` 内识别）
CASCADE_NOTICE = "系统补偿，原始回报丢失"

#: 终态事件状态（与 service._TERMINAL_STATUSES 对齐）
_EVENT_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

#: 任务终态 → (事件终态, exit_code 兜底)。
#: `timeout_failed` 不是 events.status 的枚举值（新增枚举会波及状态机/去重/展示），
#: 故映射为 failed + -1：既不把超时失败记成完成，也不扩散状态机改动。
_TERMINAL_STATE_MAP = {
    "completed": ("completed", 0),
    "failed": ("failed", 1),
    "cancelled": ("cancelled", -1),
    "timeout_failed": ("failed", -1),
}


def cascade_event_terminal_state(task_status: str, exit_code: Optional[int] = None) -> Tuple[str, int]:
    """任务终态 → (事件终态, exit_code)。

    覆盖全部 ``TASK_TERMINAL_STATUSES``（completed/failed/cancelled/timeout_failed）。
    failed/timeout_failed 的 exit_code 显式非 0：调用方传 0 或空都按非 0 兜底，
    避免「失败被记成成功」。未知状态回退 failed/-1（**不再**写 completed/0）。
    """
    _st = (task_status or "").strip()
    _ev, _ec = _TERMINAL_STATE_MAP.get(_st, ("failed", -1))
    if _ev == "completed":
        return "completed", 0
    if _ev == "cancelled":
        # 保持 #587 语义：cancel 恒 exit_code=-1（cancel 不作为业务通过）
        return "cancelled", -1
    if exit_code is None or int(exit_code) == 0:
        return _ev, _ec
    return _ev, int(exit_code)


#: 终态语义说明（保留 #587 时代各变体携带的信息，不再用「任务已完成：级联收敛」这类措辞）
_STATUS_NOTES = {
    "cancelled": "任务已取消，cancel 不作为业务通过，保留事件独立终态",
    "failed": "任务失败，未收到详写终态回报",
    "timeout_failed": "任务超时失败（worker 未在阈值内自报），不得记为成功",
}


def build_cascade_result(*, task_status: str, prev_event_status: str,
                         triggered_by: str) -> str:
    """由结构化字段渲染补偿文案（不再硬编码常量、不再拼串兜底）。

    单行可读 + 可解析：`系统补偿，原始回报丢失｜补偿路径=…｜原始任务终态=…｜最后已知事件状态=…[｜说明=…]`
    """
    _st = (task_status or "").strip()
    _text = (f"{CASCADE_NOTICE}｜补偿路径={triggered_by or 'unknown'}"
             f"｜原始任务终态={_st or 'unknown'}"
             f"｜最后已知事件状态={prev_event_status or 'unknown'}")
    _note = _STATUS_NOTES.get(_st)
    if _note:
        _text += f"｜说明={_note}"
    return _text


def is_cascade_result_text(text: str) -> bool:
    """文本是否为补偿文案（含历史 #587 固定文案）。

    用于展示层对**存量**记录兜底识别（老行没有结构化字段）。
    """
    _t = (text or "").strip()
    if not _t:
        return False
    if _t.startswith(CASCADE_NOTICE):
        return True
    # 历史固定文案：「任务已完成：级联收敛（回报丢失补偿）」/「级联收敛：任务{status}」/
    # 「任务已取消：级联收敛（…）」——#1938 前只有这几类以「级联收敛」自述的补写措辞。
    if _t.startswith("级联收敛：") or "级联收敛（" in _t:
        return True
    return False


def is_structured_agent_output(text: str) -> bool:
    """文本是否为结构化 Agent 产出（与 routes/tasks.py 的组装口径一致）。"""
    _t = (text or "").strip()
    if not _t or not _t.startswith("{"):
        return False
    try:
        import json as _json
        _d = _json.loads(_t)
    except Exception:
        return False
    return isinstance(_d, dict) and bool(_d.get("actions") or _d.get("conclusion"))


def display_meta(*, cascade_reason: str = "", report_missing: Any = 0,
                 triggered_by: str = "", task_status: str = "",
                 prev_event_status: str = "", result_text: str = "") -> Dict[str, Any]:
    """展示层标注：是否系统补偿、是否计入「Agent 答复结论」口径、以及归因信息。

    口径决策：
    - 无补偿字段（历史正常回复）→ ``is_agent_conclusion=True``（可计入）。
    - ``report_missing_backfilled`` → 回填的是 Agent 真实结论（只是迟到），**可计入**。
    - ``report_missing_synthesized`` → 系统合成文案，**不计入** Agent 答复。
    - 老数据（无字段但文案命中历史固定串）→ 识别为「系统补偿（历史记录，无归因）」，不计入。
    """
    _reason = (cascade_reason or "").strip()
    _legacy = False
    if not _reason:
        if is_cascade_result_text(result_text):
            _reason = REASON_SYNTHESIZED
            _legacy = True
        else:
            return {
                "system_cascade": False,
                "report_missing": False,
                "cascade_reason": "",
                "cascade_triggered_by": "",
                "cascade_task_status": "",
                "cascade_prev_event_status": "",
                "cascade_label": "",
                "result_kind": "agent",
                "is_agent_conclusion": True,
            }
    _backfilled = _reason == REASON_BACKFILLED
    if _backfilled:
        _label = f"{CASCADE_NOTICE}（已从任务记录回填真实结论）"
        _kind = "system_compensation_backfilled"
    else:
        _label = f"{CASCADE_NOTICE}（历史记录，无归因）" if _legacy else CASCADE_NOTICE
        _kind = "system_compensation_synthesized"
    return {
        "system_cascade": True,
        "report_missing": _truthy_int(report_missing) or _legacy,
        "cascade_reason": _reason,
        "cascade_triggered_by": (triggered_by or "").strip(),
        "cascade_task_status": (task_status or "").strip(),
        "cascade_prev_event_status": (prev_event_status or "").strip(),
        "cascade_label": _label,
        "result_kind": _kind,
        "is_agent_conclusion": _backfilled,
    }


def _truthy_int(value: Any) -> bool:
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return False


def normalize_label(value: str, fallback: str = "unknown") -> str:
    """指标标签规范化：空值/None → fallback，超长截断（防标签基数爆炸与非法字符）。"""
    _v = (value or "").strip()
    if not _v:
        return fallback
    return _v[:64]


#: events 表补偿归因列（顺序与 :func:`fetch_display_meta` 读取一致）
CASCADE_FIELDS = ("cascade_reason", "report_missing", "triggered_by",
                  "cascade_task_status", "cascade_prev_event_status")


def fetch_display_meta(cursor, seqs, result_by_seq: Optional[Dict[Any, str]] = None) -> Dict[int, Dict[str, Any]]:
    """按 seq 批量取补偿归因 → ``{seq: display_meta(...)}``（展示层共用）。

    - 老库缺列 / 查询异常 → 返回空 dict（调用方退回「无补偿字段」口径，绝不抛错）；
    - ``result_by_seq`` 可选：已知 result 文本时用于匹配历史固定文案（存量兜底识别）。
    """
    _seqs = [int(s) for s in (seqs or []) if s]
    if not _seqs:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    try:
        _ph = ",".join("?" * len(_seqs))
        rows = cursor.execute(
            f"SELECT seq, {', '.join(CASCADE_FIELDS)} FROM events "
            f"WHERE seq IN ({_ph})", _seqs).fetchall()
        for _r in rows or []:
            _seq = int(_r[0])
            _res = (result_by_seq or {}).get(_seq, "")
            out[_seq] = display_meta(
                cascade_reason=_r[1] or "", report_missing=_r[2] or 0,
                triggered_by=_r[3] or "", task_status=_r[4] or "",
                prev_event_status=_r[5] or "", result_text=_res)
    except Exception:
        return {}
    return out


def record_compensation(*, agent_type: str, scene_id: str, triggered_by: str,
                        task_status: str) -> None:
    """上报「回报丢失补偿」次数（异常静默：可观测不得影响补偿主流程）。"""
    try:
        from src.domains.insight.observability import record_cascade_compensation
        record_cascade_compensation(
            agent_type=normalize_label(agent_type),
            scene_id=normalize_label(scene_id, fallback="default"),
            trigger=normalize_label(triggered_by),
            task_status=normalize_label(task_status),
        )
    except Exception:  # pragma: no cover - 指标不可用不影响业务
        pass