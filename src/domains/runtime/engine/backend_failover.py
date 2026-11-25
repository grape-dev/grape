"""Issue #3928：spawn 期后端不可用 → 自动重派 + runtime 熔断。

与业务失败严格区分：只有「后端命令不存在 / 引擎启动器起不来」这类环境失败才走
本模块；业务失败仍走原有瞬时重试 / 判死链路。
"""
from __future__ import annotations

import os
import re

from src.kernel.config import log
from src.kernel.owner_db import owner_db
from src.domains.runtime.engine import backend_health
from src.domains.runtime.engine.backend_capability import (
    is_missing_cli_error,
    canonical_engine_type,
)

#: 默认重派上限（实际值在调用期读 env，QA 复审 Optional：import 期求值会让
#: ``GRAPE_BACKEND_FAILOVER_MAX`` 运行期不可调、用例也验证不到上限）。
DEFAULT_FAILOVER_MAX = 2

_STARTUP_RE = re.compile(
    r"(engine\s+startup\s+failed|pre_exec\s+failed|spawn\s+failed|"
    r"failed\s+to\s+start\s+(?:the\s+)?engine|backend\s+(?:not\s+available|unavailable))",
    re.I)

#: spawn 之前就失败的上报（agent.py `_execute_task_impl` 的 ``_error`` + ``_started=False``）：
#: 这些路径根本没启动引擎，不能作为「后端可执行」的证据（QA 复审 R3）。
_PRE_EXECUTION_RE = re.compile(
    r"^\s*(?:\[skill_mismatch\]|dependency\s+check\s+failed|"
    r"workspace\s+baseline|credential_\w+|platform_not_registered|empty_repo|"
    r"task\s+terminated\s+by\s+server)",
    re.I)
#: 引擎进程内报错（spawn 成功后的失败文案）：可作为「引擎确实跑过」的证据。
_ENGINE_EXECUTED_RE = re.compile(r"(engine\s+exit_code=|engine\s+errors:|no\s+done\s+event)", re.I)


