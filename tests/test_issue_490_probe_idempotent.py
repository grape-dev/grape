"""Issue #490：Worker 探针观测重复计数修复的单测。

覆盖两块：
1. 服务端心跳幂等去重（src/api/routes/runtime.py）：
   - 同一失败观测（相同 last_probe）连续 3 次心跳：probe_fail_count 只 +1
   - 两条 last_probe 不同的失败观测：计数达 2、Runtime 标记不健康
   - 新成功观测：计数清零、恢复健康
   - 旧 Worker 不带 last_probe（回退 now）：每条当作新观测，递增（不回归）
2. Worker 启动时序（src/runtime/agent.py）：
   - _worker_probe_loop() 首轮先 sleep _PROBE_INTERVAL 再探测
   - 心跳成功后未变化的观测不再重发（指纹去重）
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_credential(name: str = ""):
    name = name or f"cred-490-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{name}", name, f"dev-{name}", _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, name: str = ""):
    name = name or f"engine-490-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, scope, last_seen_at, probe_fail_count, probe_last) "
            "VALUES (?, ?, 'online', 1, 'global', ?, 0, '')",
            (cid, name, _now_iso()))
        rid = c.lastrowid
        db.commit()
    return rid, name


def _cleanup(cid: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _probe_state(name: str, cid: int) -> tuple:
    """返回 (probe_fail_count, healthy, probe_last, probe_status)。"""
    with db_lock:
        row = db.cursor().execute(
            "SELECT probe_fail_count, healthy, probe_last, probe_status "
            "FROM agent_runtimes WHERE name=? AND credential_id=?",
            (name, cid)).fetchone()
    return tuple(row) if row else None


class _FakeRequest:
    headers = {}
    client = None


def _do_heartbeat(probe_results: dict, runtime_ids: list[int], cid: int):
    """直接调用 runtime_heartbeat（绕过鉴权，传 credential_id=cid）。"""
    from src.api.routes.runtime import runtime_heartbeat, HeartbeatBody
    body = HeartbeatBody(runtime_ids=runtime_ids, probe_results=probe_results)
    return runtime_heartbeat(body, _FakeRequest(), credential_id=cid)


# ============================ 服务端幂等去重 ============================

class TestServerProbeIdempotent:
    """服务端 heartbeat 按 last_probe 幂等去重。"""

    def test_same_fail_observation_three_heartbeats_only_plus_one(self):
        """验收 1：同一失败观测连续 3 次心跳 → probe_fail_count 只 +1。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            last_probe = "2026-10-28T19:00:00+00:00"
            fail = {"status": "fail", "latency": 5.0, "last_probe": last_probe, "error": "timeout"}
            for _ in range(3):
                _do_heartbeat({name: fail}, [rid], cid)
            cnt, healthy, _, status = _probe_state(name, cid)
            assert cnt == 1, f"同一观测重放 3 次应只 +1, got probe_fail_count={cnt}"
            assert healthy == 1, f"单次失败不应摘除 Runtime, got healthy={healthy}"
            assert status == "fail"
        finally:
            _cleanup(cid)

    def test_two_distinct_fail_observations_count_reaches_two_and_unhealthy(self):
        """验收 2：两条 last_probe 不同的失败观测 → 计数达 2、Runtime 不健康。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            _do_heartbeat({name: {"status": "fail", "latency": 5.0,
                                  "last_probe": "2026-10-28T19:00:00+00:00", "error": "t1"}}, [rid], cid)
            _do_heartbeat({name: {"status": "fail", "latency": 6.0,
                                  "last_probe": "2026-10-28T19:10:00+00:00", "error": "t2"}}, [rid], cid)
            cnt, healthy, _, status = _probe_state(name, cid)
            assert cnt == 2, f"两条不同失败观测应计数 2, got {cnt}"
            assert healthy == 0, f"连败 2 次应摘除 Runtime, got healthy={healthy}"
            assert status == "fail"
        finally:
            _cleanup(cid)

    def test_new_ok_observation_resets_count_and_restores_health(self):
        """验收 3：新成功观测 → 计数清零、恢复健康。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            # 先制造两次失败摘除
            _do_heartbeat({name: {"status": "fail", "latency": 5.0,
                                  "last_probe": "2026-10-28T19:00:00+00:00", "error": "t1"}}, [rid], cid)
            _do_heartbeat({name: {"status": "fail", "latency": 6.0,
                                  "last_probe": "2026-10-28T19:10:00+00:00", "error": "t2"}}, [rid], cid)
            assert _probe_state(name, cid)[1] == 0
            # 新成功观测（last_probe 不同）
            _do_heartbeat({name: {"status": "ok", "latency": 1.2,
                                  "last_probe": "2026-10-28T19:20:00+00:00", "error": ""}}, [rid], cid)
            cnt, healthy, _, status = _probe_state(name, cid)
            assert cnt == 0, f"新 ok 观测应清零计数, got {cnt}"
            assert healthy == 1, f"新 ok 观测应恢复健康, got healthy={healthy}"
            assert status == "ok"
        finally:
            _cleanup(cid)

    def test_replayed_ok_does_not_double_reset(self):
        """同一 ok 观测重放：probe_last 不变 → 幂等（仍 ok/0/healthy=1）。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            ok = {"status": "ok", "latency": 1.2, "last_probe": "2026-10-28T19:00:00+00:00", "error": ""}
            _do_heartbeat({name: ok}, [rid], cid)
            _do_heartbeat({name: ok}, [rid], cid)
            cnt, healthy, plast, status = _probe_state(name, cid)
            assert (cnt, healthy, status) == (0, 1, "ok")
            assert plast == "2026-10-28T19:00:00+00:00"
        finally:
            _cleanup(cid)

    def test_fail_then_replay_same_then_new_fail(self):
        """失败 → 重放同一失败（不增）→ 新失败（增至 2，摘除）。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            f1 = {"status": "fail", "latency": 5.0, "last_probe": "2026-10-28T19:00:00+00:00", "error": "t1"}
            _do_heartbeat({name: f1}, [rid], cid)
            _do_heartbeat({name: f1}, [rid], cid)  # 重放，不增
            _do_heartbeat({name: f1}, [rid], cid)  # 重放，不增
            assert _probe_state(name, cid) == (1, 1, "2026-10-28T19:00:00+00:00", "fail")
            f2 = {"status": "fail", "latency": 6.0, "last_probe": "2026-10-28T19:10:00+00:00", "error": "t2"}
            _do_heartbeat({name: f2}, [rid], cid)
            assert _probe_state(name, cid) == (2, 0, "2026-10-28T19:10:00+00:00", "fail")
        finally:
            _cleanup(cid)

    def test_legacy_worker_without_last_probe_still_increments(self):
        """旧 Worker 不带 last_probe → 回退为 now（每次不同）→ 走原递增逻辑（不回归）。"""
        cid = _insert_credential()
        rid, name = _insert_runtime(cid)
        try:
            # 不带 last_probe 字段
            _do_heartbeat({name: {"status": "fail", "latency": 5.0, "error": "t1"}}, [rid], cid)
            _do_heartbeat({name: {"status": "fail", "latency": 6.0, "error": "t2"}}, [rid], cid)
            cnt, healthy, _, status = _probe_state(name, cid)
            assert cnt == 2, f"旧 Worker 无 last_probe 应每条递增, got {cnt}"
            assert healthy == 0
            assert status == "fail"
        finally:
            _cleanup(cid)


