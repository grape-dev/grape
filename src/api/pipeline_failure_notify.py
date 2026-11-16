"""Issue #2432：流水线节点失败通知（飞书群）与去重。

失败终态统一（执行失败 / 超时 / 重试耗尽）在 ``event_store`` 的终态收口处
落库 ``events.failed_node`` 并调用本模块。本模块负责：

1. 去重：按「需求 + 节点」维度，绑定节点状态周期——同一节点上一个终态仍为
   ``failed`` 时视为同一失败周期，不重复通知；节点恢复（completed/cancelled）
   后再次失败会重新通知。
2. 内容：Issue 编号 / 标题 + 失败节点名；**不含失败原因**。
3. 发送：异步入队 IM outbox（飞书群），异常只记日志，不阻断流水线主流程。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def should_notify_for_cycle(prior_terminal_statuses) -> bool:
    """同一节点「上一层终态」是否为 failed 决定是否属于新失败周期。

    prior_terminal_statuses: 当前失败事件之前的同节点终态列表（按 seq 升序）。
    - 无历史终态 → 新周期，需通知；
    - 上一个终态 = failed → 同一失败周期，不重复通知；
    - 上一个终态 = completed/cancelled → 节点已恢复，本次失败是新周期，需通知。
    """
    last = ""
    for st in prior_terminal_statuses or []:
        st = str(st or "").strip()
        if st in _TERMINAL_STATUSES:
            last = st
    return last != "failed"


def load_prior_terminal_statuses(repo_key: str, issue_id: str, agent_type: str, seq: int) -> list:
    """取当前失败事件之前、同 (repo, issue, node) 的终态事件状态（seq 升序）。"""
    if not repo_key or not issue_id or not agent_type:
        return []
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT status FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
                "AND seq<? AND status IN ('completed','failed','cancelled') ORDER BY seq ASC",
                (repo_key, issue_id, agent_type, int(seq or 0)))
            return [(r[0] or "") for r in c.fetchall()]
    except Exception as e:
        log.warning(f"[2432] load prior terminal statuses failed seq={seq}: {e}")
        return []


def _redispatch_after_last_failure(repo_key: str, issue_id: str, agent_type: str,
                                   seq: int) -> bool:
    """Issue #2432 review: 同一个失败周期内是否发生过人工重派。

    去重口径原本只看「上一个终态是否 failed」，于是「失败 → 重派 → 再失败」
    被当成同一周期、第二条通知被吞掉。重派（redispatch 成功写入的
    ``_reissue`` 标记）说明运维已经介入处理过这一轮失败，后续失败属于新的
    失败周期，需要再通知一次。标记只取「上一次失败之后」的新事件，避免历史
    重派标记把之后所有失败都当成新周期而刷屏。
    """
    if not repo_key or not issue_id or not agent_type:
        return False
    from src.api.pipeline_constants import REISSUE_EVENT_PAYLOAD_KEY
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, status FROM events WHERE repo_key=? AND issue_id=? "
                "AND agent_type=? AND seq<? ORDER BY seq ASC",
                (repo_key, issue_id, agent_type, int(seq or 0)))
            last_failed = 0
            for r in c.fetchall():
                if str(r[1] or "") == "failed":
                    last_failed = int(r[0] or 0)
            if not last_failed:
                return False
            c.execute(
                "SELECT 1 FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
                "AND seq>? AND seq<? AND payload LIKE ? LIMIT 1",
                (repo_key, issue_id, agent_type, last_failed, int(seq or 0),
                 f'%"{REISSUE_EVENT_PAYLOAD_KEY}"%'))
            return c.fetchone() is not None
    except Exception as e:
        log.warning(f"[2432] redispatch probe failed seq={seq}: {e}")
        return False


def _issue_number(issue_id: str) -> int:
    _s = str(issue_id or "").strip().lstrip("#")
    return int(_s) if _s.isdigit() else 0


def _issue_title(repo_key: str, issue_number: int) -> str:
    if not repo_key or not issue_number:
        return ""
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT title FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                      (repo_key, issue_number))
            row = c.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def _node_name(agent_type: str) -> str:
    try:
        from src.domains.runtime.pipeline.event_pipeline import get_agent_display_name
        return get_agent_display_name(agent_type) or agent_type
    except Exception:
        return agent_type


def build_failure_text(issue_number: int, title: str, node_name: str) -> str:
    """失败通知正文：Issue 编号/标题 + 失败节点名（不含失败原因）。"""
    lines = ["流水线节点执行失败，请及时跟进。", ""]
    if issue_number:
        lines.append(f"Issue：#{issue_number}")
    if title:
        lines.append(f"标题：{title}")
    lines.append(f"失败节点：{node_name or '—'}")
    return "\n".join(lines)


def notify_node_failure(repo_key: str, issue_id: str, agent_type: str, seq: int = 0) -> dict:
    """失败节点通知入口（同步：仅入队 outbox，实际发送由异步消费者完成）。

    返回 {"notified": bool, "reason": str}，任何异常都不上抛。
    """
    if not repo_key or not agent_type:
        return {"notified": False, "reason": "missing_repo_or_node"}
    issue_number = _issue_number(issue_id)
    if not issue_number:
        # PR 订阅伪 issue / 无关联 Issue：不做群通知（内容要求 Issue 编号/标题）
        return {"notified": False, "reason": "no_issue_number"}
    try:
        prior = load_prior_terminal_statuses(repo_key, issue_id, agent_type, seq)
        if not should_notify_for_cycle(prior):
            # Issue #2432 review: 人工重派过这一轮失败后再次失败 = 新周期
            if not _redispatch_after_last_failure(repo_key, issue_id, agent_type, seq):
                return {"notified": False, "reason": "duplicate_cycle"}
        node_name = _node_name(agent_type)
        title = _issue_title(repo_key, issue_number)
        text = build_failure_text(issue_number, title, node_name)
        from src.domains.runtime.im.notify import notify_repo_group
        res = notify_repo_group(
            repo_key, "pipeline_failed", text, issue_number=issue_number,
            discriminator=f"{agent_type}:{seq}")
        return {"notified": bool((res or {}).get("queued")), "reason": (res or {}).get("reason", ""),
                "node": node_name}
    except Exception as e:
        log.warning(f"[2432] notify node failure failed repo={repo_key} seq={seq}: {e}")
        return {"notified": False, "reason": "error"}