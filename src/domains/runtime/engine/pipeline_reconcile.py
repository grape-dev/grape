# -*- coding: utf-8 -*-
"""流水线推进确定性兜底（DB 为真源，不依赖技能 / 收尾钩子）。

背景：节点完成后是否进入下一节点，历史上依赖两条"软"路径——
1) 技能自己调用 ``grape a2a handoff``；
2) 任务收尾钩子（远程 ``complete_task`` / 本地 direct 收尾）按场景 next/handoff 自动推进。

任一路径缺失都会让 Issue 停在当前节点（如 coding 完成后不再进 qa）：
进程重启、旧进程接管、技能未显式交办、收尾钩子异常，都会触发。

本模块提供第三条、也是最终兜底的**硬路径**：周期性扫描「任务已 completed，
但 Issue.stage 仍停在该 agent」的记录，按场景配置幂等推进。谓词全部来自
数据库，执行结果也落库，技能行为不再影响流水线是否收口。

幂等：
- 推进事件使用 ``auto:<task_id>:<role>`` 幂等键（append_event 唯一索引兜底）；
- 扫描前检查 Issue 是否有在途任务/事件，避免与技能显式交办重复派发；
- 成功后 Issue.stage 前移，下一轮自然不再命中。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from src.kernel.config import log
from src.kernel.issue_ident import is_actionable_issue_number, native_issue_number

# 只补偿"近期"完成任务，避免首次上线把历史 Issue 全部推进。
_DEFAULT_LOOKBACK_HOURS = 6
# 同一任务两次尝试的最小间隔（秒）：避免 node_not_found 等场景每轮重复日志。
_RETRY_SECONDS = 600
_last_attempt: dict[str, float] = {}
# 同进程单飞：Mgmt 入口存在两条 patrol_loop（server_tasks + lifespan 兜底），
# 会同时进本函数；单飞让第二路直接跳过，避免重复扫描/抢同一任务。
_run_lock = threading.Lock()

# Issue 仍可推进的状态（pending_review/completed/cancelled/done 都视为已收口）。
_ADVANCEABLE_ISSUE_STATUS = ("active", "processing", "awaiting_input")


def _lookback_seconds() -> float:
    raw = os.environ.get("PIPELINE_ADVANCE_LOOKBACK_HOURS", "")
    try:
        hours = float(raw) if raw else _DEFAULT_LOOKBACK_HOURS
    except (TypeError, ValueError):
        hours = _DEFAULT_LOOKBACK_HOURS
    return max(0.25, hours) * 3600


def _age_seconds(ts) -> float | None:
    """completed_at → 距今秒数；无法解析返回 None（不猜测、不推进）。"""
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception as _e:
        log.debug(f"pipeline-advance: completed_at parse failed ({ts}): {_e}")
        return None


def _candidate_tasks(limit: int) -> list[dict]:
    """最近完成的流水线任务（预过滤：有事件序号、Issue 形态的 issue_id）。

    读侧走 bulk_read 免锁通道：持 db_lock 做批量扫描会让心跳/claim/complete
    与请求路径一起排队（G-01 故障谱），本函数只读，不应参与进程级串行化。
    """
    from src.kernel.bulk_read import reader

    with reader("runtime") as c:
        c.execute(
            "SELECT id, repo_key, issue_id, agent_type, event_seq, completed_at "
            "FROM agent_tasks "
            "WHERE status='completed' AND event_seq>0 AND completed_at IS NOT NULL "
            "AND issue_id LIKE '#%' "
            "ORDER BY completed_at DESC LIMIT ?",
            (int(limit),),
        )
        cols = [d[0] for d in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
    return rows


def _issue_state(repo_key: str, issue_number: int) -> dict:
    from src.kernel.bulk_read import reader

    with reader("runtime") as c:
        c.execute(
            "SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=? "
            "ORDER BY id LIMIT 1",
            (repo_key, issue_number),
        )
        row = c.fetchone()
    if not row:
        return {}
    return {"status": (row[0] or ""), "stage": (row[1] or "")}


def _should_attempt(task_id: str, now: float) -> bool:
    prev = _last_attempt.get(task_id) or 0
    if now - prev < _RETRY_SECONDS:
        return False
    if len(_last_attempt) > 5000:
        _last_attempt.clear()
    _last_attempt[task_id] = now
    return True


def reconcile_pipeline_progress(limit: int = 200,
                                lookback_hours: float | None = None) -> dict:
    """扫描并推进「任务已完成、stage 未前进」的节点。返回统计（供日志/测试）。

    - ``advanced``：本次成功创建下游 A2A 的任务数；
    - ``scanned``：候选任务数；
    - ``skipped``：谓词不满足（stage 已前移 / 在途 / 已收口 / 过期 / 非流水线节点）。
    """
    from src.kernel import org_metrics
    from src.kernel.otel import feature_span

    with feature_span("pipeline.advance", {"limit": int(limit),
                                           "lookback_hours": lookback_hours}) as span:
        stats = _reconcile_guarded(limit, lookback_hours)
        if span is not None:
            try:
                for _key in ("advanced", "scanned", "skipped"):
                    span.set_attribute(_key, int(stats.get(_key) or 0))
                span.set_attribute("busy", bool(stats.get("busy")))
            except Exception as _se:
                log.debug(f"pipeline-advance: span attribute failed: {_se}")
        try:
            org_metrics.observe_pipeline_advance(stats)
        except Exception as _e:  # 指标失败不影响收敛主流程
            log.debug(f"pipeline-advance: metrics observe skipped: {_e}")
        return stats


def _reconcile_guarded(limit: int, lookback_hours: float | None) -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"scanned": 0, "advanced": 0, "skipped": 0, "actions": [],
                "busy": True}
    try:
        return _reconcile_locked(limit, lookback_hours)
    finally:
        _run_lock.release()


def _reconcile_locked(limit: int, lookback_hours: float | None) -> dict:
    from src.domains.runtime.engine.service import run_auto_advance_hook
    from src.domains.runtime.public import has_inflight

    now = time.time()
    cutoff = (lookback_hours if lookback_hours is not None
              else _lookback_seconds() / 3600.0) * 3600
    stats = {"scanned": 0, "advanced": 0, "skipped": 0, "actions": []}
    for task in _candidate_tasks(limit):
        stats["scanned"] += 1
        task_id = str(task.get("id") or "")
        repo_key = str(task.get("repo_key") or "")
        agent_type = str(task.get("agent_type") or "").strip()
        issue_id = str(task.get("issue_id") or "")
        issue_number = native_issue_number(issue_id)
        if not task_id or not repo_key or not agent_type:
            stats["skipped"] += 1
            continue
        if not is_actionable_issue_number(issue_number):
            stats["skipped"] += 1
            continue
        age = _age_seconds(task.get("completed_at"))
        if age is None or age < 0 or age > cutoff:
            stats["skipped"] += 1
            continue
        state = _issue_state(repo_key, int(issue_number))
        if not state:
            stats["skipped"] += 1
            continue
        # 已收口 / 已人工待审 → 不推进
        if str(state.get("status") or "") not in _ADVANCEABLE_ISSUE_STATUS:
            stats["skipped"] += 1
            continue
        # stage 已前移（技能已显式交办 / 上一轮已推进）→ 不重复推进
        if str(state.get("stage") or "").strip() != agent_type:
            stats["skipped"] += 1
            continue
        # 下游已在途（异步交办尚未推进 stage 的窗口）→ 让在途路径负责
        try:
            if has_inflight(repo_key, issue_id):
                stats["skipped"] += 1
                continue
        except Exception as _e:
            log.warning(f"pipeline-advance: has_inflight check failed "
                        f"task={task_id}: {_e}")
            stats["skipped"] += 1
            continue
        if not _should_attempt(task_id, now):
            stats["skipped"] += 1
            continue
        try:
            result = run_auto_advance_hook(
                task_id, repo_key, issue_id, agent_type,
                int(task.get("event_seq") or 0), result_text="")
        except Exception as _e:
            log.warning(f"pipeline-advance: task {task_id} hook failed: {_e}")
            stats["skipped"] += 1
            continue
        action = str((result or {}).get("action") or "")
        _seq = int((result or {}).get("seq") or 0)
        # append_event 撞幂等键会返回负 seq（已有事件）：不是本次推进，不能计入
        # advanced；否则统计会虚高，且掩盖「历史自动事件失败/已存在」的真实状态。
        if action in ("a2a", "recheck") and _seq > 0:
            stats["advanced"] += 1
            stats["actions"].append({
                "task_id": task_id, "repo_key": repo_key, "issue_id": issue_id,
                "agent": agent_type, "action": action,
                "next": (result or {}).get("next_role") or "",
                "seq": (result or {}).get("seq") or 0,
            })
            log.info(f"pipeline-advance: {agent_type}->{(result or {}).get('next_role')} "
                     f"task={task_id} issue={repo_key}/{issue_id} "
                     f"action={action} seq={(result or {}).get('seq')}")
        else:
            stats["skipped"] += 1
    return stats