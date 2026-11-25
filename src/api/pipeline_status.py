"""流水线节点状态统一计算模块。

以菜单流水线页的"事件状态法"为基准，任务详情页对齐到同一套逻辑。
新增 cancelled(4) 和 skipped(5) 状态码。

Issue #539：pipeline 汇总新增「需求级业务状态」（review_status）与
阶段「任务/结果引用」（segments[].output.task_ref / result_ref），
与既有「事件执行状态」（顶层 status = 事件执行结束）明确区分。
"""

import json as _json
import logging
import re
from datetime import datetime, timezone

from src.api.pipeline_constants import (
    DEFAULT_CONFIRM_MINUTES,
    PAUSED_ISSUE_STATUSES,
    PIPELINE_SEGMENT_EVENT_TYPES,
    TERMINAL_ISSUE_STAGES,
    TERMINAL_TASK_STATUSES,
)

log = logging.getLogger(__name__)

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
    # Issue #3444：评论内容去重留痕事件（不派发任务）显式标为 skipped，
    # 避免未知状态按 pending 渲染成「待执行」。
    "skipped": STATUS_SKIPPED,
    "deduped": STATUS_SKIPPED,
    # 接管中：原 worker 已被 cancel，Issue 由本地交互式会话来推进。
    # 展示上等同「进行中」，但服务端不写 running —— runner 已退出，
    # 声称 running 会误导 claim / 串行槽 / 超时判断。
    "takeover": STATUS_RUNNING,
}

# 流水线终态阶段
DONE_STAGES = ("done", "pending_review")

# Issue #3444：评论内容去重留痕事件的状态集（与 event_store 的
# find_recent_duplicate_comment「真实处理事件」口径一致）。events 表中
# status='skipped'/'deduped' 的行均为去重/跳过留痕（不派发任务），
# 携带真实 agent_type 仅供审计定位。
DEDUP_LEFTOVER_STATUSES = ("skipped", "deduped")


def is_dedup_leftover_event(e) -> bool:
    """Issue #3444：判断事件是否为去重/跳过留痕（不参与节点状态聚合）。

    events 表中 status='skipped'/'deduped' 的行均为去重留痕
    （append_comment_dedup_event 写入，payload 带 `_dedup.deduped=True`），
    不派发任务、不代表节点执行进度；聚合「最新事件」时必须排除，否则重复评论
    到达后会把仍在 running / 已 completed 的真实节点状态覆盖成 skipped/pending。
    口径与 event_store.find_recent_duplicate_comment 的
    `status NOT IN ('skipped','deduped')`（真实处理事件）一致。

    按 status 单条件判定即可：append_comment_dedup_event 是 events.status='skipped'
    的唯一写入者（event_pipeline 提前 return 的 skipped 走 event_skipped 审计表，
    不落 events 行）；`deduped` 为预留别名（当前无写入者），保留在状态集内与判重
    查询口径对齐。SQL 侧（_issue_latest_event_status_by_agent）也按状态列过滤，
    不解析 payload，保证两处判定一致。
    """
    return isinstance(e, dict) and str(e.get("status") or "") in DEDUP_LEFTOVER_STATUSES


def extract_failed_node(nodes, statuses=None):
    """提取首个失败节点 (failed_node, failed_node_name)——路线图统一口径（Issue #2432）。

    nodes 可来自 compute_node_statuses（自带 node_status），也可配合 statuses 列表使用；
    多失败节点只取第一个，节点级 failed_node_name 优先，回退节点 name。
    """
    for i, node in enumerate(nodes or []):
        if statuses is not None:
            status = statuses[i] if i < len(statuses) else None
        else:
            status = node.get("node_status")
        if status != STATUS_FAILED:
            continue
        failed_node = node.get("failed_node") or node.get("id", "") or node.get("tag", "")
        failed_node_name = node.get("failed_node_name") or node.get("name") or failed_node
        return failed_node, failed_node_name
    return "", ""