# ============================ Worker 启动时序 ============================

class TestWorkerProbeLoopFirstRoundDelay:
    """_worker_probe_loop() 首轮应先 sleep _PROBE_INTERVAL 再探测。"""

    def test_first_round_sleeps_before_probing(self, monkeypatch):
        import src.runtime.agent as ag

        sleep_calls = []
        probe_calls = []

        # 记录 sleep 调用
        def _fake_sleep(secs):
            sleep_calls.append(secs)
            # 首轮 sleep 后立即抛 StopIteration 终止循环（只验证首轮顺序）
            raise KeyboardInterrupt("stop-after-first-sleep")

        # 替换探测函数为记录器
        def _fake_probe_once_in_loop():
            probe_calls.append(True)

        monkeypatch.setattr(ag._time_mod, "sleep", _fake_sleep)
        # _worker_probe_loop 内部调用 _discover_local_runtimes + _probe_engine_worker
        monkeypatch.setattr(ag, "_discover_local_runtimes", lambda: [{"name": "fake", "installed": True, "cli_path": ""}])
        monkeypatch.setattr(ag, "_probe_engine_worker", lambda name, cli: {"status": "ok", "latency": 0.1, "error": ""})

        try:
            ag._worker_probe_loop()
        except KeyboardInterrupt:
            pass

        # 首轮应先 sleep（sleep_calls 非空且第一个是 _PROBE_INTERVAL）
        assert sleep_calls, "首轮应先调用 sleep"
        assert sleep_calls[0] == ag._PROBE_INTERVAL, \
            f"首轮 sleep 应为 _PROBE_INTERVAL={ag._PROBE_INTERVAL}, got {sleep_calls[0]}"
        # sleep 发生在任何探测之前（probe_calls 在 sleep 之后才发生——此处因 KeyboardInterrupt 中断，probe_calls 应为空）
        assert probe_calls == [], "首轮 sleep 前不应执行探测"


class TestHeartbeatFingerprintDedup:
    """心跳成功后未变化的观测不再重发（指纹去重）。"""

    def test_unchanged_observation_not_resent_after_success(self, monkeypatch):
        import src.runtime.agent as ag

        # 预置探针结果
        with ag._worker_probe_lock:
            ag._worker_probe_results.clear()
            ag._worker_probe_results["eng-490"] = {
                "status": "fail", "latency": 5.0,
                "last_probe": "2026-10-28T19:00:00+00:00", "error": "t"}

        # 清空指纹缓存（隔离）
        monkeypatch.setattr(ag, "_last_sent_probe_fingerprint", {}, raising=False)

        # 第一次 collect：应包含该引擎（指纹为空，视为新）
        first = ag._collect_worker_probe_results()
        assert "eng-490" in first

        # 模拟心跳成功后写回指纹
        ag._mark_probe_fingerprint_sent(first)

        # 第二次 collect：观测未变化 → 不应包含该引擎
        second = ag._collect_worker_probe_results()
        assert "eng-490" not in second, "未变化的观测不应重发"

        # 观测变化（新 last_probe）后 → 应再次包含
        with ag._worker_probe_lock:
            ag._worker_probe_results["eng-490"]["last_probe"] = "2026-10-28T19:10:00+00:00"
        third = ag._collect_worker_probe_results()
        assert "eng-490" in third, "新观测应发送"
