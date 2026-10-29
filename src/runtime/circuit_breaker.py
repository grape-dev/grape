"""引擎连续失败熔断模块（Issue #495）。

背景：codearts worker CLI 二进制损坏（rc=127）时 runtime 仍 status=online、
tag=all 持续参与任务认领，浪费任务配额并产生用户可见的"执行失败"误报。

机制（Spec 评论 196643584 + 196675863 修订确认）：
- 同一 runtime 连续任务失败达到阈值 N（默认 3，env
  ENGINE_CIRCUIT_BREAKER_THRESHOLD 可配置）→ 自动置 status='offline' +
  healthy=0 摘出调度池，写 WARNING 日志（claim 门控读 healthy 即时生效）。
- 任务成功（exit_code=0）→ 连续失败计数清零。
- 置 offline 保留记录，修复后可人工恢复（status/healthy 回写 online/1）。

计数存储：服务端进程内存（fail/complete 终态均回流服务端单进程，
无需跨进程持久化；进程重启计数归零属可接受语义——熔断保护期内离线状态
已落 DB，不依赖内存计数）。
"""
import os
import threading

from src.config import log

# 熔断阈值：同一 runtime 连续失败 N 次自动置 offline（默认 3，可配置）
FAILURE_THRESHOLD = int(os.environ.get("ENGINE_CIRCUIT_BREAKER_THRESHOLD", "3"))

_failure_counts: dict = {}  # {runtime_id: consecutive_failures}
_lock = threading.Lock()


def _db():
    from src.session_manager import db
    return db


def _db_lock():
    from src.session_manager import db_lock
    return db_lock


def get_failure_count(runtime_id: int) -> int:
    """当前连续失败计数（供测试/排障查询）。"""
    with _lock:
        return _failure_counts.get(runtime_id, 0)


def reset_all():
    """清空全部计数（测试辅助）。"""
    with _lock:
        _failure_counts.clear()


def record_task_failure(runtime_id: int, task_id: str = "", exit_code: int = -1) -> int:
    """任务失败上报：连续失败计数 +1；达阈值自动摘除（置 offline + healthy=0）。

    返回累计后的连续失败次数。runtime_id 无效（0/None）时忽略。
    摘除动作失败不影响主流程（熔断是增强层，不阻塞终态写入）。
    """
    if not runtime_id:
        return 0
    with _lock:
        count = _failure_counts.get(runtime_id, 0) + 1
        _failure_counts[runtime_id] = count
    if count < FAILURE_THRESHOLD:
        return count
    # 达阈值 → 熔断摘除（幂等：已 offline 不重复写）
    tripped = False
    try:
        with _db_lock():
            c = _db().cursor()
            c.execute(
                "UPDATE agent_runtimes SET status='offline', healthy=0 "
                "WHERE id=? AND status!='offline'",
                (runtime_id,))
            tripped = bool(c.rowcount)
            _db().commit()
    except Exception as e:
        log.warning(f"circuit_breaker: offline update failed for runtime={runtime_id}: {e}")
        return count
    if tripped:
        log.warning(
            f"circuit_breaker: runtime {runtime_id} TRIPPED OFFLINE after {count} "
            f"consecutive task failures (threshold={FAILURE_THRESHOLD}, "
            f"last task={task_id or 'n/a'}, exit_code={exit_code}); "
            f"manual recovery (status/healthy) required after fix")
    return count


def record_task_success(runtime_id: int):
    """任务成功上报：连续失败计数清零（偶发失败不累积熔断）。"""
    if not runtime_id:
        return
    with _lock:
        _failure_counts.pop(runtime_id, None)
