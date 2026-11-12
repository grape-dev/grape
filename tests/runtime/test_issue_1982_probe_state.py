"""Issue #1982：探活三态化与 fd 类 spawn_failed 不触发引擎摘除。

Issue #1982 复审补充：spawn_failed 独立计数（probe_spawn_fail_count）——
偶发资源紧张不摘除，但连续达到阈值（默认 50）后降级 healthy=0 并在
probe_error 附修复指引；ok 清零恢复，fail/model_unreachable 断链清零；
last_probe 重放幂等（不重复计数）。
"""
import errno
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_credential():
    name = f"cred-1982-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, probe_fail_count=0):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, scope, last_seen_at, "
            "probe_fail_count, probe_status, probe_last) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, ?, 'ok', '')",
            (cid, name, healthy, _now_iso(), probe_fail_count))
        rid = c.lastrowid
        db.commit()
    return rid


def _cleanup(cid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _state(name, cid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT healthy, probe_status, probe_fail_count, probe_spawn_fail_count, probe_error "
            "FROM agent_runtimes WHERE name=? AND credential_id=?", (name, cid)).fetchone()
    return tuple(row) if row else None


class _FakeRequest:
    headers = {}
    client = None


def _heartbeat(probe_results, runtime_ids, cid):
    from src.domains.runtime.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(runtime_ids=runtime_ids, probe_results=probe_results)
    return runtime_heartbeat(body, _FakeRequest(), credential_id=cid)


def _spawn_failed_obs(name, i):
    """一次独立的 spawn_failed 观测（last_probe 各不相同，不触发重放去重）。"""
    return {name: {"status": "spawn_failed", "latency": 0.1,
                   "last_probe": f"2026-11-12T00:{i // 60:02d}:{i % 60:02d}+00:00",
                   "error": "OSError: [Errno 24] Too many open files"}}


def test_spawn_failed_not_counted_as_consecutive_failure():
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=1)
        _heartbeat(_spawn_failed_obs(name, 0), [rid], cid)
        healthy, status, count, spawn_count, _err = _state(name, cid)
        assert status == "spawn_failed"
        assert count == 1, "spawn_failed 不计入探活连续失败（probe_fail_count 不动）"
        assert spawn_count == 1, "spawn_failed 独立计数 probe_spawn_fail_count 递增"
        assert healthy == 1, "首次 spawn_failed 不得触发引擎摘除"
    finally:
        _cleanup(cid)


def test_spawn_failed_below_threshold_keeps_healthy():
    """49 次（阈值-1）连续 spawn_failed：独立计数累加，healthy 仍为 1。"""
    from src.domains.runtime.routes.runtime import _PROBE_SPAWN_FAIL_TRIP
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        for i in range(_PROBE_SPAWN_FAIL_TRIP - 1):
            _heartbeat(_spawn_failed_obs(name, i), [rid], cid)
        healthy, status, count, spawn_count, err = _state(name, cid)
        assert status == "spawn_failed"
        assert spawn_count == _PROBE_SPAWN_FAIL_TRIP - 1
        assert count == 0, "spawn_failed 不计入 probe_fail_count"
        assert healthy == 1, "阈值内不得降级摘除"
        assert "persistent resource exhaustion" not in (err or ""), "阈值内不附降级指引"
    finally:
        _cleanup(cid)


def test_spawn_failed_trip_degrades_and_appends_fix_hint():
    """第 50 次（达到阈值）：healthy=0 且 probe_error 附修复指引。"""
    from src.domains.runtime.routes.runtime import _PROBE_SPAWN_FAIL_TRIP
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        for i in range(_PROBE_SPAWN_FAIL_TRIP):
            _heartbeat(_spawn_failed_obs(name, i), [rid], cid)
        healthy, status, count, spawn_count, err = _state(name, cid)
        assert status == "spawn_failed"
        assert spawn_count == _PROBE_SPAWN_FAIL_TRIP
        assert healthy == 0, "连续 spawn_failed 达阈值必须降级，不能永远 healthy 接单"
        assert "persistent resource exhaustion" in (err or ""), "probe_error 须附修复指引"
        assert "LimitNOFILE" in (err or "") or "ulimit" in (err or "")
    finally:
        _cleanup(cid)


