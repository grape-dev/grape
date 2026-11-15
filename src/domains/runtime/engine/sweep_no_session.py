"""「认领了但引擎会话从未建立」任务的独立兜底回收。

背景（2026-11-15，#87/#88 等 5 个 issue 永久「处理中」）：
- agent 认领任务后，引擎进程没起来（runtime_session_id 恒空、task_messages 恒 0）；
- 这类任务原本只由 agent 主动调 /recover-orphans 时才被处理，
  一旦 agent 自身卡住或不再调用，任务就永远停在 running、事件永远 running；
- 判死条件与 recover-orphans 保持一致（无会话 + 超阈值 + 最近无消息），
  避免误杀「静默型长任务」。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from src.domains.runtime.engine.cleanup import _sweep_cursor

log = logging.getLogger(__name__)

# 与 routes/runtime.py 的 CLAIM_NO_SESSION_FAIL_SECONDS 保持同一口径
NO_SESSION_FAIL_SECONDS = int(os.environ.get("CLAIM_NO_SESSION_FAIL_SECONDS", "90"))
# 近期有消息说明引擎确实活着，不动它
RECENT_MESSAGE_ACTIVE_SECONDS = int(os.environ.get("RUNTIME_RECENT_MESSAGE_ACTIVE_SECONDS", "180"))

FAIL_TXT = ("claimed but the engine session never started "
            "(runtime_session_id empty; the worker never spawned the engine process)")


def sweep_claim_without_session_once() -> int:
    """回收「认领超过阈值仍无引擎会话」的 running 任务，并把事件收敛为 failed。

    返回收敛的任务数。幂等：已被其它路径判终态的行不会被重复处理。
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=NO_SESSION_FAIL_SECONDS)).isoformat()
    msg_cutoff = (now - timedelta(seconds=RECENT_MESSAGE_ACTIVE_SECONDS)).isoformat()
    fixed = 0
    try:
        # 用 sweeper 专用游标（三库拓扑走独立连接），避免与请求路径抢进程级 db_lock
        with _sweep_cursor() as c:
            c.execute(
                "SELECT t.id, t.runtime_id, t.event_seq FROM agent_tasks t "
                "WHERE t.status IN ('running','dispatched') "
                "AND COALESCE(t.runtime_session_id,'')='' "
                "AND COALESCE(t.started_at, t.created_at) < ? "
                "AND NOT EXISTS (SELECT 1 FROM task_messages m "
                "                WHERE m.task_id=t.id AND m.created_at > ?)",
                (cutoff, msg_cutoff))
            rows = c.fetchall()
            for tid, rid, ev_seq in rows:
                cur = c.execute(
                    "UPDATE agent_tasks SET status='failed', exit_code=1, "
                    "result_text=?, completed_at=? "
                    "WHERE id=? AND status IN ('running','dispatched')",
                    (FAIL_TXT, now.isoformat(), tid))
                if not (getattr(cur, "rowcount", 0) or 0):
                    continue
                fixed += 1
                log.info(f"sweeper: task {tid} claimed without an engine session for "
                         f">{NO_SESSION_FAIL_SECONDS}s -> failed")
                if ev_seq:
                    try:
                        from src.domains.runtime.engine.service import _cascade_event_terminal
                        _cascade_event_terminal(c, ev_seq, "failed", exit_code=1)
                    except Exception as _ce:
                        log.debug(f"sweeper: cascade event {ev_seq} failed: {_ce}")
                if rid and int(rid) > 0:
                    try:
                        from src.domains.runtime.engine.service import release_busy_slot
                        release_busy_slot(int(rid), cursor=c)
                    except Exception as _re:
                        log.debug(f"sweeper: release busy slot {rid} failed: {_re}")
            try:
                c.connection.commit()
            except Exception as _ce:
                log.debug(f"sweeper: commit after no-session reclaim failed: {_ce}")
    except Exception as e:
        log.warning(f"sweeper claim-without-session sweep failed: {e}")
        return fixed
    return fixed