"""执行流「正常收尾但自报丢失」的判据：把这类任务收敛成 completed，而不是超时失败。

背景（2026-11-15 #85/#84 等 issue 卡在「处理中」并最终被判 timeout_failed）：
- 引擎跑完活，最后一条消息是 `Step N done (stopReason=end_turn)`；
- 随后那一次「自报完成」的 HTTP 调用没成功（超时/被重启打断/504），
  任务停在 pending_self_report；
- 兜底逻辑只看「有没有会话 + 静默多久」，于是把**已经干完的活**判成
  timeout_failed —— 结论被覆盖、issue 需要人工重派。

判定用执行流自身的终止信号，而不是时间去猜：
`sweep_pending_self_report_timeouts` 在判死之前先问这里，
命中即按 completed 收敛，保留 agent 原本写入的 result/exit_code。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 正常收尾的流结束标记。acp/opencode 侧写成 "stopReason=end_turn"，
# 部分后端只写 "Step N done" 不带原因，两者都视为「本步正常结束」。
_END_TURN_MARKERS = ("stopReason=end_turn", "stop_reason=end_turn", "end_turn")


def stream_ended_normally(cursor, task_id: str) -> bool:
    """最后一条执行流消息是否为「正常结束」。

    只认 step_finish 类型（或内容里明确带 end_turn）的消息；
    最后一条是 tool_use/tool_finish/reasoning 的一律返回 False ——
    那种情况是中途断掉，应该继续走超时判死。
    """
    try:
        rows = cursor.execute(
            "SELECT type, content FROM task_messages WHERE task_id=? "
            "ORDER BY seq DESC, id DESC LIMIT 6",
            (task_id,)).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug(f"stream_ended_normally query failed for {task_id}: {e}")
        return False
    if not rows:
        return False
    # 不能只看最后一条：acp 在 step_finish 之后还会补一条 reasoning/text 收尾，
    # 于是「最后一条」常常不是 step_finish（实测 #148 reasoning / #147 step_finish）。
    # 正确口径：从新往旧找第一个 step_finish；若它带 end_turn，且它之后没有新步骤
    # 再开跑（step_start / tool_use），就判定这一轮正常结束。
    for idx, row in enumerate(rows):
        _type = str(row[0] or "").strip().lower()
        _content = str(row[1] or "")
        if _type in ("step_start", "tool_use") and idx < len(rows) - 1:
            # 更靠近末尾的位置出现了新步骤 → 说明 flow 还在走，不是收尾
            if idx > 0:
                return False
        if _type in ("step_finish", "step_finished", "turn_finish"):
            if not any(m.lower() in _content.lower() for m in _END_TURN_MARKERS):
                return False
            # step_finish 之后（更靠近结尾的行，即 idx 更小的那些）不能有 step_start
            newer = rows[:idx]
            for _nr in newer:
                if str(_nr[0] or "").strip().lower() in ("step_start", "tool_use"):
                    return False
            return True
    return False