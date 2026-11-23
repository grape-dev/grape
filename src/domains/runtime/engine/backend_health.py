"""Issue #3928：引擎后端健康态 + runtime 熔断 + 跨引擎可观测。

- 注册/心跳观测：``available / degraded / unavailable / unknown`` 落
  ``agent_runtimes.backend_*``；blocking 状态立即 ``healthy=0`` 并告警（带 runtime_id）。
- 熔断：runtime_id 粒度，指数冷却（默认 300s 起、封顶 3600s）；冷却结束只允许一次
  半开试探；探活 ok 或任务成功自动恢复。
- 跨引擎：任务最终由与 session 指针不同引擎类型执行 → WARNING + 计数，按 task 去重。
"""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from src.kernel.config import log
from src.kernel.owner_db import owner_db, owner_lock
from src.domains.runtime.engine.backend_capability import (
    BLOCKING_STATUSES,
    STATUS_AVAILABLE,
    STATUS_DEGRADED,
    STATUS_UNKNOWN,
    STATUS_UNAVAILABLE,
    canonical_engine_type,
)

CIRCUIT_COOLDOWN_SECONDS = max(1, int(os.environ.get("RUNTIME_BACKEND_CIRCUIT_SECONDS", "300")))
CIRCUIT_COOLDOWN_MAX_SECONDS = max(
    CIRCUIT_COOLDOWN_SECONDS, int(os.environ.get("RUNTIME_BACKEND_CIRCUIT_MAX_SECONDS", "3600")))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: str) -> float | None:
    from src.domains.runtime.engine.health import parse_dt
    return parse_dt(value)


def _audit(c, runtime_id: int, runtime_name: str, kind: str, detail: dict) -> None:
    try:
        c.execute(
            "INSERT INTO grape_t_engine_lifecycle_audit "
            "(runtime_id, runtime_name, kind, scene_row_id, node_tag, detail, created_at) "
            "VALUES (?,?,?,0,'',?,?)",
            (runtime_id, runtime_name or "", kind,
             json.dumps(detail, ensure_ascii=False), _now_iso()))
    except Exception as e:
        log.debug(f"backend lifecycle audit insert failed (runtime={runtime_id}, kind={kind}): {e}")


def _runtime_row(c, runtime_id: int):
    try:
        return c.execute(
            "SELECT name, backend_status, backend_reason, backend_checked_at, "
            "circuit_open_until, circuit_fail_count, circuit_half_open, healthy "
            "FROM agent_runtimes WHERE id=?", (runtime_id,)).fetchone()
    except Exception:
        # 存量库尚未迁移（缺列）→ 不阻断主链路（fail-open，旧行为）
        return None


def _cooldown_seconds(fail_count: int) -> int:
    n = max(1, int(fail_count or 1))
    return min(CIRCUIT_COOLDOWN_SECONDS * (2 ** (n - 1)), CIRCUIT_COOLDOWN_MAX_SECONDS)


def observe_capability(c, runtime_id: int, status: str, reason: str = "",
                       checked_at: str = "") -> dict:
    """落库一次后端能力观测（注册/心跳同入口；按状态决定 healthy/熔断）。"""
    status = (status or "").strip().lower()
    if status not in (STATUS_AVAILABLE, STATUS_DEGRADED, STATUS_UNAVAILABLE, STATUS_UNKNOWN):
        return {}
    row = _runtime_row(c, runtime_id)
    if not row:
        return {}
    name, prev_status, _prev_reason, _prev_checked, until, fail_count, half, healthy = row
    checked_at = checked_at or _now_iso()
    prev_status = (prev_status or "").strip().lower()
    try:
        if status == STATUS_AVAILABLE:
            c.execute(
                "UPDATE agent_runtimes SET backend_status=?, backend_reason='', "
                "backend_checked_at=?, circuit_open_until='', circuit_fail_count=0, "
                "circuit_half_open=0 WHERE id=?",
                (STATUS_AVAILABLE, checked_at, runtime_id))
            if prev_status in BLOCKING_STATUSES:
                log.info(f"ENGINE_BACKEND_RECOVERED: runtime_id={runtime_id} engine={name} "
                         f"prev_status={prev_status} (probe ok, circuit cleared)")
                _audit(c, runtime_id, name, "backend_recovered",
                       {"prev_status": prev_status})
        elif status == STATUS_UNAVAILABLE:
            # 缺 CLI：立即摘除认领资格并熔断。首次进入或冷却已过期才开新冷却，
            # 后续同状态心跳只刷新原因，不重复计数/告警。
            _now = datetime.now(timezone.utc)
            _until_ts = _ts(until) if until else None
            need_open = (prev_status not in BLOCKING_STATUSES) or (not until) \
                or (_until_ts is not None and _until_ts <= _now.timestamp())
            if need_open:
                _until = (_now + timedelta(seconds=CIRCUIT_COOLDOWN_SECONDS)).isoformat()
                c.execute(
                    "UPDATE agent_runtimes SET backend_status=?, backend_reason=?, "
                    "backend_checked_at=?, healthy=0, circuit_open_until=?, "
                    "circuit_half_open=0 WHERE id=?",
                    (status, reason, checked_at, _until, runtime_id))
                log.warning(
                    f"ENGINE_BACKEND_UNAVAILABLE: runtime_id={runtime_id} engine={name} "
                    f"status={status} reason={str(reason)[:240]} "
                    f"circuit_open_until={_until} (excluded from claiming)")
                _audit(c, runtime_id, name, "backend_unavailable",
                       {"status": status, "reason": str(reason)[:500],
                        "circuit_open_until": _until})
            else:
                c.execute(
                    "UPDATE agent_runtimes SET backend_status=?, backend_reason=?, "
                    "backend_checked_at=?, healthy=0 WHERE id=?",
                    (status, reason, checked_at, runtime_id))
        elif status == STATUS_DEGRADED:
            # 命令在但探活/spawn 失败：摘除认领资格（backend_status 闸门），但不改写
            # healthy —— 保留 #1982 的 spawn_failed 独立计数/阈值语义（偶发资源紧张
            # 不立即摘除，持续失败由既有阈值降级）。
            c.execute(
                "UPDATE agent_runtimes SET backend_status=?, backend_reason=?, "
                "backend_checked_at=? WHERE id=?",
                (status, reason, checked_at, runtime_id))
        else:  # unknown：不摘除（防探针故障导致全量停摆）
            c.execute(
                "UPDATE agent_runtimes SET backend_status=?, backend_reason=?, "
                "backend_checked_at=? WHERE id=?",
                (STATUS_UNKNOWN, reason, checked_at, runtime_id))
    except Exception as e:
        log.warning(f"observe_capability failed (runtime={runtime_id}, status={status}): {e}")
        return {}
    return {"runtime_id": runtime_id, "status": status, "prev_status": prev_status,
            "healthy": healthy, "circuit_open_until": until}


