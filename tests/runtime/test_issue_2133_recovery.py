"""Issue #2133：引擎恢复加回闭环 + spawn_failed 派单缺口 + 任务绑定对账。

覆盖：
1. 认领闸门显式挡 spawn_failed（即使 worker_version 非空）；
2. spawn_failed 时间窗：达告警阈值强告警、超第二阈值标记 degraded（不摘除）；
3. 连续 M 次探活 ok 后自动加回场景绑定 + 任务绑定对账（已取消回写终态、
   worker 从未收到的 dispatched 悬挂释放回 queued、busy_slots 收敛）；
4. 加回幂等（重复加回不重复绑定、不重复回写）；
5. #1982 复现链路回归：spawn_failed 期间不派单 → ok 恢复后自动出黑名单并加回；
6. QA 审查补充：悬挂释放守卫（task_messages 活性 + 宽限窗）、WARN hint 幂等拼接、
   readd 端点路由级测试（可见性 404 / 200 响应结构 / 幂等）。
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


def _insert_credential(scope: str = "user"):
    name = f"cred-2133-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, ?, 'route-user-2133', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", scope, "2026-11-12T00:00:00+00:00"))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, **cols):
    keys = ["credential_id", "name", "status", "healthy", "installed", "scope", "last_seen_at"]
    # scope 默认 user 级：readd/health-reset 口径下全局引擎不开放成员级手动操作
    vals = [cid, name, "online", healthy, 1, "user", "2026-11-12T00:00:00+00:00"]
    for k, v in cols.items():
        if k in keys:  # 同名列覆盖默认值（如 scope="global"）
            vals[keys.index(k)] = v
        else:
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


def _insert_task(rid, status, completed_at=None, started_at=None, created_at=None):
    tid = f"t_2133_{uuid.uuid4().hex[:10]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, agent_type, prompt, "
            "status, completed_at, started_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "sess-2133", rid, "coding", "prompt", status, completed_at, started_at,
             created_at or "2026-11-12T00:00:00+00:00"))
        db.commit()
    return tid


def _insert_task_message(tid, seq=1, created_at="2026-11-12T00:30:00+00:00"):
    """插入一条 task_messages（模拟 worker 已收到任务并产出消息）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (tid, seq, "text", "worker alive", created_at))
        db.commit()


