"""Issue #4346：session/new 超时分类 + 换引擎重试 + session 层熔断 + provider 校验。

覆盖验收项：
- 超时可区分（ENGINE_UNREACHABLE / SESSION_CREATE_BLOCKED / ENGINE_MISCONFIGURED）；
- SESSION_CREATE_BLOCKED 事件/任务错误条目含 engine_id/queue_depth/disk_avail_bytes；
- 超时自动换引擎重试 ≥1 次（requeue 归池，故障引擎被 session 冷却挡在认领闸门外）；
- 连续 N 次超时熔断 + 调度候选剔除 + 冷却后半开单次探测 + 成功恢复；
- 健康分可读 session 层失败计数与熔断状态；
- 无 LLM provider 引擎注册期被拦截（llm_provider_ok=0 不进池）；
- #423 解析侧 10 行上限不得被重新引入。
"""
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import (
    backend_health, health_scorer, provider_check, service, session_guard,
)

SESSION_TIMEOUT_TEXT = (
    "engine exit_code=-1, errors: session/new failed: session/new timeout after 100s "
    "[SESSION_CREATE_BLOCKED]"
)


# ── DB helpers（同 tests/runtime/test_issue_3928_backend_failover.py 口径）──

def _insert_credential():
    name = f"cred-4346-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, "
            "owner_name, status, last_seen_at) VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"h_{name}", name, f"dev-{name}", datetime.now(timezone.utc).isoformat()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid, name, healthy=1, tag="all", status="online", probe_status="ok",
                    worker_version="w-4346", backend_status="available", installed=1):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, "
            "scope, last_seen_at, probe_status, worker_version, backend_status) "
            "VALUES (?, ?, ?, ?, ?, 'global', ?, ?, ?, ?)",
            (cid, name, status, healthy, installed, now, probe_status, worker_version,
             backend_status))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (?, ?, 5, 50, 1)", (rid, tag))
        db.commit()
    return rid