def compute_node_statuses(nodes, issue_events, current_stage,
                          repo_key="", issue_id=""):
    """统一计算流水线节点状态。

    参数：
        nodes: 节点列表（来自场景配置或 AGENT_ORDER 兜底）
        issue_events: 该 Issue 的事件列表（按 seq ASC）
        current_stage: issue_state.stage
        repo_key / issue_id: 提供时查询 active 接管，命中节点锁定 running（Issue #3201）

    返回：(nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表
    """
    # 深拷贝 nodes 避免修改原始配置
    nodes = [dict(n) for n in nodes]
    is_done = current_stage in DONE_STAGES

    # Issue #3201：active 接管期间该节点状态固定 running，不被后续 completed 覆盖。
    takeover_agent = ""
    if repo_key and issue_id:
        try:
            from src.domains.runtime.engine.takeover import active_takeover_for
            # 只读形态：流水线列表/详情是 GET 读路径，不能在这里落库自愈
            # （N 个 issue = N 次写库 + N+1 查询，见 PR #1869 架构审查 M1）。
            _tko = active_takeover_for(repo_key, issue_id, readonly=True)
            if _tko:
                takeover_agent = str(_tko.get("agent_type") or "")
        except Exception as e:
            log.warning(f"compute_node_statuses: takeover lookup failed: {e}")

    # 按 agent_type 分组，保留最新一条事件（后覆盖前；先按 seq 升序排序，不依赖调用方传入顺序）
    # Issue #3201：skipped_takeover 是「被守门忽略」的事件，不参与最后一条事件映射。
    # Issue #3444：评论内容去重留痕（is_dedup_leftover_event）不参与——它是审计
    # 留痕而非节点执行记录，若成为「最新事件」会把仍在 running / 已 completed 的
    # 真实事件覆盖成 skipped/pending。
    events_by_agent = {}
    for e in sorted(issue_events, key=lambda x: x.get("seq") or 0):
        at = e.get("agent_type", "")
        if (at and str(e.get("status") or "") != "skipped_takeover"
                and not is_dedup_leftover_event(e)):
            events_by_agent[at] = e

    statuses = []
    for n in nodes:
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)
        # Issue #2268：组织场景节点 id 是 org_intake/org_dispatch/org_aggregate，
        # 但事件 agent_type 记录的是节点 tag（issue/coding/qa）。只按 id 匹配时
        # 组织流水线三个节点永远查不到事件 → 全部灰色「待处理」。
        if not latest_event:
            _tag = str(n.get("tag") or "").strip()
            if _tag and _tag != nid:
                latest_event = events_by_agent.get(_tag)

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

        if takeover_agent and nid == takeover_agent:
            status = STATUS_RUNNING
            n["takeover"] = True

        n["node_status"] = status
        n["latest_event"] = latest_event
        # Issue #2432：失败节点输出失败节点名（不输出失败原因）。
        # failed_node 为终态落库字段；老数据/未迁移库回退按事件 agent_type 推断。
        if status == STATUS_FAILED:
            _fn = (latest_event or {}).get("failed_node") or nid
            n["failed_node"] = _fn
            n["failed_node_name"] = n.get("name") or _fn
        else:
            n["failed_node"] = ""
            n["failed_node_name"] = ""
        statuses.append(status)

    return nodes, statuses


# ===================== Issue #2050：停滞态（stalled）可见性 =====================

STALL_STATE_RUNNING = "running"
STALL_STATE_COMPLETED = "completed"
STALL_STATE_PAUSED = "paused"
STALL_STATE_GRACE = "grace"
STALL_STATE_STALLED = "stalled"
STALL_STATE_UNKNOWN = "unknown"


def _pipeline_segments(issue_events):
    """取流水线段事件（排除 admin_action / assignee_changed 等非段事件）。

    当事件行带 ``event_type`` 时只认 a2a_event（真实的 agent 执行段）；
    历史 archive 段没有 event_type 字段时按全量段兼容（无法区分类型）。
    """
    typed = [e for e in issue_events if str(e.get("event_type") or "").strip()]
    if typed:
        # PIPELINE_SEGMENT_EVENT_TYPES 与 NON_PIPELINE_EVENT_TYPES 互斥（前者只有
        # a2a_event，后者是 admin_action/assignee_changed），旧写法里的排除子句恒真，
        # 已删（QA 第 5 轮 M3）。存在 typed 行时整体忽略无类型行：无类型行来自
        # archive 快照，同一 issue 同时出现两套时以 DB 的 typed 行为准，避免重复计段。
        return [e for e in typed
                if str(e.get("event_type") or "").strip() in PIPELINE_SEGMENT_EVENT_TYPES]
    return list(issue_events)


def _parse_stall_ts(value):
    """解析事件时间（统一复用巡检的时间规范：UTC aware ISO / MySQL CST）。"""
    if not value:
        return None
    try:
        from src.api.patrol import _parse_dt
        return _parse_dt(str(value))
    except Exception:
        return None