def test_ok_resets_spawn_fail_count_and_restores_healthy():
    """阈值触发降级后一次 ok：计数清零、healthy 恢复 1。"""
    from src.domains.runtime.routes.runtime import _PROBE_SPAWN_FAIL_TRIP
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        for i in range(_PROBE_SPAWN_FAIL_TRIP):
            _heartbeat(_spawn_failed_obs(name, i), [rid], cid)
        assert _state(name, cid)[0] == 0
        _heartbeat({name: {"status": "ok", "latency": 1.0,
                           "last_probe": "2026-11-12T23:59:00+00:00", "error": ""}}, [rid], cid)
        healthy, status, count, spawn_count, err = _state(name, cid)
        assert status == "ok"
        assert healthy == 1, "ok 探活恢复 healthy"
        assert spawn_count == 0, "ok 探活清零 spawn_failed 独立计数"
        assert count == 0
        assert "persistent resource exhaustion" not in (err or ""), "恢复后不带降级指引"
    finally:
        _cleanup(cid)


def test_fail_breaks_spawn_failed_streak():
    """fail/model_unreachable 说明进程已成功 spawn → 独立计数断链清零。"""
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        for i in range(5):
            _heartbeat(_spawn_failed_obs(name, i), [rid], cid)
        assert _state(name, cid)[3] == 5
        _heartbeat({name: {"status": "fail", "latency": 2.0,
                           "last_probe": "2026-11-12T01:00:00+00:00",
                           "error": "ACP prompt no response"}}, [rid], cid)
        healthy, status, count, spawn_count, _err = _state(name, cid)
        assert status == "fail"
        assert count == 1, "fail 正常计入 probe_fail_count"
        assert spawn_count == 0, "fail 断开 spawn_failed 连败链"
    finally:
        _cleanup(cid)


def test_spawn_failed_replay_is_idempotent():
    """相同 last_probe 重放：只刷新 probe_* 字段，独立计数不虚增。"""
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        obs = _spawn_failed_obs(name, 0)
        _heartbeat(obs, [rid], cid)
        _heartbeat(obs, [rid], cid)
        _heartbeat(obs, [rid], cid)
        healthy, status, count, spawn_count, _err = _state(name, cid)
        assert status == "spawn_failed"
        assert spawn_count == 1, "last_probe 重放不得重复计数"
        assert healthy == 1
    finally:
        _cleanup(cid)


def test_model_unreachable_counted_and_degrades_on_second():
    cid = _insert_credential()
    name = f"engine-1982-{uuid.uuid4().hex[:8]}"
    try:
        rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
        _heartbeat({name: {"status": "model_unreachable", "latency": 15.0,
                           "last_probe": "2026-11-11T00:10:00+00:00",
                           "error": "no JSON-RPC response"}}, [rid], cid)
        assert _state(name, cid)[2] == 1
        _heartbeat({name: {"status": "model_unreachable", "latency": 15.0,
                           "last_probe": "2026-11-11T00:20:00+00:00",
                           "error": "no JSON-RPC response"}}, [rid], cid)
        healthy, status, count, _spawn_count, _err = _state(name, cid)
        assert status == "model_unreachable"
        assert count == 2
        assert healthy == 0, "模型不可达连续 2 次应正常降级"
    finally:
        _cleanup(cid)


def test_hermes_probe_emfile_classified_as_spawn_failed():
    from src.domains.runtime.engine import agent as ra
    exc = OSError(errno.EMFILE, "Too many open files")
    with patch("src.domains.runtime.engine.agent._subprocess.Popen", side_effect=exc):
        res = ra._probe_hermes_acp("/bin/hermes")
    assert res["status"] == "spawn_failed", res
    assert "Errno 24" in res["error"] or "Too many open files" in res["error"]
