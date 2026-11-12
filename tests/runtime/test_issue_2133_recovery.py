"""Issue #2133：引擎恢复加回闭环 + spawn_failed 派单缺口 + 任务绑定对账。

覆盖：
1. 认领闸门显式挡 spawn_failed（即使 worker_version 非空）；
2. spawn_failed 时间窗：达告警阈值强告警、超第二阈值标记 degraded（不摘除）；
3. 连续 M 次探活 ok 后自动加回场景绑定 + 任务绑定对账（已取消回写终态、
   worker 从未收到的 dispatched 悬挂释放回 queued、busy_slots 收敛）；
4. 加回幂等（重复加回不重复绑定、不重复回写）；
5. #1982 复现链路回归：spawn_failed 期间不派单 → ok 恢复后自动出黑名单并加回。
"""
import os
import sys
import uuid
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock  # noqa: E402
from src.domains.runtime.engine import recovery  # noqa: E402


def _insert_credential():
    name = f"cred-2133-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", "2026-11-12T00:00:00+00:00"))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, **cols):
    keys = ["credential_id", "name", "status", "healthy", "installed", "scope", "last_seen_at"]
    vals = [cid, name, "online", healthy, 1, "global", "2026-11-12T00:00:00+00:00"]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_runtimes ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            vals)
        rid = c.lastrowid
        db.commit()
    return rid


def _insert_scene(runtime_node_tag="coding", eids=None):
    scene_id = f"scene-2133-{uuid.uuid4().hex[:8]}"
    import json
    nodes = [{"tag": runtime_node_tag, "engine_runtime_ids": eids or []}]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (scene_id, "scene-2133", "gitcode:o/r", json.dumps(nodes)))
        row_id = c.lastrowid
        db.commit()
    return row_id, scene_id


def _insert_detach_log(scene_row_id, scene_id, tag, rid, name):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_engine_detach_log (scene_row_id, repo_key, scene_id, node_tag, "
            "runtime_id, runtime_name, detached_at, reason) VALUES (?,?,?,?,?,?,?,?)",
            (scene_row_id, "gitcode:o/r", scene_id, tag, rid, name,
             "2026-11-12T00:00:00+00:00", "offline_timeout"))
        log_id = c.lastrowid
        db.commit()
    return log_id


def _insert_task(rid, status, completed_at=None, started_at=None):
    tid = f"t_2133_{uuid.uuid4().hex[:10]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, agent_type, prompt, "
            "status, completed_at, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, "sess-2133", rid, "coding", "prompt", status, completed_at, started_at))
        db.commit()
    return tid


def _cleanup(cid, scene_row_id=0, task_ids=()):
    with db_lock:
        c = db.cursor()
        for tid in task_ids:
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute("DELETE FROM engine_lifecycle_audit WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM scene_engine_detach_log WHERE scene_row_id=?", (scene_row_id,))
        if scene_row_id:
            c.execute("DELETE FROM devops_pipeline_scene WHERE id=?", (scene_row_id,))
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _scene_nodes(scene_row_id):
    import json
    with db_lock:
        row = db.cursor().execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE id=?", (scene_row_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def _detach_rebound(log_id):
    with db_lock:
        row = db.cursor().execute(
            "SELECT rebound_at FROM scene_engine_detach_log WHERE id=?", (log_id,)).fetchone()
    return row[0] if row else ""