def compute_stall_state(issue_events, stage, task_status="", confirm_minutes=None):
    """判定流水线是否「完成但未接力（stalled）」并给出停滞时长（分钟）。

    判据与巡检兜底一致：末个流水线段（a2a_event）completed、issue 未到终态，
    且距末段 ``completed_at`` 已超过确认窗（默认 patrol:confirm_minutes）。
    admin_action / assignee_changed 等审计事件不参与末段判定；存在 running 段
    时视为正常在途（running）。「等用户」暂停态（awaiting_input 等
    PAUSED_ISSUE_STATUSES）既非停滞也非完成，返回 paused，不得上板为 stalled。
    缺少 completed_at 时不回退 created_at（后者含任务运行时长，会高估停滞时长）
    ——数据异常返回 unknown。返回新增可选字段（旧字段语义不变）：
    ``stall_state`` / ``stalled`` / ``stalled_minutes``。
    """
    stage = (stage or "").strip()
    task_status = (task_status or "").strip()
    out = {"stall_state": STALL_STATE_UNKNOWN, "stalled": False, "stalled_minutes": 0}
    events = [e for e in (issue_events or []) if isinstance(e, dict)]
    if task_status in PAUSED_ISSUE_STATUSES:
        out["stall_state"] = STALL_STATE_PAUSED
        return out
    if not events:
        return out
    events.sort(key=lambda e: e.get("seq") or 0)
    segments = _pipeline_segments(events)
    if not segments:
        return out
    last = segments[-1]
    last_status = str(last.get("status") or "").strip()
    if last_status in ("running", "dispatched", "queued", "pending"):
        out["stall_state"] = STALL_STATE_RUNNING
        return out
    if stage in TERMINAL_ISSUE_STAGES or task_status in TERMINAL_TASK_STATUSES:
        out["stall_state"] = STALL_STATE_COMPLETED
        return out
    if last_status != "completed":
        out["stall_state"] = STALL_STATE_UNKNOWN
        return out
    ts = _parse_stall_ts(last.get("completed_at") or "")
    if ts is None:
        return out
    minutes = max(0, int((datetime.now(timezone.utc) - ts).total_seconds() // 60))
    if confirm_minutes is None:
        try:
            from src.api.patrol import patrol_runtime_config
            confirm_minutes = patrol_runtime_config().get(
                "confirm_minutes", DEFAULT_CONFIRM_MINUTES)
        except Exception:
            confirm_minutes = DEFAULT_CONFIRM_MINUTES
    try:
        confirm_minutes = int(confirm_minutes)
    except (TypeError, ValueError):
        confirm_minutes = DEFAULT_CONFIRM_MINUTES
    out["stalled_minutes"] = minutes
    if minutes >= max(confirm_minutes, 0):
        out["stall_state"] = STALL_STATE_STALLED
        out["stalled"] = True
    else:
        out["stall_state"] = STALL_STATE_GRACE
    return out


def attach_stall_state(target, issue_events, stage, task_status="", confirm_minutes=None):
    """把停滞态字段叠加到 pipeline/详情响应对象（dict 或无操作）。"""
    if not isinstance(target, dict):
        return target
    target.update(compute_stall_state(issue_events, stage, task_status, confirm_minutes))
    return target


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
    stage, task_status = "", ""
    if not repo_keys:
        return stage, task_status
    # P4：issue / issue_state 归 runtime 域 —— 读语句在该域 public，本模块不再持 SQL
    from src.domains.runtime import public as _rt_public
    try:
        stage, task_status = _rt_public.issue_business_stage_status_any(repo_keys, str(number))
        if not stage:
            stage = _rt_public.issue_state_stage_any(repo_keys, f"#{number}") or ""
    except Exception as exc:  # noqa: BLE001 - 流水线展示不允许因业务状态查询失败而中断
        log.debug(f"load_issue_business_status: business status unavailable: {exc}")
    return stage, task_status


def load_segment_task_refs(repo_keys, seqs=None):
    """按 event_seq 聚合 agent_tasks 的 (task_ref, result_ref)。

    同一 seq 多任务时保留最后创建的一条（ORDER BY created_at/id ASC 后覆盖）。
    seqs 给定时仅取这些事件的任务（避免整仓拉取 result_text 大字段）。
    查询失败 → 空映射（调用方按 unavailable 显式标注，不阻塞 pipeline）。
    """
    out = {}
    if not repo_keys:
        return out
    # P4：agent_tasks 归 runtime 域 —— 语句与排序口径都在该域 public
    from src.domains.runtime import public as _rt_public
    try:
        rows = _rt_public.segment_task_rows_any(repo_keys, seqs)
    except Exception as exc:  # noqa: BLE001 - 任务引用失败按 unavailable 呈现，不阻塞 pipeline
        log.debug(f"load_segment_task_refs: task refs unavailable: {exc}")
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
    attach_stall_state(pipeline, pipeline.get("segments") or [], stage, task_status)
    return pipeline
