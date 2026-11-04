"""批次级上报失败降级（Issue #975）。

背景：worker→master 消息批次遇 413/4xx 时，原实现把失败当成功吞掉
（client._request >=400 → None → report_messages 返回 {} → agent 清空 batch），
整批 task_messages 静默丢失且无占位。

本模块提供降级链路：二分拆批重发 → 单条仍失败发 dropped 占位 + 落补报队列。
被 agent.py（执行中 flush）与 worker_report_queue.replay_pending_reports（补报重放）
共用，两侧降级策略一致。
"""
import json
import time

from src.config import log
from src.runtime.client import ReportBatchError

# 拆批深度上限：50 → 25 → 12 → … → 1 约 6 层；超限直接逐条，防止病态递归
MAX_SPLIT_DEPTH = 6

# 占位消息 data 截断长度：占位必须小到能通过触发 413 的 body 限制
_PLACEHOLDER_DATA_HEAD = 200


class SplitOutcome:
    """拆批重发结果（可观测计数）。"""

    def __init__(self):
        self.delivered = 0        # 成功送达条数
        self.failed: list = []    # 仍失败的消息（单条 4xx 或网络错误保留项）
        self.terminal = False     # 过程中确认任务已终态（410）


async def split_resend(report, messages, depth: int = 0, outcome: SplitOutcome = None) -> SplitOutcome:
    """二分拆批重发一批消息。

    report: async (messages) -> resp 的上报函数（即 client.report_messages 语义：
            dict=成功（status ok）、None=任务终态、{}=网络错误、raise ReportBatchError=批次级 4xx）。
    遇 ReportBatchError 递归二分；单条仍失败/网络错误保留到 outcome.failed 交由调用方降级。
    """
    if outcome is None:
        outcome = SplitOutcome()
    if not messages or outcome.terminal:
        return outcome
    if len(messages) == 1:
        try:
            resp = await report(messages)
        except ReportBatchError:
            outcome.failed.append(messages[0])
            return outcome
        if resp is None:
            outcome.terminal = True
            return outcome
        if isinstance(resp, dict) and resp.get("status") == "ok":
            outcome.delivered += 1
            return outcome
        # 网络错误：单条保留，交由调用方落补报队列
        outcome.failed.append(messages[0])
        return outcome
    if depth >= MAX_SPLIT_DEPTH:
        for m in messages:
            await split_resend(report, [m], depth + 1, outcome)
            if outcome.terminal:
                return outcome
        return outcome
    half = len(messages) // 2
    for part in (messages[:half], messages[half:]):
        if outcome.terminal:
            return outcome
        try:
            resp = await report(part)
        except ReportBatchError:
            await split_resend(report, part, depth + 1, outcome)
            continue
        if resp is None:
            outcome.terminal = True
            return outcome
        if isinstance(resp, dict) and resp.get("status") == "ok":
            outcome.delivered += len(part)
            continue
        # 网络错误：整段保留，交由调用方落补报队列
        outcome.failed.extend(part)
    return outcome


def build_dropped_placeholder(failed: list, err: ReportBatchError) -> dict:
    """构造 dropped 占位消息（小 payload，能在触发 413 的 body 限制下送达）。

    字段：dropped/count/reason/first_type/data_head——排障时区分占位与真实消息。
    """
    first = failed[0] if failed else {}
    first_data = first.get("data") if isinstance(first, dict) else first
    try:
        head = str(first_data)[:_PLACEHOLDER_DATA_HEAD]
    except Exception:
        head = ""
    return {
        "type": "log",
        "data": json.dumps({
            "dropped": True,
            "count": len(failed),
            "reason": f"http {getattr(err, 'status', '?')}",
            "first_type": str(first.get("type", "")) if isinstance(first, dict) else "",
            "data_head": head,
        }, ensure_ascii=False),
    }


def truncate_message(message: dict, data_limit: int = 4000) -> dict:
    """截断单条消息 data，保证补报队列重放时不再触发 413。"""
    try:
        m = dict(message)
        d = m.get("data")
        if d is None:
            return m
        if isinstance(d, str) and len(d) > data_limit:
            m["data"] = d[:data_limit] + "…[truncated]"
            return m
        if isinstance(d, dict):
            s = json.dumps(d, ensure_ascii=False)
            if len(s) > data_limit:
                m["data"] = {"_truncated": True, "head": s[:data_limit]}
        return m
    except Exception:
        return {"type": "log", "data": "[unserializable message]"}


async def degrade_failed_batch(report, task_id: str, batch: list, err: ReportBatchError,
                               enqueue=None) -> dict:
    """4xx 批次级失败降级入口：拆批重发 → 单条失败发占位 + 落补报队列。

    enqueue: 可选 callable(entry)（WorkerReportQueue.add）——补报队列可用时传入。
    返回 {"delivered", "dropped", "queued", "terminal"} 供调用方记录与决策。
    """
    outcome = await split_resend(report, batch)
    queued = 0
    if outcome.failed:
        placeholder = build_dropped_placeholder(outcome.failed, err)
        try:
            await report([placeholder])
            log.warning(
                f"report degrade: task={task_id} dropped={len(outcome.failed)} "
                f"reason=http {getattr(err, 'status', '?')} placeholder=delivered")
        except Exception as e:
            log.warning(f"report degrade: task={task_id} placeholder send failed: {e}")
        if enqueue:
            _ts = time.time()
            for i, m in enumerate(outcome.failed):
                try:
                    enqueue({
                        "kind": "messages",
                        "task_id": f"{task_id}#msg-{_ts}-{i}",
                        "task": task_id,
                        "message": truncate_message(m),
                    })
                    queued += 1
                except Exception as e:
                    log.warning(f"report degrade: task={task_id} enqueue failed: {e}")
    if outcome.delivered or outcome.failed or outcome.terminal:
        log.info(f"report degrade: task={task_id} delivered={outcome.delivered} "
                 f"dropped={len(outcome.failed)} queued={queued} terminal={outcome.terminal}")
    return {"delivered": outcome.delivered, "dropped": len(outcome.failed),
            "queued": queued, "terminal": outcome.terminal}