def _failover_max() -> int:
    """重派上限（每次调用读 env，运行期可调）。"""
    try:
        return max(1, int(os.environ.get("GRAPE_BACKEND_FAILOVER_MAX", DEFAULT_FAILOVER_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_FAILOVER_MAX


def has_execution_evidence(text: str, exit_code=None) -> bool:
    """失败上报是否带有「引擎真实执行过」的证据（QA 复审 R3）。

    只有这种证据才允许释放熔断/复活实例；spawn 前的业务/配置类失败
    （skill_mismatch、prompt 为空、skill 未配置、workspace baseline、credential_*、
    dependency check 等）一律不算——它们没有验证后端 CLI 是否可执行。
    """
    s = str(text or "")
    if _PRE_EXECUTION_RE.search(s):
        return False
    if _ENGINE_EXECUTED_RE.search(s):
        return True
    try:
        return int(exit_code) >= 0
    except (TypeError, ValueError):
        return False


def is_backend_unavailable_error(text: str) -> bool:
    """错误文本是否属于「后端可执行文件/启动器不可用」的 spawn 期失败。"""
    s = str(text or "")
    if not s:
        return False
    if is_missing_cli_error(s):
        return True
    # 引擎启动器/pre_exec 明确失败（spawn failed / engine startup failed）：换健康
    # 同类型实例通常可恢复（缺二进制、本机资源/配置坏）；业务失败不走本通道。
    return bool(_STARTUP_RE.search(s))


def _db():
    return owner_db("runtime")


def _lock():
    """与 runtime 域既有约定同锁（同 backend_health._lock，QA 复审 Optional）。"""
    from src.kernel import db_registry
    if db_registry.uses_own_dsn("runtime"):
        return db_registry.domain_lock("runtime")
    from src.kernel.session_manager import db_lock
    return db_lock


def _runtime_name(runtime_id: int) -> str:
    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,)).fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def handle_task_failure(task_id: str, task: dict | None, error, exit_code=-1) -> str:
    """在 fail_task 终态写入前处理后端不可用失败。

    返回：
    - ``requeued``：已 requeue（调用方跳过本次判死）；
    - ``already_requeued``：重复上报，任务已在 queued（调用方跳过）；
    - ``exhausted``：重派超上限，已记录诊断（调用方按 failed 判死并告警）；
    - ``not_backend``：非后端失败（调用方继续原链路）。
    """
    if not task:
        return "not_backend"
    rid = int(task.get("runtime_id") or 0)
    if not rid:
        return "not_backend"

    text = str(error or "")
    if not is_backend_unavailable_error(text):
        # 非后端失败：只有「引擎真实执行过」的证据才释放熔断。spawn 前的
        # skill_mismatch / prompt 为空 / skill 未配置 / workspace baseline /
        # credential_* 等失败没有验证后端可执行性，释放会误复活缺 CLI 引擎（R3）。
        executed = has_execution_evidence(text, exit_code)
        try:
            with _lock():
                d = _db()
                c = d.cursor()
                backend_health.note_claim_outcome(c, rid, backend_failed=False,
                                                  executed=executed)
                d.commit()
        except Exception as e:
            log.debug(f"backend_failover: release half-open failed (runtime={rid}): {e}")
        return "not_backend"

    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT status, attempts, event_seq, pool_tag, pool_agent_type, agent_type "
                "FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    except Exception as e:
        log.warning(f"backend_failover: load task {task_id} failed: {e}")
        return "not_backend"
    if not row:
        return "not_backend"
    status, attempts, _event_seq, _pool_tag, _pool_agent_type, _agent_type = row
    if status == "queued":
        return "already_requeued"
    if status != "running":
        return "not_backend"

    engine = _runtime_name(rid) or str(task.get("engine_name_snapshot") or "") or "-"
    reason = text[:500]
    attempts = int(attempts or 0)
    max_attempts = _failover_max()

    # 先熔断故障 runtime（无论是否还能重派，故障实例都不应继续吞任务）
    circuit = backend_health.note_backend_failure(rid, reason)
    if attempts >= max_attempts:
        _append_error(task_id, {
            "error_code": "BACKEND_UNAVAILABLE_EXHAUSTED",
            "attempts": attempts,
            "max": max_attempts,
            "runtime_id": rid,
            "engine": canonical_engine_type(engine) or engine,
            "reason": reason[:240],
            "note": "backend unavailable and failover attempts exhausted; task will be failed",
        })
        log.warning(
            f"BACKEND_FAILOVER_EXHAUSTED: task_id={task_id} runtime_id={rid} engine={engine} "
            f"attempts={attempts}/{max_attempts} reason={reason[:240]}")
        return "exhausted"

    # 未超上限：requeue 回池，由健康同类型实例认领；故障实例已熔断。
    from src.domains.runtime.engine.transient_retry import _do_requeue
    _do_requeue(task_id)
    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row[0] != "queued":
            log.warning(f"backend_failover: requeue task {task_id} did not reach queued "
                        f"(status={row[0] if row else 'vanished'})")
            return "not_backend"
    except Exception:
        pass
    _append_error(task_id, {
        "error_code": "BACKEND_UNAVAILABLE_FAILOVER",
        "attempt": attempts + 1,
        "max": max_attempts,
        "runtime_id": rid,
        "engine": canonical_engine_type(engine) or engine,
        "reason": reason[:240],
        "note": "requeued to another healthy runtime; failed runtime circuit-opened",
    })
    log.info(
        f"BACKEND_FAILOVER_REQUEUE: task_id={task_id} runtime_id={rid} engine={engine} "
        f"attempt={attempts + 1}/{max_attempts} "
        f"circuit_open_until={circuit.get('circuit_open_until', '')}")
    return "requeued"


def _append_error(task_id: str, entry: dict) -> None:
    try:
        from src.domains.runtime.engine.transient_retry import _append_task_error
        _append_task_error(task_id, entry)
    except Exception as e:
        log.warning(f"backend_failover: append task error failed for {task_id}: {e}")