def _insert_task(runtime_id, status="running", attempts=0, repo="issue4346", tag="all",
                 session_retry_count=None):
    tid = f"t_4346_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    cols = ("id, agent_session_id, runtime_id, repo_key, agent_type, skill, prompt, status, "
            "pool_tag, pool_agent_type, attempts, started_at, created_at")
    vals = [tid, f"sess-{tid}", runtime_id, f"gitcode:{repo}/repo", "qa", "qa", "p",
            status, tag, "qa", attempts, now, now]
    if session_retry_count is not None:
        cols += ", session_retry_count"
        vals.append(session_retry_count)
    with db_lock:
        c = db.cursor()
        c.execute(f"INSERT INTO agent_tasks ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
        db.commit()
    return tid


def _cleanup(cid, repo="issue4346"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (f"gitcode:{repo}/repo",))
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM runtime_health_scores WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        c.execute("DELETE FROM grape_t_engine_lifecycle_audit WHERE runtime_name LIKE ?",
                  (f"%4346%",))
        db.commit()


@contextlib.contextmanager
def _pool_isolated():
    """临时禁用仓内其它引擎的 ``tag='all'`` 能力，让池判定只看到本用例的引擎。

    ``has_other_session_engine`` 走真实 SQL（``ec.tag=? OR ec.tag='all'``），而测试库是
    进程共享的：别的用例遗留的 ``tag='all'`` 引擎会串进来。这里只隔离**数据**，不替换
    被测函数（复审要求：不得 monkeypatch 绕过 SQL 口径）。退出时无条件恢复。
    """
    disabled: list = []
    with db_lock:
        c = db.cursor()
        disabled = [r[0] for r in c.execute(
            "SELECT id FROM engine_capabilities WHERE tag='all' AND enabled=1").fetchall()]
        if disabled:
            c.execute(f"UPDATE engine_capabilities SET enabled=0 WHERE id IN "
                      f"({','.join('?' * len(disabled))})", disabled)
        db.commit()
    try:
        yield
    finally:
        if disabled:
            with db_lock:
                c = db.cursor()
                c.execute(f"UPDATE engine_capabilities SET enabled=1 WHERE id IN "
                          f"({','.join('?' * len(disabled))})", disabled)
                db.commit()


def _expire_session_window(rid):
    """把 session 熔断窗口拨到过去（模拟「窗口已过期 → 进入半开」）。"""
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET session_circuit_until=? WHERE id=?", (past, rid))
        db.commit()


def _runtime_session_row(rid):
    with db_lock:
        return db.cursor().execute(
            "SELECT healthy, COALESCE(session_fail_streak,0), COALESCE(session_fail_total,0), "
            "COALESCE(session_circuit_until,''), COALESCE(session_circuit_half_open,0), "
            "COALESCE(session_last_error_code,''), COALESCE(llm_provider_ok,1) "
            "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()


def _task_row(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, runtime_id, attempts, errors FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()


# ── 1. 分类 ──

class TestSessionFailureClassification:
    def test_three_codes_are_distinguishable(self):
        blocked = session_guard.classify_session_failure(SESSION_TIMEOUT_TEXT)
        assert blocked and blocked["error_code"] == session_guard.SESSION_CREATE_BLOCKED
        assert blocked["stage"] == "session/new"

        unreachable = session_guard.classify_session_failure(
            "engine exit_code=-1, errors: session/new failed: write session/new: broken pipe")
        assert unreachable["error_code"] == session_guard.ENGINE_UNREACHABLE

        init_fail = session_guard.classify_session_failure(
            "engine exit_code=-1, errors: initialize failed: initialize timeout after 100s")
        assert init_fail["error_code"] == session_guard.ENGINE_UNREACHABLE
        assert init_fail["stage"] == "initialize"

        misconfig = session_guard.classify_session_failure(
            "engine exit_code=1, errors: ACP error 500: No LLM provider configured")
        assert misconfig["error_code"] == session_guard.ENGINE_MISCONFIGURED

    def test_unrelated_failure_is_not_session(self):
        assert session_guard.classify_session_failure("tests failed: 2 assertions") is None
        assert session_guard.classify_session_failure("") is None
        assert session_guard.classify_session_failure("[skill_mismatch] sources disagree") is None

    def test_format_session_error_marks_code(self):
        text = session_guard.format_session_error("session/new", "session/new timeout after 100s")
        assert text.startswith("session/new failed:")
        assert "[SESSION_CREATE_BLOCKED]" in text
        assert session_guard.classify_session_failure(text)["error_code"] == \
            session_guard.SESSION_CREATE_BLOCKED


# ── 2. 观测 payload 字段恒存在 ──

class TestFailurePayload:
    def test_fields_always_present_even_if_probe_fails(self, monkeypatch):
        monkeypatch.setattr(session_guard, "probe_queue_depth", lambda rid: None)
        monkeypatch.setattr(session_guard, "probe_disk_avail_bytes", lambda path="/": None)
        p = session_guard.failure_payload(
            7, "hermes-cred0",
            {"error_code": session_guard.SESSION_CREATE_BLOCKED, "stage": "session/new",
             "reason": "timeout"})
        assert p["error_code"] == session_guard.SESSION_CREATE_BLOCKED
        assert p["engine_id"] == 7
        assert "queue_depth" in p and p["queue_depth"] is None
        assert "disk_avail_bytes" in p and p["disk_avail_bytes"] is None

    def test_disk_probe_real_value(self):
        v = session_guard.probe_disk_avail_bytes()
        assert v is None or (isinstance(v, int) and v >= 0)


# ── 3. session 熔断 / 半开 / 恢复 ──

class TestSessionCircuit:
    def test_streak_threshold_opens_circuit_and_blocks_claim(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "3")
        monkeypatch.setenv("RUNTIME_SESSION_CIRCUIT_SECONDS", "600")
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-a")
        try:
            # 前两次只开短冷却：不进熔断窗口，但认领已被挡（保证换引擎）
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            assert session_guard.session_claim_allowed(rid) is False
            assert session_guard.session_circuit_open(rid) is False
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            assert session_guard.session_circuit_open(rid) is False
            # 第 3 次 → 熔断
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            assert session_guard.session_circuit_open(rid) is True
            assert session_guard.session_claim_allowed(rid) is False
            # 既有 backend 认领闸门同尺（派发/认领口径一致）
            assert backend_health.claim_allowed(rid) is False
            healthy, streak, total, until, half, code, _llm = _runtime_session_row(rid)
            assert streak == 3 and total == 3 and until and code == session_guard.SESSION_CREATE_BLOCKED
            assert healthy == 1, "session 熔断不改写任务层 healthy（ENGINE_OFFLINE 语义独立）"
        finally:
            _cleanup(cid)

    def test_half_open_single_probe_then_recovery(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-b")
        try:
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            assert session_guard.session_claim_allowed(rid) is False
            # 熔断窗口结束（时钟前移）
            _expire_session_window(rid)
            assert session_guard.session_claim_allowed(rid) is True
            # 真实认领顺序：任务先绑定到引擎（dispatched/running），再置半开占位
            tid = _insert_task(rid, status="running")
            with db_lock:
                c = db.cursor()
                session_guard.note_session_probe_started(c, rid)
                db.commit()
            assert session_guard.session_claim_allowed(rid) is False, "半开只放行一次探测"
            service.update_task_status(tid, "completed")
            assert session_guard.session_claim_allowed(rid) is True
            _healthy, streak, _total, until, half, _code, _llm = _runtime_session_row(rid)
            assert streak == 0 and until == "" and half == 0
        finally:
            _cleanup(cid)

    # ── Issue #4346 复审 Critical：半开占位必须有释放路径 ──

    def test_half_open_released_when_probe_task_cancelled(self, monkeypatch):
        """半开探测以「任务取消」收尾 → 占位必须释放，引擎恢复可认领。"""
        rid, tid = self._probe_in_flight(monkeypatch, "hermes-4346-r1")
        try:
            assert session_guard.session_claim_allowed(rid) is False
            service.cancel_task(tid)
            _healthy, _streak, _total, _until, half, _code, _llm = _runtime_session_row(rid)
            assert half == 0, "取消收尾后半开占位必须释放（否则永久挡门）"
            assert session_guard.session_claim_allowed(rid) is True
            assert session_guard.session_state(rid)["session_circuit_half_open"] is False
        finally:
            _cleanup(self._cid)

    def test_half_open_released_on_non_session_business_failure(self, monkeypatch):
        """半开探测以 exit!=0 的业务失败（非 session 类）收尾 → 同样必须释放。"""
        rid, tid = self._probe_in_flight(monkeypatch, "hermes-4346-r2")
        try:
            assert session_guard.session_claim_allowed(rid) is False
            service.update_task_status(tid, "failed",
                                       errors=json.dumps([{"error_code": "EXIT_NONZERO"}]))
            _healthy, _streak, _total, _until, half, _code, _llm = _runtime_session_row(rid)
            assert half == 0, "非 session 失败收尾后半开占位不得残留"
            assert session_guard.session_claim_allowed(rid) is True
            # 闸门与看板口径必须一致：显示未熔断 ⇒ 必须真的可认领
            state = session_guard.session_state(rid)
            assert state["session_circuit_open"] is False
            assert state["session_circuit_half_open"] is False
        finally:
            _cleanup(self._cid)

    def test_half_open_kept_while_probe_still_running(self, monkeypatch):
        """探测确实在飞（引擎上有 running 任务）→ 维持「只放行一次」语义。"""
        rid, tid = self._probe_in_flight(monkeypatch, "hermes-4346-r3")
        try:
            assert session_guard.session_claim_allowed(rid) is False
            _healthy, _streak, _total, _until, half, _code, _llm = _runtime_session_row(rid)
            assert half == 1
        finally:
            _cleanup(self._cid)

    def test_orphaned_half_open_self_heals_without_active_task(self, monkeypatch):
        """占位残留但引擎上已无在飞任务（如被 stale/requeue 收回）→ 闸门自动释放。"""
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-r4")
        try:
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            _expire_session_window(rid)
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE agent_runtimes SET session_circuit_half_open=1 WHERE id=?",
                          (rid,))
                db.commit()
            assert session_guard.session_claim_allowed(rid) is True
            _healthy, _streak, _total, _until, half, _code, _llm = _runtime_session_row(rid)
            assert half == 0
        finally:
            _cleanup(cid)

    def _probe_in_flight(self, monkeypatch, name):
        """构造「熔断窗口已过 + 已认领探测任务 + 半开占位在位」的真实状态。"""
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
        self._cid = _insert_credential()
        rid = _insert_runtime(self._cid, name)
        session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
        _expire_session_window(rid)
        assert session_guard.session_claim_allowed(rid) is True
        tid = _insert_task(rid, status="running")
        with db_lock:
            c = db.cursor()
            session_guard.note_session_probe_started(c, rid)
            db.commit()
        return rid, tid

    def test_dispatch_candidate_map_excludes_circuit_open_engine(self, monkeypatch):
        from src.domains.runtime.engine.engine_select import _backend_claimable_map
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
        cid = _insert_credential()
        bad = _insert_runtime(cid, "hermes-4346-c")
        good = _insert_runtime(cid, "hermes-4346-d")
        try:
            session_guard.note_session_failure(bad, session_guard.SESSION_CREATE_BLOCKED)
            claimable = _backend_claimable_map([bad, good])
            assert claimable is not None
            assert claimable[bad] is False
            assert claimable[good] is True
        finally:
            _cleanup(cid)

    def test_health_score_exposes_session_metrics(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-e")
        try:
            session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
            data = health_scorer.get_health_score(rid, force_refresh=True)
            assert data["session_timeout_streak"] == 1
            assert data["session_failure_total"] == 1
            assert data["session_circuit_open"] is True
            assert data["session_last_error_code"] == session_guard.SESSION_CREATE_BLOCKED
            assert data["llm_provider_ok"] is True
        finally:
            _cleanup(cid)


# ── 4. 换引擎重试 / 判死回落 ──

class TestSessionRetry:
    def test_timeout_requeues_to_another_engine(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_MAX_RETRY", "1")
        monkeypatch.setenv("RUNTIME_SESSION_RETRY_COOLDOWN", "60")
        cid = _insert_credential()
        bad = _insert_runtime(cid, "hermes-4346-f")
        good = _insert_runtime(cid, "hermes-4346-g")
        try:
            tid = _insert_task(bad)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid), SESSION_TIMEOUT_TEXT, -1)
            assert out == "requeued"
            st, rid, attempts, errors = _task_row(tid)
            assert st == "queued" and rid == 0 and attempts == 1
            entries = json.loads(errors or "[]")
            retry = next(e for e in entries if isinstance(e, dict)
                         and e.get("error_code") == "SESSION_RETRY")
            assert retry["failed_error_code"] == session_guard.SESSION_CREATE_BLOCKED
            assert retry["engine_id"] == bad
            assert "queue_depth" in retry and "disk_avail_bytes" in retry
            # 故障引擎在 session 冷却期 → 同引擎不得再认领；另一实例可认领（= 换引擎）
            assert service.claim_by_tag("all", bad) is None
            task = service.claim_by_tag("all", good)
            assert task and task["id"] == tid
        finally:
            _cleanup(cid)

    def test_retry_exhausted_falls_back_to_failed(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_MAX_RETRY", "1")
        cid = _insert_credential()
        bad = _insert_runtime(cid, "hermes-4346-h")
        _insert_runtime(cid, "hermes-4346-i")
        try:
            # 专用计数已用满 1 次（session_retry_count=1）→ 判死
            tid = _insert_task(bad, session_retry_count=1)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid), SESSION_TIMEOUT_TEXT, -1)
            assert out == "exhausted", "重试耗尽必须回落判死，不得原地挂起"
            st, rid, _attempts, errors = _task_row(tid)
            assert st == "running" and rid == bad
            assert "SESSION_CREATE_BLOCKED_EXHAUSTED" in (errors or "")
            assert "session_retry_count" in (errors or "")
        finally:
            _cleanup(cid)

    def test_historical_attempts_do_not_skip_session_retry(self, monkeypatch):
        """复审 Required：attempts 被 #403/#537 历史轮次用过，不得吞掉 session 换引擎重试。"""
        monkeypatch.setenv("RUNTIME_SESSION_MAX_RETRY", "1")
        cid = _insert_credential()
        bad = _insert_runtime(cid, "hermes-4346-h2")
        _insert_runtime(cid, "hermes-4346-i2")
        try:
            tid = _insert_task(bad, attempts=3, session_retry_count=0)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid), SESSION_TIMEOUT_TEXT, -1)
            assert out == "requeued", "历史 attempts 已 +1 不得跳过换引擎重试（spec 至少重试 1 次）"
            _st, _rid, attempts, errors = _task_row(tid)
            assert attempts == 4, "#403 的 attempts 口径不受影响（仍由 _do_requeue 累加）"
            with db_lock:
                cnt = db.cursor().execute(
                    "SELECT COALESCE(session_retry_count,0) FROM agent_tasks WHERE id=?",
                    (tid,)).fetchone()[0]
            assert cnt == 1, "session 专用计数必须独立累加"
            entries = json.loads(errors or "[]")
            retry = next(e for e in entries if isinstance(e, dict)
                         and e.get("error_code") == "SESSION_RETRY")
            assert retry["counter"] == "session_retry_count" and retry["attempt"] == 1
        finally:
            _cleanup(cid)

    def test_no_other_engine_fails_fast(self, monkeypatch):
        """池内只有故障引擎 → 直接判死（走真实池查询，不替换被测函数）。"""
        monkeypatch.setenv("RUNTIME_SESSION_MAX_RETRY", "1")
        cid = _insert_credential()
        tag = f"t4346pool-{uuid.uuid4().hex[:8]}"
        with _pool_isolated():
            only = _insert_runtime(cid, "hermes-4346-j", tag=tag)
            try:
                tid = _insert_task(only, tag=tag)
                out = session_guard.handle_task_failure(
                    tid, service.get_task(tid), SESSION_TIMEOUT_TEXT, -1)
                assert out == "exhausted"
                _st, _rid, _a, errors = _task_row(tid)
                assert "NO_SESSION_ENGINE_AVAILABLE" in (errors or "")
            finally:
                _cleanup(cid)

    def test_unreachable_is_observed_not_requeued(self, monkeypatch):
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-k")
        try:
            tid = _insert_task(rid)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid),
                "engine exit_code=-1, errors: initialize failed: connection refused", -1)
            assert out == "observed"
            st, _rid, attempts, errors = _task_row(tid)
            assert st == "running" and attempts == 0
            assert session_guard.ENGINE_UNREACHABLE in (errors or "")
        finally:
            _cleanup(cid)

    def test_misconfigured_engine_removed_from_pool(self, monkeypatch):
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-l")
        try:
            tid = _insert_task(rid)
            session_guard.handle_task_failure(
                tid, service.get_task(tid),
                "engine exit_code=1, errors: No LLM provider configured", -1)
            healthy, streak, _total, until, _half, code, llm_ok = _runtime_session_row(rid)
            assert llm_ok == 0 and healthy == 0, "配置缺陷引擎必须摘出调度池"
            assert code == session_guard.ENGINE_MISCONFIGURED and until
            assert backend_health.claim_allowed(rid) is False
        finally:
            _cleanup(cid)

    def test_non_session_failure_untouched(self, monkeypatch):
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-m")
        try:
            tid = _insert_task(rid)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid), "tests failed: 2 assertions", 1)
            assert out == "not_session"
            _st, _rid, _a, errors = _task_row(tid)
            assert session_guard.SESSION_CREATE_BLOCKED not in (errors or "")
        finally:
            _cleanup(cid)


