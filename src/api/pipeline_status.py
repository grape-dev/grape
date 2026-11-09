"""流水线节点状态统一计算模块。

以菜单流水线页的"事件状态法"为基准，任务详情页对齐到同一套逻辑。
新增 cancelled(4) 和 skipped(5) 状态码。

Issue #539：pipeline 汇总新增「需求级业务状态」（review_status）与
阶段「任务/结果引用」（segments[].output.task_ref / result_ref），
与既有「事件执行状态」（顶层 status = 事件执行结束）明确区分。
"""

import json as _json
import re

# 状态码（与菜单流水线页现有 0/1/2/3 兼容，新增 4/5）
STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_COMPLETED = 2
STATUS_FAILED = 3
STATUS_CANCELLED = 4    # 新增
STATUS_SKIPPED = 5      # 新增

# event status → node status 映射
EVENT_STATUS_MAP = {
    "pending": STATUS_PENDING,
    "running": STATUS_RUNNING,
    "completed": STATUS_COMPLETED,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
}

# 流水线终态阶段
DONE_STAGES = ("done", "pending_review")


def compute_node_statuses(nodes, issue_events, current_stage):
    """统一计算流水线节点状态。

    参数：
        nodes: 节点列表（来自场景配置或 AGENT_ORDER 兜底）
        issue_events: 该 Issue 的事件列表（按 seq ASC）
        current_stage: issue_state.stage

    返回：(nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表
    """
    # 深拷贝 nodes 避免修改原始配置
    nodes = [dict(n) for n in nodes]
    is_done = current_stage in DONE_STAGES

    # 按 agent_type 分组，保留最新一条事件（后覆盖前；先按 seq 升序排序，不依赖调用方传入顺序）
    events_by_agent = {}
    for e in sorted(issue_events, key=lambda x: x.get("seq") or 0):
        at = e.get("agent_type", "")
        if at:
            events_by_agent[at] = e

    statuses = []
    for n in nodes:
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)

        if latest_event:
            # 有事件：按最新 event 状态映射
            evt_status = latest_event.get("status", "pending")
            status = EVENT_STATUS_MAP.get(evt_status, STATUS_PENDING)

            # done 阶段：cancelled 视为 completed
            if is_done and status == STATUS_CANCELLED:
                status = STATUS_COMPLETED
        else:
            # 无事件
            if is_done:
                status = STATUS_SKIPPED  # 流水线已结束但该节点未执行
            else:
                status = STATUS_PENDING

        n["node_status"] = status
        n["latest_event"] = latest_event
        statuses.append(status)

    return nodes, statuses


# ===================== Issue #539：需求级业务状态 + 阶段任务/结果引用 =====================
# 语义澄清：pipeline 顶层 `status` 仅表示「事件执行结束」（阶段执行语义，
# 如 completed = 最后一个事件执行完毕），不代表需求已确认或生产发布完成。
# 需求级业务状态由新增可选字段 `review_status`（+ `issue_stage` / `issue_task_status`）
# 独立暴露；SRE 等阶段的真实执行结论经 `segments[].output.task_ref` / `result_ref`
# 引用+摘要呈现（不内嵌全量 result_text，避免泄漏受限日志/凭据）。

# review_status 枚举（需求级业务状态）
REVIEW_STATUS_PENDING_REVIEW = "pending_review"   # 待人工确认（≠ 阶段执行完成）
REVIEW_STATUS_COMPLETED = "completed"             # 需求已完成（含生产发布完成）
REVIEW_STATUS_CANCELLED = "cancelled"             # 需求已取消
REVIEW_STATUS_AWAITING_INPUT = "awaiting_input"   # 等待用户澄清
REVIEW_STATUS_IN_PROGRESS = "in_progress"         # 流水线执行中（生产尚未开始/被打回）
REVIEW_STATUS_UNKNOWN = "unknown"                 # 无法判定（显式标注，不用空值）

# result_ref.status 枚举（阶段任务结果引用状态）
RESULT_REF_AVAILABLE = "available"     # 有可读结论（附脱敏摘要）
RESULT_REF_PENDING = "pending"         # 任务在途（queued/dispatched/running），尚无结果
RESULT_REF_UNAVAILABLE = "unavailable"  # 无关联任务 / 无结果或结果不可读

