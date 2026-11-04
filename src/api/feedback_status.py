"""反馈状态口径（Issue #673 落地，与 #22 通知机制共用同一映射）。

统一 user_feedback.status 与 issue（issue_state 阶段 stage / task status）的映射：
- 反馈状态枚举：open / in_progress / resolved / closed（前后端共用，#673 / #22 一套口径）
- issue.stage（vod/issue/coding/qa/sre/done）→ 反馈状态推导
- 联动同步只升不降（open < in_progress < resolved < closed）：
  admin 手工标记的 resolved/closed 不被自动同步回退覆盖。
"""
from src.config import log
from src.session_manager import db, db_lock

# 反馈状态枚举（与 routes/feedback.py update_feedback_status 白名单一致）
FEEDBACK_STATUS_OPEN = "open"
FEEDBACK_STATUS_IN_PROGRESS = "in_progress"
FEEDBACK_STATUS_RESOLVED = "resolved"
FEEDBACK_STATUS_CLOSED = "closed"

# 状态推进序（只升不降判断依据；closed 为人工终态）
FEEDBACK_STATUS_ORDER = {
    FEEDBACK_STATUS_OPEN: 0,
    FEEDBACK_STATUS_IN_PROGRESS: 1,
    FEEDBACK_STATUS_RESOLVED: 2,
    FEEDBACK_STATUS_CLOSED: 3,
}

# issue.stage（vod/issue/coding/qa/sre/done）→ 反馈状态
ISSUE_STAGE_TO_FEEDBACK_STATUS = {
    "vod": FEEDBACK_STATUS_OPEN,
    "issue": FEEDBACK_STATUS_OPEN,
    "coding": FEEDBACK_STATUS_IN_PROGRESS,
    "qa": FEEDBACK_STATUS_IN_PROGRESS,
    "sre": FEEDBACK_STATUS_IN_PROGRESS,
    "done": FEEDBACK_STATUS_RESOLVED,
}

# issue.status（任务态）→ 反馈状态（覆盖 stage 推导）
_ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS = {
    "completed": FEEDBACK_STATUS_RESOLVED,
    "closed": FEEDBACK_STATUS_CLOSED,
    "cancelled": FEEDBACK_STATUS_CLOSED,
}


def derive_feedback_status(stage: str, task_status: str = "") -> str:
    """由 issue 的 (stage, task_status) 推导反馈状态（#673/#22 共用口径）。

    task_status 优先（completed/closed/cancelled 为终态）；
    否则按 stage：分析阶段（vod/issue）=open，执行阶段（coding/qa/sre）=in_progress，
    done=resolved（失败完成的卡片不视为 resolved，由 stage 兜底为 open）。
    """
    ts = (task_status or "").strip().lower()
    if ts in _ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS:
        return _ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS[ts]
    return ISSUE_STAGE_TO_FEEDBACK_STATUS.get((stage or "").strip().lower(),
                                              FEEDBACK_STATUS_OPEN)


def sync_feedback_statuses(limit: int = 200) -> int:
    """联动同步：user_feedback.status ← 关联 issue 的 (stage, status)。

    范围：已转换（issue_url 非空）且带内部 issue_id 的反馈；
    规则：推导状态 rank 高于当前状态才更新（只升不降，不覆盖 admin 手工终态）。
    返回更新条数。由 feedback reconcile 周期调用（#673），#22 通知机制复用本函数同一口径。
    """
    updated = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT f.id, f.status, i.stage, i.status FROM user_feedback f "
                "JOIN issue i ON i.id = f.issue_id "
                "WHERE f.issue_url!='' AND f.issue_id IS NOT NULL AND f.is_deleted=0 "
                "ORDER BY f.id LIMIT ?",
                (limit,))
            rows = c.fetchall()
            for fid, cur, stage, task_status in rows:
                derived = derive_feedback_status(stage or "", task_status or "")
                cur = (cur or "").strip().lower() or FEEDBACK_STATUS_OPEN
                if derived == cur:
                    continue
                if FEEDBACK_STATUS_ORDER.get(derived, 0) <= FEEDBACK_STATUS_ORDER.get(cur, 0):
                    continue
                c.execute("UPDATE user_feedback SET status=? WHERE id=?", (derived, fid))
                updated += 1
            db.commit()
        if updated:
            log.info(f"feedback status sync: {updated} rows updated")
    except Exception as e:
        log.warning(f"feedback status sync failed: {e}")
    return updated