# ── 5. 池可达性：has_other_session_engine 必须与认领闸门同一把尺子 ──

class TestOtherEngineAvailability:
    """复审 Required：真实 SQL 口径（不 monkeypatch 被测函数）。

    旧实现自造第三把尺（``healthy=1`` + ``credential_id>0`` 时 ``status=='online'``），
    与 ``claim_by_tag`` 的准入脱钩，两个方向都会错——把 ``probe_status=fail`` /
    ``backend_status=unavailable`` 的引擎算成可用（requeue 后无人认领，白等 600s 判死），
    又把 ``status`` 非 online 但探活正常的引擎算成不可用（直接 failed，丢掉重试）。
    """

    def _setup(self, name):
        cid = _insert_credential()
        tag = f"t4346pool-{uuid.uuid4().hex[:8]}"
        return cid, tag

    def test_probe_failed_engine_is_not_counted(self):
        cid, tag = self._setup("probe-fail")
        with _pool_isolated():
            failing = _insert_runtime(cid, "hermes-4346-q1", tag=tag)
            dead = _insert_runtime(cid, "hermes-4346-q2", tag=tag, probe_status="fail")
            try:
                assert session_guard.has_other_session_engine(
                    {"pool_tag": tag}, failing) is False
            finally:
                _cleanup(cid)

    def test_backend_unavailable_engine_is_not_counted(self):
        cid, tag = self._setup("backend-unavailable")
        with _pool_isolated():
            failing = _insert_runtime(cid, "hermes-4346-q3", tag=tag)
            dead = _insert_runtime(cid, "hermes-4346-q4", tag=tag, healthy=0,
                                   backend_status="unavailable")
            try:
                assert session_guard.has_other_session_engine(
                    {"pool_tag": tag}, failing) is False
            finally:
                _cleanup(cid)

    def test_session_circuit_open_engine_is_not_counted(self):
        cid, tag = self._setup("session-circuit")
        with _pool_isolated():
            failing = _insert_runtime(cid, "hermes-4346-q5", tag=tag)
            cooling = _insert_runtime(cid, "hermes-4346-q6", tag=tag)
            try:
                session_guard.note_session_failure(
                    cooling, session_guard.SESSION_CREATE_BLOCKED)
                assert session_guard.session_claim_allowed(cooling) is False
                assert session_guard.has_other_session_engine(
                    {"pool_tag": tag}, failing) is False
            finally:
                _cleanup(cid)

    def test_verified_engine_with_non_online_status_is_counted(self):
        """探活正常但 ``status`` 非 online → 认领闸门放行，池判定也必须算可用。"""
        cid, tag = self._setup("non-online")
        with _pool_isolated():
            failing = _insert_runtime(cid, "hermes-4346-q7", tag=tag)
            idle = _insert_runtime(cid, "hermes-4346-q8", tag=tag, status="idle")
            try:
                assert backend_health.claim_allowed(idle) is True
                assert session_guard.has_other_session_engine(
                    {"pool_tag": tag}, failing) is True
            finally:
                _cleanup(cid)

    def test_healthy_verified_engine_is_counted(self):
        cid, tag = self._setup("healthy")
        with _pool_isolated():
            failing = _insert_runtime(cid, "hermes-4346-q9", tag=tag)
            good = _insert_runtime(cid, "hermes-4346-q10", tag=tag)
            try:
                assert session_guard.has_other_session_engine(
                    {"pool_tag": tag}, failing) is True
                # 与认领闸门口径一致：可认领 ⇒ 池判定为可用
                assert backend_health.claim_allowed(good) is True
            finally:
                _cleanup(cid)