def _open_circuit_in_tx(c, runtime_id: int, reason: str = "") -> dict:
    row = _runtime_row(c, runtime_id)
    if not row:
        return {}
    name, _prev_status, _prev_reason, _prev_checked, _until, fail_count, _half, _healthy = row
    fail_count = int(fail_count or 0) + 1
    cooldown = _cooldown_seconds(fail_count)
    until = (datetime.now(timezone.utc) + timedelta(seconds=cooldown)).isoformat()
    checked_at = _now_iso()
    c.execute(
        "UPDATE agent_runtimes SET backend_status=?, backend_reason=?, backend_checked_at=?, "
        "healthy=0, circuit_open_until=?, circuit_fail_count=?, circuit_half_open=0 WHERE id=?",
        (STATUS_UNAVAILABLE, reason, checked_at, until, fail_count, runtime_id))
    log.warning(
        f"ENGINE_BACKEND_CIRCUIT_OPEN: runtime_id={runtime_id} engine={name} "
        f"reason={str(reason)[:240]} fail_count={fail_count} cooldown={cooldown}s "
        f"circuit_open_until={until}")
    _audit(c, runtime_id, name, "backend_circuit_open",
           {"reason": str(reason)[:500], "fail_count": fail_count,
            "cooldown_seconds": cooldown, "circuit_open_until": until})
    return {"runtime_id": runtime_id, "fail_count": fail_count,
            "cooldown_seconds": cooldown, "circuit_open_until": until}


def _db():
    """agent_runtimes 归 runtime 域：三库拓扑下必须走 owner 连接。"""
    return owner_db("runtime")


def _lock():
    from src.kernel.owner_db import owner_lock as _owner_lock
    return _owner_lock("runtime")


def note_backend_failure(runtime_id: int, reason: str = "") -> dict:
    """spawn 期后端不可用 → 熔断该 runtime（计数 + 指数冷却 + WARNING）。"""
    with _lock():
        d = _db()
        c = d.cursor()
        try:
            out = _open_circuit_in_tx(c, runtime_id, reason)
            d.commit()
            return out
        except Exception as e:
            log.warning(f"note_backend_failure failed (runtime={runtime_id}): {e}")
            try:
                d.rollback()
            except Exception:
                pass
            return {}


def claim_allowed(runtime_id: int, now: float | None = None, cursor=None) -> bool:
    """认领闸门：blocking 状态在冷却期内拒绝；冷却结束仅放行一次半开试探。"""
    _now = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        if cursor is not None:
            row = _runtime_row(cursor, runtime_id)
        else:
            with _lock():
                row = _runtime_row(_db().cursor(), runtime_id)
    except Exception:
        return True
    if not row:
        return True
    _name, status, _reason, _checked, until, _fail_count, half, _healthy = row
    status = (status or "").strip().lower()
    if status not in BLOCKING_STATUSES:
        return True
    if not until:
        # 已观测为不可用但冷却尚未落库：保守拒绝（注册/心跳会补开冷却）
        return False
    until_ts = _ts(until)
    if until_ts is not None and until_ts > _now:
        return False
    return int(half or 0) == 0