_TASK_RUNNING_STATUSES = ("queued", "dispatched", "running")
_RESULT_SUMMARY_LIMIT = 240

# 凭据/token 类敏感模式（摘要前强制脱敏；复用 event_archive _redact 同思路）
_SENSITIVE_TOKEN_RES = (
    re.compile(r"(?:tk_|grape_|rta_|gh[pousr]_|AK|SK)[A-Za-z0-9_\-]{10,}"),
)
_UNAVAILABLE_TASK_REF = {"task_id": None, "task_status": "unavailable", "query_path": ""}
_UNAVAILABLE_RESULT_REF = {"status": RESULT_REF_UNAVAILABLE}


def derive_review_status(stage, task_status):
    """由需求级 stage（issue/issue_state.stage）与 task_status（issue.status）
    推导需求级业务状态。规则（优先级从高到低）：

    Issue #1599（审核态漂移收敛，方案 A）：**以 task_status 为唯一事实源**判定
    pending_review，`stage=pending_review` 不再单独判待审核——避免 resume 后
    status=processing 而 stage 残留 pending_review 时页面仍显示「待审核」。

    1. task_status=pending_review → 待人工确认；
    2. task_status=cancelled → 已取消；
    3. task_status=completed 或 stage=done → 需求已完成（含生产发布完成）；
    4. task_status=awaiting_input → 等待用户澄清；
    5. 其余在途（active/processing 或任一非空 stage）→ 流水线执行中；
    6. 两者为空 → unknown（显式标注）。
    """
    stage = (stage or "").strip()
    task_status = (task_status or "").strip()
    if task_status == "pending_review":
        return REVIEW_STATUS_PENDING_REVIEW
    if task_status == "cancelled":
        return REVIEW_STATUS_CANCELLED
    if task_status == "completed" or stage == "done":
        return REVIEW_STATUS_COMPLETED
    if task_status == "awaiting_input":
        return REVIEW_STATUS_AWAITING_INPUT
    if task_status in ("active", "processing") or stage:
        return REVIEW_STATUS_IN_PROGRESS
    return REVIEW_STATUS_UNKNOWN


def redact_sensitive(text):
    """摘要脱敏：token/AK-SK 类模式替换为 [REDACTED]（Issue #539：不泄漏凭据）。"""
    if not text:
        return ""
    out = str(text)
    for rx in _SENSITIVE_TOKEN_RES:
        out = rx.sub(lambda m: m.group(0)[:3] + "[REDACTED]", out)
    return out


def extract_result_summary(result_text, limit=_RESULT_SUMMARY_LIMIT):
    """从任务 result_text 提取可外露的结论摘要。

    result_text 可能是结构化 JSON（tasks complete 组装：conclusion/
    issue_summary/...），优先取 conclusion → issue_summary → result_text
    字段；否则按原文截断。脱敏 + 压缩空白 + 限长。空/不可解析 → ""。
    """
    if not result_text:
        return ""
    raw = str(result_text).strip()
    if not raw:
        return ""
    picked = raw
    try:
        obj = _json.loads(raw)
        if isinstance(obj, dict):
            for k in ("conclusion", "issue_summary", "result_text"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    picked = v.strip()
                    break
    except Exception:
        pass
    picked = redact_sensitive(picked)
    picked = re.sub(r"\s+", " ", picked).strip()
    if len(picked) > limit:
        picked = picked[:limit] + "..."
    return picked


def build_task_ref(task_id, task_status):
    """构造可授权查询的任务引用（task_ref）。"""
    return {
        "task_id": task_id,
        "task_status": task_status or "",
        "query_path": f"/api/runtime/tasks/{task_id}/status",
    }


def build_result_ref(task_status, result_text):
    """构造阶段结果引用（result_ref）：显式枚举，不用空值。

    - 任务在途 → pending；
    - 有可读结论 → available + 脱敏摘要；
    - 无关联任务 / 无结果或不可读 → unavailable。
    """
    if (task_status or "") in _TASK_RUNNING_STATUSES:
        return {"status": RESULT_REF_PENDING}
    summary = extract_result_summary(result_text)
    if not summary:
        return dict(_UNAVAILABLE_RESULT_REF)
    return {"status": RESULT_REF_AVAILABLE, "summary": summary}


def load_issue_business_status(repo_keys, number):
    """读需求级 (stage, task_status)：issue 单表权威，issue_state 兜底。

    与 issues._issue_stage_map / _task_status_map 同口径（Issue #539 数据源）。
    """
    from src.session_manager import db, db_lock
    stage, task_status = "", ""
    if not repo_keys:
        return stage, task_status
    placeholders = ",".join("?" * len(repo_keys))
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT stage, status FROM issue "
                f"WHERE repo_key IN ({placeholders}) AND issue_number=?",
                list(repo_keys) + [str(number)])
            row = c.fetchone()
            if row:
                stage, task_status = row[0] or "", row[1] or ""
        if not stage:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT stage FROM issue_state "
                    f"WHERE repo_key IN ({placeholders}) AND issue_id=?",
                    list(repo_keys) + [f"#{number}"])
                row = c.fetchone()
                if row:
                    stage = row[0] or ""
    except Exception:
        pass
    return stage, task_status