# ── 6. 健康分 completed 复位熔断 ──

def test_completed_task_clears_session_circuit(monkeypatch):
    """completed = 建会话成功的强证据 → 终态收口完整复位熔断（走真实终态入口）。"""
    monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
    cid = _insert_credential()
    rid = _insert_runtime(cid, "hermes-4346-n")
    try:
        tid = _insert_task(rid)
        session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
        assert session_guard.session_circuit_open(rid) is True
        service.update_task_status(tid, "completed")
        assert session_guard.session_circuit_open(rid) is False
        assert session_guard.session_claim_allowed(rid) is True
    finally:
        _cleanup(cid)


# ── 6. LLM provider 注册/启用期校验 ──

class TestProviderCheck:
    def test_missing_config_is_skipped_not_rejected(self, tmp_path, monkeypatch):
        """配置文件缺失 ≠ provider 缺失：按 engine_model 口径跳过校验（放行）。"""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
        env = {k: "" for k in provider_check._ENV_KEY_NAMES}
        res = provider_check.validate_engine_llm_provider("hermes-cred0", env=env)
        assert res["ok"] is True and res["source"] == "no_config"

    def test_config_present_without_target_is_rejected(self, tmp_path, monkeypatch):
        """配置文件在但解析不到 (base_url, api_key) → 拒绝（生产 500 的真实成因）。"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("model:\n  default: m\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        env = {k: "" for k in provider_check._ENV_KEY_NAMES}
        res = provider_check.validate_engine_llm_provider("hermes-cred0", env=env)
        assert res["ok"] is False, res
        assert "not usable" in res["reason"]

    def test_env_key_allows_missing_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
        env = {"HERMES_API_KEY": "k-test"}
        res = provider_check.validate_engine_llm_provider("hermes-cred0", env=env)
        assert res["ok"] is True and res["source"] == "no_config"
        assert "HERMES_API_KEY" in res["reason"]

    def test_unknown_engine_is_not_blocked(self):
        res = provider_check.validate_engine_llm_provider("hwcloud-cred0")
        assert res["ok"] is True and res["source"] == "unverified"

    def test_apply_registration_state_marks_row(self):
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-o")
        try:
            with db_lock:
                c = db.cursor()
                provider_check.apply_registration_state(
                    c, rid, "hermes-4346-o",
                    {"ok": False, "reason": "no llm provider configured", "provider": ""})
                db.commit()
            healthy, _s, _t, _u, _h, _code, llm_ok = _runtime_session_row(rid)
            assert llm_ok == 0 and healthy == 0
            assert session_guard.session_claim_allowed(rid) is False
            # 重新注册且 provider 正常 → 恢复（healthy 由 register 主链路写回，这里只验 ok 列）
            with db_lock:
                c = db.cursor()
                provider_check.apply_registration_state(
                    c, rid, "hermes-4346-o",
                    {"ok": True, "reason": "ok", "provider": "https://gw/v1"})
                c.execute("UPDATE agent_runtimes SET healthy=1 WHERE id=?", (rid,))
                db.commit()
            _healthy2, _s2, _t2, _u2, _h2, _code2, llm_ok2 = _runtime_session_row(rid)
            assert llm_ok2 == 1
            assert session_guard.session_claim_allowed(rid) is True
        finally:
            _cleanup(cid)

    def test_worker_without_report_keeps_old_behavior(self):
        cid = _insert_credential()
        rid = _insert_runtime(cid, "hermes-4346-p")
        try:
            with db_lock:
                c = db.cursor()
                state = provider_check.apply_registration_state(c, rid, "hermes-4346-p", None)
                db.commit()
            assert state["source"] == "not_reported"
            _healthy, _s, _t, _u, _h, _code, llm_ok = _runtime_session_row(rid)
            assert llm_ok == 1, "旧 worker 未上报时不得改写列（fail-open）"
        finally:
            _cleanup(cid)


# ── 7. #423 解析侧回归守护 ──

def test_acp_read_response_line_budget_not_regressed():
    """#423 曾因 `_read_response` 固定 10 行上限在 session/update 通知流中误报无响应。

    本 Issue 只改 session 失败分类，不得重新引入 10 行上限——防御上限必须远大于
    通知行数（行为级回归见 tests/insight/test_probe_acp_read_response_423.py）。
    """
    from src.domains.runtime.engine import agent
    assert agent._ACP_READ_MAX_LINES > 10, (
        "ACP 读取防御上限不得回退到 10 行（Issue #423 误报根因）")
    assert agent._ACP_READ_MAX_LINES >= 30