def _task(tid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT status, runtime_id, completed_at, errors FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()
    return row


def test_claim_gate_blocks_spawn_failed_even_with_worker_version():
    from src.domains.runtime.engine.service import _has_verified_executor
    assert _has_verified_executor("spawn_failed", "2.0.0", credential_id=9,
                                  installed=True, healthy=True) is False
    assert _has_verified_executor("spawn_failed", "", credential_id=0,
                                  installed=True, healthy=True) is False
    # 未回归：pending 有 worker_version 仍可认领；fail 仍被挡
    assert _has_verified_executor("pending", "2.0.0", credential_id=9) is True
    assert _has_verified_executor("failed", "", credential_id=9) is False


def test_spawn_failed_window_alarm_then_degrade_without_detach():
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_spawn_fail_count=3,
                              spawn_fail_since="2026-11-12T00:00:00+00:00")
        with patch.object(recovery, "SPAWN_FAIL_WARN_SECONDS", 600), \
             patch.object(recovery, "SPAWN_FAIL_DEGRADE_SECONDS", 1800), \
             patch.object(recovery, "SPAWN_FAIL_WARN_STREAK", 10):
            with db_lock:
                c = db.cursor()
                out = recovery.note_spawn_failed(
                    c, rid, now_iso="2026-11-12T00:20:00+00:00")
                db.commit()
            assert out["warned"] is True
            assert out["degraded"] is False
            with db_lock:
                row = db.cursor().execute(
                    "SELECT spawn_fail_warned_at, spawn_fail_degraded_at, probe_error, healthy "
                    "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
            assert row[0], "达告警窗口后必须记录强告警时间"
            assert not row[1], "未超第二阈值不得标记 degraded"
            assert "spawn_failed warning" in (row[2] or "")
            assert row[3] == 1, "时间窗告警阶段不自动摘除引擎"

            with db_lock:
                c = db.cursor()
                out2 = recovery.note_spawn_failed(
                    c, rid, now_iso="2026-11-12T00:35:00+00:00")
                db.commit()
            assert out2["degraded"] is True
            with db_lock:
                row = db.cursor().execute(
                    "SELECT spawn_fail_degraded_at, probe_error, healthy "
                    "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
            assert row[0], "持续超第二阈值必须标记 degraded"
            assert "spawn_failed degraded" in (row[1] or "")
            assert row[2] == 1, "degraded 标记不等于摘除（保留 healthy，UI 高亮）"
    finally:
        _cleanup(cid)


def test_ok_streak_auto_readds_scene_and_reconciles_tasks_idempotently():
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    scene_row_id, scene_id = _insert_scene("coding", eids=[])
    tasks = []
    try:
        rid = _insert_runtime(cid, name, healthy=0, detached_from_scenes_at="2026-11-12T00:00:00+00:00",
                              spawn_fail_degraded_at="2026-11-12T00:10:00+00:00",
                              busy_slots=9)
        log_id = _insert_detach_log(scene_row_id, scene_id, "coding", rid, name)
        cancelled = _insert_task(rid, "cancelled")
        dangling = _insert_task(rid, "dispatched", started_at=None)
        tasks.extend([cancelled, dangling])

        with patch.object(recovery, "RECOVER_OK_STREAK", 3):
            for i in range(3):
                with db_lock:
                    c = db.cursor()
                    out = recovery.note_probe_ok(
                        c, rid, now_iso=f"2026-11-12T01:0{i}:00+00:00")
                    db.commit()
                if i < 2:
                    assert out["recovered"] is False, "未达连续 M 次 ok 不得加回"
        assert out["recovered"] is True

        # 场景绑定重建
        nodes = _scene_nodes(scene_row_id)
        assert rid in (nodes[0].get("engine_runtime_ids") or [])
        assert _detach_rebound(log_id), "加回后解绑审计必须标记 rebound（幂等）"

        # 已取消任务回写终态
        st, _rtid, completed, errors = _task(cancelled)
        assert st == "cancelled"
        assert completed, "已取消任务必须回写 completed_at"
        assert "reconciled by engine re-add" in (errors or "")

        # worker 从未收到的 dispatched 悬挂绑定释放回 queued
        st2, rtid2, _c2, errors2 = _task(dangling)
        assert st2 == "queued"
        assert rtid2 == 0
        assert "released dangling binding" in (errors2 or "")

        # degraded 标记清除
        with db_lock:
            row = db.cursor().execute(
                "SELECT spawn_fail_degraded_at, detached_from_scenes_at, busy_slots "
                "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert not row[0] and not row[1]
        assert row[2] == 0, "busy_slots 必须按真实 active 收敛"

        audit = None
        with db_lock:
            audit = db.cursor().execute(
                "SELECT kind, detail FROM engine_lifecycle_audit WHERE runtime_id=? "
                "AND kind='recovered' ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        assert audit is not None, "加回必须写审计记录"

        # 幂等：再次一键加回不重复加绑定、不重复回写
        completed_before = _task(cancelled)[2]
        out2 = recovery.recover_engine(rid, trigger="manual", force=True)
        assert out2["recovered"] is True
        nodes2 = _scene_nodes(scene_row_id)
        assert (nodes2[0].get("engine_runtime_ids") or []).count(rid) == 1
        assert _task(cancelled)[2] == completed_before, "重复加回不得改写已回写的终态时刻"
        assert len(out2["terminalized"]) == 0, "已回写任务不得重复计入"
    finally:
        _cleanup(cid, scene_row_id, tasks)


def test_1982_reproduction_spawn_failed_blocks_then_recovers_on_ok():
    """#1982 复现链路：spawn_failed 期间引擎不派单；ok 恢复后出黑名单并完成加回。"""
    from src.domains.runtime.routes.runtime import runtime_heartbeat, HeartbeatBody
    from src.domains.runtime.engine.service import _has_verified_executor
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    scene_row_id, scene_id = _insert_scene("coding", eids=[])
    try:
        rid = _insert_runtime(cid, name, healthy=1, worker_version="2.0.0",
                              probe_status="ok")
        _insert_detach_log(scene_row_id, scene_id, "coding", rid, name)

        class _Req:
            headers = {}
            client = None

        # spawn_failed：healthy 保持 1（不摘除），但认领闸门必须挡住
        body = HeartbeatBody(runtime_ids=[rid], probe_results={
            name: {"status": "spawn_failed", "latency": 0.1,
                   "last_probe": "2026-11-12T00:00:00+00:00",
                   "error": "OSError: [Errno 24] Too many open files"}})
        runtime_heartbeat(body, _Req(), credential_id=cid)
        assert _has_verified_executor("spawn_failed", "2.0.0", credential_id=cid,
                                      installed=True, healthy=True) is False

        # 连续 3 次 ok → 自动出黑名单 + 自动加回
        with patch.object(recovery, "RECOVER_OK_STREAK", 3):
            for i in range(3):
                body = HeartbeatBody(runtime_ids=[rid], probe_results={
                    name: {"status": "ok", "latency": 1.0,
                           "last_probe": f"2026-11-12T01:0{i}:00+00:00", "error": ""}})
                runtime_heartbeat(body, _Req(), credential_id=cid)

        with db_lock:
            probe_status, streak = db.cursor().execute(
                "SELECT probe_status, probe_ok_streak FROM agent_runtimes WHERE id=?",
                (rid,)).fetchone()
        assert probe_status == "ok"
        assert _has_verified_executor(probe_status, "2.0.0", credential_id=cid,
                                      installed=True, healthy=True) is True
        nodes = _scene_nodes(scene_row_id)
        assert rid in (nodes[0].get("engine_runtime_ids") or []), "恢复后必须自动加回场景"
    finally:
        _cleanup(cid, scene_row_id)