def note_claim_started(c, runtime_id: int) -> None:
    """认领成功后置半开试探占位（同一 runtime 冷却结束后只允许一个试探任务）。"""
    row = _runtime_row(c, runtime_id)
    if not row:
        return
    name, status, reason, _checked, until, _fail_count, half, _healthy = row
    status = (status or "").strip().lower()
    if status not in BLOCKING_STATUSES or not until:
        return
    until_ts = _ts(until)
    now_ts = datetime.now(timezone.utc).timestamp()
    if until_ts is not None and until_ts <= now_ts and int(half or 0) == 0:
        c.execute("UPDATE agent_runtimes SET circuit_half_open=1 WHERE id=?", (runtime_id,))
        log.info(f"ENGINE_BACKEND_HALF_OPEN: runtime_id={runtime_id} engine={name} "
                 f"reason={str(reason or '')[:200]}")


def note_claim_outcome(c, runtime_id: int, backend_failed: bool = False) -> None:
    """认领任务结果：后端失败 → 续开冷却；成功/非后端失败 → 释放半开并恢复可用。"""
    if backend_failed:
        _open_circuit_in_tx(c, runtime_id, "backend unavailable during task execution")
        return
    row = _runtime_row(c, runtime_id)
    if not row:
        return
    name, status, _reason, _checked, until, _fail_count, half, healthy = row
    status = (status or "").strip().lower()
    if status not in BLOCKING_STATUSES and not until:
        return
    c.execute(
        "UPDATE agent_runtimes SET backend_status=?, backend_reason='', healthy=1, "
        "circuit_open_until='', circuit_fail_count=0, circuit_half_open=0 WHERE id=?",
        (STATUS_AVAILABLE, runtime_id))
    log.info(f"ENGINE_BACKEND_RECOVERED: runtime_id={runtime_id} engine={name} "
             f"prev_status={status or '-'} (task executed successfully, circuit cleared)")
    _audit(c, runtime_id, name, "backend_recovered",
           {"prev_status": status, "trigger": "task_success"})


def note_task_success(runtime_id: int) -> None:
    if not runtime_id:
        return
    try:
        with _lock():
            d = _db()
            c = d.cursor()
            note_claim_outcome(c, runtime_id, backend_failed=False)
            d.commit()
    except Exception as e:
        log.debug(f"note_task_success failed (runtime={runtime_id}): {e}")


# ── 跨引擎执行可观测 ─────────────────────────────────────────

_CROSS_ENGINE_SEEN: OrderedDict[str, float] = OrderedDict()
_CROSS_ENGINE_LOCK = threading.Lock()
_CROSS_ENGINE_TOTAL = 0
_CROSS_ENGINE_SUPPRESSED = 0
_CROSS_ENGINE_MAX_KEYS = 4096


def note_cross_engine(task_id: str, runtime_id: int, engine_name: str,
                      pointer_engine: str) -> bool:
    """任务由与 session 指针不同的引擎类型执行 → WARNING（按 task 去重）。

    返回 True 表示本次产生了告警；同 task 重复调用只累加 suppressed 计数。
    """
    global _CROSS_ENGINE_TOTAL, _CROSS_ENGINE_SUPPRESSED
    et = canonical_engine_type(engine_name) or (engine_name or "").strip().lower()
    pt = canonical_engine_type(pointer_engine) or (pointer_engine or "").strip().lower()
    if not et or not pt or et == pt:
        return False
    with _CROSS_ENGINE_LOCK:
        if task_id in _CROSS_ENGINE_SEEN:
            _CROSS_ENGINE_SUPPRESSED += 1
            return False
        _CROSS_ENGINE_SEEN[task_id] = datetime.now(timezone.utc).timestamp()
        while len(_CROSS_ENGINE_SEEN) > _CROSS_ENGINE_MAX_KEYS:
            _CROSS_ENGINE_SEEN.popitem(last=False)
        _CROSS_ENGINE_TOTAL += 1
        total = _CROSS_ENGINE_TOTAL
    log.warning(
        f"CROSS_ENGINE_EXECUTION: task_id={task_id} runtime_id={runtime_id} "
        f"engine={et} pointer_engine={pt} runtime={engine_name or '-'} "
        f"pointer={pointer_engine or '-'} "
        f"metric=grape_cross_engine_execution_total value={total}")
    return True


def cross_engine_warning_count() -> int:
    with _CROSS_ENGINE_LOCK:
        return _CROSS_ENGINE_TOTAL


def reset_cross_engine_for_tests() -> None:
    global _CROSS_ENGINE_TOTAL, _CROSS_ENGINE_SUPPRESSED
    with _CROSS_ENGINE_LOCK:
        _CROSS_ENGINE_SEEN.clear()
        _CROSS_ENGINE_TOTAL = 0
        _CROSS_ENGINE_SUPPRESSED = 0