def load_segment_task_refs(repo_keys, seqs=None):
    """按 event_seq 聚合 agent_tasks 的 (task_ref, result_ref)。

    同一 seq 多任务时保留最后创建的一条（ORDER BY created_at/id ASC 后覆盖）。
    seqs 给定时仅取这些事件的任务（避免整仓拉取 result_text 大字段）。
    查询失败 → 空映射（调用方按 unavailable 显式标注，不阻塞 pipeline）。
    """
    from src.session_manager import db, db_lock
    out = {}
    if not repo_keys:
        return out
    placeholders = ",".join("?" * len(repo_keys))
    params = list(repo_keys)
    where = f"repo_key IN ({placeholders}) AND event_seq IS NOT NULL"
    if seqs:
        seqs = [s for s in seqs if s]
        if seqs:
            where += f" AND event_seq IN ({','.join('?' * len(seqs))})"
            params += list(seqs)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT id, event_seq, status, result_text FROM agent_tasks "
                f"WHERE {where} ORDER BY created_at ASC, id ASC",
                params)
            rows = c.fetchall()
    except Exception:
        return out
    for tid, seq, st, rt in rows:
        try:
            seq_i = int(seq)
        except (TypeError, ValueError):
            continue
        out[seq_i] = (build_task_ref(tid, st), build_result_ref(st, rt))
    return out


def _pipeline_segment_seqs(pipeline):
    """提取 pipeline 各段 seq（供任务引用按需取数）。"""
    seqs = []
    for seg in (pipeline.get("segments") or []) if isinstance(pipeline, dict) else []:
        if isinstance(seg, dict):
            seqs.append(seg.get("seq"))
    return seqs


def enrich_pipeline_business_status(pipeline, stage, task_status, task_refs=None):
    """就地叠加需求级业务状态与阶段任务/结果引用（全部新增可选字段）。

    旧字段（issue/summary/status/segments[].output.conclusion 等）语义与
    结构不变；无任务关联的段用显式 unavailable 标注。
    """
    if not isinstance(pipeline, dict):
        return pipeline
    pipeline["issue_stage"] = (stage or "").strip()
    pipeline["issue_task_status"] = (task_status or "").strip()
    pipeline["review_status"] = derive_review_status(stage, task_status)
    refs = task_refs if task_refs is not None else {}
    for seg in pipeline.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        out = seg.get("output")
        if not isinstance(out, dict):
            out = {}
            seg["output"] = out
        try:
            seq_i = int(seg.get("seq") or 0)
        except (TypeError, ValueError):
            seq_i = 0
        pair = refs.get(seq_i)
        if pair:
            out["task_ref"] = pair[0]
            out["result_ref"] = pair[1]
        else:
            out["task_ref"] = dict(_UNAVAILABLE_TASK_REF)
            out["result_ref"] = dict(_UNAVAILABLE_RESULT_REF)
    return pipeline