def _cleanup(cid, scene_row_id=0, task_ids=()):
    with db_lock:
        c = db.cursor()
        for tid in task_ids:
            c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute("DELETE FROM grape_t_engine_lifecycle_audit WHERE runtime_id IN "
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
                "SELECT kind, detail FROM grape_t_engine_lifecycle_audit WHERE runtime_id=? "
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


def test_dangling_release_guarded_by_liveness_and_grace_window():
    """悬挂释放守卫（QA 审查意见，与 service.py #1071 P3 活性守卫口径对齐）。

    - worker 已产出消息（task_messages 有记录）的悬挂绑定不释放——worker 确已
      收到任务，仅 running 上报在途，释放会引发重复执行；
    - 悬挂未满宽限窗（created_at 距恢复时刻 < RECOVERY_RELEASE_GRACE_SECONDS）不释放
      ——引擎健康时手动一键加回撞上「派发→running 上报」正常间隙的误释放窗口；
    - 无消息且超宽限窗的真悬挂 → 释放回 queued。
    """
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    scene_row_id, scene_id = _insert_scene("coding", eids=[])
    tasks = []
    try:
        rid = _insert_runtime(cid, name, detached_from_scenes_at="2026-11-12T00:00:00+00:00")
        _insert_detach_log(scene_row_id, scene_id, "coding", rid, name)
        # 活性守卫命中：有 task_messages → 不释放
        alive = _insert_task(rid, "dispatched", started_at=None,
                             created_at="2026-11-12T00:00:00+00:00")
        _insert_task_message(alive)
        # 宽限窗命中：无消息但悬挂仅 7 分钟（< 默认 600s 宽限）→ 不释放
        fresh = _insert_task(rid, "dispatched", started_at=None,
                             created_at="2026-11-12T00:58:00+00:00")
        # 真悬挂：无消息且悬挂超宽限窗 → 释放
        stale = _insert_task(rid, "dispatched", started_at=None,
                             created_at="2026-11-12T00:00:00+00:00")
        tasks.extend([alive, fresh, stale])

        with patch.object(recovery, "RECOVER_OK_STREAK", 1):
            with db_lock:
                c = db.cursor()
                out = recovery.note_probe_ok(
                    c, rid, now_iso="2026-11-12T01:05:00+00:00")
                db.commit()
        assert out["recovered"] is True
        assert out["released"] == [stale], "仅无消息且超宽限窗的真悬挂才释放"
        assert _task(alive)[0] == "dispatched", "有 task_messages 活性的任务不得释放"
        assert _task(fresh)[0] == "dispatched", "宽限窗内的悬挂绑定不得释放"
        assert _task(stale)[0] == "queued"
    finally:
        _cleanup(cid, scene_row_id, tasks)


def test_spawn_fail_hint_append_is_idempotent():
    """WARN hint 走 _append_error（QA Nit 修复回归）。

    上一轮连败已把 hint 拼入 probe_error、时间窗标记被 ok 探活清零后，新连败
    观测（probe_error 为空 → base 取存量）不得重复拼入 hint。
    """
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, probe_spawn_fail_count=12,
                              spawn_fail_since="2026-11-12T00:00:00+00:00")
        stored = "OSError: [Errno 24] Too many open files" + recovery._WARN_HINT
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET probe_error=? WHERE id=?", (stored, rid))
            db.commit()
        with patch.object(recovery, "SPAWN_FAIL_WARN_STREAK", 10):
            with db_lock:
                c = db.cursor()
                recovery.note_spawn_failed(c, rid, probe_error="",
                                           now_iso="2026-11-12T00:20:00+00:00")
                db.commit()
            with db_lock:
                row = db.cursor().execute(
                    "SELECT probe_error FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert (row[0] or "").count("spawn_failed warning") == 1, \
            "已含 hint 的 probe_error 不得重复拼入 WARN hint"
    finally:
        _cleanup(cid)


def test_readd_endpoint_route_level():
    """readd 端点路由级测试（QA Optional）：可见性 404 / 200 响应结构 / 幂等重复点击。"""
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app
    cid = _insert_credential()
    name = f"engine-2133-{uuid.uuid4().hex[:8]}"
    scene_row_id, scene_id = _insert_scene("coding", eids=[])
    try:
        rid = _insert_runtime(cid, name, healthy=1,
                              detached_from_scenes_at="2026-11-12T00:00:00+00:00")
        log_id = _insert_detach_log(scene_row_id, scene_id, "coding", rid, name)
        USER = {"id": 0, "username": "route-user-2133", "org_id": 0, "org_role": "owner"}
        app.dependency_overrides[require_org_member] = lambda: USER
        try:
            with TestClient(app) as cli:
                # 可见性复用列表过滤：不存在/不可见 rid → 404
                r404 = cli.post("/api/settings/agent-runtimes/999999999/readd")
                assert r404.status_code == 404, r404.text
                # I-c（QA 第2轮）：全局引擎不开放成员级一键加回 → 403。
                # 引擎生效 scope 随凭据（cid!=0 时 rt["scope"]=cred.scope），
                # 故用 global 凭据建第二台引擎。
                gcid = _insert_credential(scope="global")
                grid = _insert_runtime(gcid, name + "-global", healthy=1,
                                       detached_from_scenes_at="2026-11-12T00:00:00+00:00")
                try:
                    r403 = cli.post(f"/api/settings/agent-runtimes/{grid}/readd")
                    assert r403.status_code == 403, r403.text
                finally:
                    with db_lock:
                        db.cursor().execute(
                            "DELETE FROM agent_runtimes WHERE id=?", (grid,))
                        db.cursor().execute(
                            "DELETE FROM runtime_credentials WHERE id=?", (gcid,))
                        db.commit()
                # 正常一键加回 → 200 + 响应结构
                r = cli.post(f"/api/settings/agent-runtimes/{rid}/readd")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["runtime_id"] == rid
                assert body["recovered"] is True
                assert body["bindings"] and body["bindings"][0]["rebound"] is True
                assert _detach_rebound(log_id), "路由触发的加回同样必须标记 rebound"
                # 幂等：重复点击仍 200，解绑审计已标记 rebound → 无重复绑定动作，
                # 场景节点引擎列表不重复追加
                r2 = cli.post(f"/api/settings/agent-runtimes/{rid}/readd")
                assert r2.status_code == 200, r2.text
                body2 = r2.json()
                assert body2["recovered"] is True
                nodes = _scene_nodes(scene_row_id)
                assert (nodes[0].get("engine_runtime_ids") or []).count(rid) == 1
        finally:
            app.dependency_overrides.pop(require_org_member, None)
    finally:
        _cleanup(cid, scene_row_id)