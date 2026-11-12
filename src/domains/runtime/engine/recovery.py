"""Issue #2133：引擎恢复加回闭环 + spawn_failed 派单缺口 + 任务绑定对账。

在 PR !1202 建立的 spawn_failed 三态语义之上补齐闭环：

1. 认领闸门挡 spawn_failed：见 ``service._has_verified_executor``（状态驱动，
   探活恢复 ok 自动出黑名单）。
2. 时间窗兜底：连续 spawn_failed 达告警阈值（次数或时长）发强告警；超第二阈值
   标记 degraded（UI 高亮），**不自动摘除**（保留 PR !1202 不误摘除语义）。
3. 恢复加回：探活连续 M 次 ok + 心跳正常 → 自动按 ``scene_engine_detach_log``
   重建场景节点绑定并广播日志告知；前端一键加回走同一实现（manual）。
4. 加回时对账：已取消任务回写终态（completed_at + errors 留痕）；worker 从未
   收到的 dispatched 悬挂任务释放为 queued；busy_slots 收敛；全过程审计。

所有阈值走环境变量可调；所有写操作幂等（重复加回不重复加绑定/不重复回写）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from src.kernel.config import log
from src.kernel.session_manager import db, db_lock

# 强告警：连续 spawn_failed 次数或持续时长任一达到即告警（默认 10 次 / 10 分钟）
SPAWN_FAIL_WARN_STREAK = max(1, int(os.environ.get("RUNTIME_SPAWN_FAIL_WARN_STREAK", "10")))
SPAWN_FAIL_WARN_SECONDS = max(1, int(os.environ.get("RUNTIME_SPAWN_FAIL_WARN_SECONDS", "600")))
# degraded：持续 spawn_failed 超过该时长 → 标记 degraded（UI 高亮，不摘除）
SPAWN_FAIL_DEGRADE_SECONDS = max(1, int(os.environ.get("RUNTIME_SPAWN_FAIL_DEGRADE_SECONDS", "1800")))
# 恢复判定：连续探活 ok 次数
RECOVER_OK_STREAK = max(1, int(os.environ.get("RUNTIME_RECOVER_OK_STREAK", "3")))

_WARN_HINT = (
    " | spawn_failed warning: consecutive failures reached the alarm window; "
    "engine is quarantined from claiming and will retry claiming automatically after an ok probe"
)
_DEGRADE_HINT = (
    " | spawn_failed degraded: resource exhaustion persisted beyond the degrade window; "
    "engine kept disabled for claiming (not detached); fix fd/memory limits and it recovers on ok probe"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: str) -> float | None:
    from src.domains.runtime.engine.health import parse_dt
    return parse_dt(value)


def _audit(c, runtime_id: int, runtime_name: str, kind: str, detail: dict,
           scene_row_id: int = 0, node_tag: str = "") -> None:
    try:
        c.execute(
            "INSERT INTO engine_lifecycle_audit "
            "(runtime_id, runtime_name, kind, scene_row_id, node_tag, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (runtime_id, runtime_name or "", kind, scene_row_id or 0, node_tag or "",
             json.dumps(detail, ensure_ascii=False), _now_iso()))
    except Exception as e:
        log.warning(f"engine_lifecycle_audit insert failed (runtime={runtime_id}, kind={kind}): {e}")


def _append_error(errors: str, note: str) -> str:
    if note in (errors or ""):
        return errors or ""
    return (errors or "") + note


def note_spawn_failed(c, runtime_id: int, probe_error: str = "", now_iso: str = "") -> dict:
    """心跳 spawn_failed 分支的时间窗兜底（调用于同事务内）。

    维护 spawn_fail_since / spawn_fail_warned_at / spawn_fail_degraded_at：
    - 首见 spawn_failed 记 since；
    - 连续次数达 WARN_STREAK 或持续时长达 WARN_SECONDS → 首次强告警 + 审计；
    - 持续时长达 DEGRADE_SECONDS → 首次标记 degraded + 审计（不摘除引擎）。
    同时清零 probe_ok_streak（连败链开始）。
    """
    now_iso = now_iso or _now_iso()
    row = c.execute(
        "SELECT name, probe_spawn_fail_count, spawn_fail_since, spawn_fail_warned_at, "
        "spawn_fail_degraded_at, probe_error FROM agent_runtimes WHERE id=?",
        (runtime_id,)).fetchone()
    if not row:
        return {}
    name, count, since, warned_at, degraded_at, stored_err = row
    count = int(count or 0)
    since = since or now_iso
    since_ts = _ts(since)
    now_ts = _ts(now_iso) or since_ts
    elapsed = max(0.0, (now_ts - since_ts)) if since_ts is not None else 0.0
    base_err = probe_error or stored_err or ""

    result = {"runtime_id": runtime_id, "count": count, "elapsed_sec": int(elapsed),
              "warned": bool(warned_at), "degraded": bool(degraded_at)}

    if not warned_at and (count >= SPAWN_FAIL_WARN_STREAK or elapsed >= SPAWN_FAIL_WARN_SECONDS):
        warned_at = now_iso
        base_err = base_err + _WARN_HINT
        result["warned"] = True
        log.warning(
            f"ENGINE_SPAWN_FAIL_ALARM: runtime={runtime_id} engine={name} "
            f"count={count}/{SPAWN_FAIL_WARN_STREAK} elapsed={int(elapsed)}s/"
            f"{SPAWN_FAIL_WARN_SECONDS}s — quarantined from claiming, no dispatch until ok probe")
        _audit(c, runtime_id, name, "spawn_fail_alarm",
               {"count": count, "elapsed_sec": int(elapsed),
                "warn_streak": SPAWN_FAIL_WARN_STREAK, "warn_seconds": SPAWN_FAIL_WARN_SECONDS})

    if not degraded_at and elapsed >= SPAWN_FAIL_DEGRADE_SECONDS:
        degraded_at = now_iso
        base_err = base_err + _DEGRADE_HINT
        result["degraded"] = True
        log.warning(
            f"ENGINE_SPAWN_FAIL_DEGRADED: runtime={runtime_id} engine={name} "
            f"elapsed={int(elapsed)}s >= {SPAWN_FAIL_DEGRADE_SECONDS}s — marked degraded "
            f"(UI highlighted; engine NOT detached)")
        _audit(c, runtime_id, name, "spawn_fail_degraded",
               {"count": count, "elapsed_sec": int(elapsed),
                "degrade_seconds": SPAWN_FAIL_DEGRADE_SECONDS})

    c.execute(
        "UPDATE agent_runtimes SET spawn_fail_since=?, spawn_fail_warned_at=?, "
        "spawn_fail_degraded_at=?, probe_ok_streak=0, probe_error=? WHERE id=?",
        (since, warned_at or "", degraded_at or "", base_err, runtime_id))
    return result


def note_probe_break(c, runtime_id: int) -> None:
    """非 spawn_failed 观测（fail/model_unreachable/skip/pending）断开时间窗链路。"""
    try:
        c.execute(
            "UPDATE agent_runtimes SET spawn_fail_since='', spawn_fail_warned_at='', "
            "spawn_fail_degraded_at='', probe_ok_streak=0 WHERE id=?",
            (runtime_id,))
    except Exception as e:
        log.warning(f"note_probe_break failed (runtime={runtime_id}): {e}")


def note_probe_ok(c, runtime_id: int, now_iso: str = "") -> dict:
    """探活 ok：清零 spawn_failed 时间窗、累计连续 ok，达 M 次触发自动加回。

    恢复判定口径 = 连续 RECOVER_OK_STREAK 次探活 ok + 心跳正常（本函数由心跳调用，
    天然满足）；且此前确有告警/降级/解绑事实才触发，避免无意义加回。
    """
    now_iso = now_iso or _now_iso()
    row = c.execute(
        "SELECT name, probe_ok_streak, spawn_fail_warned_at, spawn_fail_degraded_at, "
        "detached_from_scenes_at FROM agent_runtimes WHERE id=?",
        (runtime_id,)).fetchone()
    if not row:
        return {}
    name, streak, warned_at, degraded_at, detached = row
    streak = int(streak or 0) + 1
    # 存量库可能未写回 detached_from_scenes_at，但解绑审计仍在 → 也算待恢复事实
    try:
        has_detach = c.execute(
            "SELECT 1 FROM scene_engine_detach_log WHERE runtime_id=? "
            "AND (rebound_at='' OR rebound_at IS NULL) LIMIT 1",
            (runtime_id,)).fetchone() is not None
    except Exception:
        has_detach = False
    was_active = bool(warned_at) or bool(degraded_at) or bool(detached) or has_detach
    c.execute(
        "UPDATE agent_runtimes SET probe_ok_streak=?, spawn_fail_since='', "
        "spawn_fail_warned_at='', spawn_fail_degraded_at='' WHERE id=?",
        (streak, runtime_id))
    out = {"runtime_id": runtime_id, "ok_streak": streak, "recovered": False}
    if was_active and streak >= RECOVER_OK_STREAK:
        # force=True：告警/降级/解绑事实已确认，确保恢复动作必留审计（幂等）
        out.update(_recover_in_tx(c, runtime_id, "auto", force=True, now_iso=now_iso))
    return out


def _readd_scene_bindings(c, runtime_id: int, now_iso: str) -> list:
    """按解绑审计记录重建场景节点绑定（幂等：已绑不打重，处理后标 rebound）。"""
    added = []
    try:
        rows = c.execute(
            "SELECT id, scene_row_id, node_tag FROM scene_engine_detach_log "
            "WHERE runtime_id=? AND (rebound_at='' OR rebound_at IS NULL)",
            (runtime_id,)).fetchall()
    except Exception as e:
        log.warning(f"_readd_scene_bindings: query detach log failed (runtime={runtime_id}): {e}")
        return added
    for log_id, scene_row_id, node_tag in rows:
        try:
            srow = c.execute(
                "SELECT nodes_json FROM devops_pipeline_scene WHERE id=?",
                (scene_row_id,)).fetchone()
            if not srow:
                c.execute("UPDATE scene_engine_detach_log SET rebound_at=? WHERE id=?",
                          (now_iso, log_id))
                continue
            nodes = json.loads(srow[0] or "[]") if srow[0] else []
            if not isinstance(nodes, list):
                nodes = []
            matched = False
            for node in nodes:
                tag = node.get("tag") or node.get("id") or ""
                if tag != (node_tag or ""):
                    continue
                matched = True
                eids = node.get("engine_runtime_ids") or []
                if runtime_id not in eids:
                    eids.append(runtime_id)
                    node["engine_runtime_ids"] = eids
                    c.execute(
                        "UPDATE devops_pipeline_scene SET nodes_json=?, updated_at=? WHERE id=?",
                        (json.dumps(nodes, ensure_ascii=False), now_iso, scene_row_id))
                    added.append({"scene_row_id": scene_row_id, "node_tag": node_tag,
                                  "rebound": True})
                else:
                    added.append({"scene_row_id": scene_row_id, "node_tag": node_tag,
                                  "rebound": False, "reason": "already_bound"})
                break
            if not matched:
                log.warning(f"_readd_scene_bindings: node_tag={node_tag} not found in scene {scene_row_id}")
            c.execute("UPDATE scene_engine_detach_log SET rebound_at=? WHERE id=?",
                      (now_iso, log_id))
        except Exception as e:
            log.warning(f"_readd_scene_bindings: scene={scene_row_id} failed: {e}")
    return added


def _reconcile_task_bindings(c, runtime_id: int, now_iso: str) -> dict:
    """加回时任务绑定对账（幂等）。

    - 已取消且未落终态时刻的任务 → 回写 completed_at + errors 审计留痕；
    - dispatched 但 started_at 为空（worker 从未收到的悬挂绑定）→ 释放回 queued 重派；
    - busy_slots 按真实 dispatched/running 收敛。
    """
    result = {"terminalized": [], "released": [], "busy_slots": 0}
    try:
        rows = c.execute(
            "SELECT id, errors FROM agent_tasks WHERE runtime_id=? AND status='cancelled' "
            "AND (completed_at IS NULL OR completed_at='')",
            (runtime_id,)).fetchall()
        for tid, errors in rows:
            note = (f" | reconciled by engine re-add at {now_iso}: cancelled task terminalized")
            c.execute(
                "UPDATE agent_tasks SET completed_at=?, errors=? WHERE id=? "
                "AND (completed_at IS NULL OR completed_at='')",
                (now_iso, _append_error(errors, note), tid))
            if c.rowcount:
                result["terminalized"].append(tid)
    except Exception as e:
        log.warning(f"_reconcile_task_bindings: terminalize failed (runtime={runtime_id}): {e}")

    try:
        rows = c.execute(
            "SELECT id FROM agent_tasks WHERE runtime_id=? AND status='dispatched' "
            "AND (started_at IS NULL OR started_at='')",
            (runtime_id,)).fetchall()
        for (tid,) in rows:
            c.execute(
                "UPDATE agent_tasks SET status='queued', runtime_id=0, "
                "errors=? WHERE id=? AND status='dispatched'",
                (_append_error("", f" | released dangling binding by engine re-add at {now_iso}"), tid))
            if c.rowcount:
                result["released"].append(tid)
    except Exception as e:
        log.warning(f"_reconcile_task_bindings: release failed (runtime={runtime_id}): {e}")

    try:
        crow = c.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? "
            "AND status IN ('dispatched','running')", (runtime_id,)).fetchone()
        result["busy_slots"] = int((crow[0] if crow else 0) or 0)
        c.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?",
                  (result["busy_slots"], runtime_id))
    except Exception as e:
        log.warning(f"_reconcile_task_bindings: busy_slots reconcile failed (runtime={runtime_id}): {e}")
    return result


def _recover_in_tx(c, runtime_id: int, trigger: str, force: bool, now_iso: str = "") -> dict:
    now_iso = now_iso or _now_iso()
    row = c.execute(
        "SELECT name, detached_from_scenes_at, spawn_fail_degraded_at FROM agent_runtimes WHERE id=?",
        (runtime_id,)).fetchone()
    if not row:
        return {"recovered": False, "reason": "runtime_not_found"}
    name, detached, degraded = row
    bindings = _readd_scene_bindings(c, runtime_id, now_iso)
    reconciled = _reconcile_task_bindings(c, runtime_id, now_iso)
    c.execute(
        "UPDATE agent_runtimes SET detached_from_scenes_at='', spawn_fail_degraded_at='', "
        "spawn_fail_since='', spawn_fail_warned_at='' WHERE id=?",
        (runtime_id,))
    detail = {"trigger": trigger, "bindings": bindings,
              "terminalized": reconciled["terminalized"],
              "released": reconciled["released"], "busy_slots": reconciled["busy_slots"]}
    had_work = bool(bindings) or bool(detached) or bool(degraded) or force
    if had_work:
        _audit(c, runtime_id, name, "manual_readd" if trigger == "manual" else "recovered", detail)
        log.info(
            f"Engine recovery ({trigger}): runtime={runtime_id} engine={name} "
            f"bindings={len(bindings)} terminalized={len(reconciled['terminalized'])} "
            f"released={len(reconciled['released'])}")
    return {"recovered": True, "trigger": trigger, "bindings": bindings,
            "terminalized": reconciled["terminalized"],
            "released": reconciled["released"],
            "busy_slots": reconciled["busy_slots"]}


def recover_engine(runtime_id: int, trigger: str = "manual", force: bool = False) -> dict:
    """加回引擎场景绑定 + 任务绑定对账（供一键兜底端点调用，独立事务）。"""
    with db_lock:
        c = db.cursor()
        try:
            out = _recover_in_tx(c, runtime_id, trigger, force)
            db.commit()
            return out
        except Exception as e:
            log.warning(f"recover_engine failed (runtime={runtime_id}, trigger={trigger}): {e}")
            try:
                db.rollback()
            except Exception:
                pass
            return {"recovered": False, "reason": str(e)}