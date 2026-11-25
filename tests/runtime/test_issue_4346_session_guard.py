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


def _insert_runtime(cid, name, healthy=1, tag="all"):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed, "
            "scope, last_seen_at, probe_status, worker_version, backend_status) "
            "VALUES (?, ?, 'online', ?, 1, 'global', ?, 'ok', 'w-4346', 'available')",
            (cid, name, healthy, now))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (?, ?, 5, 50, 1)", (rid, tag))
        db.commit()
    return rid


def _insert_task(runtime_id, status="running", attempts=0, repo="issue4346", tag="all"):
    tid = f"t_4346_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "skill, prompt, status, pool_tag, pool_agent_type, attempts, started_at, created_at) "
            "VALUES (?, ?, ?, ?, 'qa', 'qa', 'p', ?, ?, 'qa', ?, ?, ?)",
            (tid, f"sess-{tid}", runtime_id, f"gitcode:{repo}/repo", status, tag, attempts, now, now))
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
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE agent_runtimes SET session_circuit_until=? WHERE id=?",
                          (past, rid))
                db.commit()
            assert session_guard.session_claim_allowed(rid) is True
            with db_lock:
                c = db.cursor()
                session_guard.note_session_probe_started(c, rid)
                db.commit()
            assert session_guard.session_claim_allowed(rid) is False, "半开只放行一次探测"
            session_guard.note_session_success(rid)
            assert session_guard.session_claim_allowed(rid) is True
            _healthy, streak, _total, until, half, _code, _llm = _runtime_session_row(rid)
            assert streak == 0 and until == "" and half == 0
        finally:
            _cleanup(cid)

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
            tid = _insert_task(bad, attempts=1)
            out = session_guard.handle_task_failure(
                tid, service.get_task(tid), SESSION_TIMEOUT_TEXT, -1)
            assert out == "exhausted", "重试耗尽必须回落判死，不得原地挂起"
            st, rid, _attempts, errors = _task_row(tid)
            assert st == "running" and rid == bad
            assert "SESSION_CREATE_BLOCKED_EXHAUSTED" in (errors or "")
        finally:
            _cleanup(cid)

    def test_no_other_engine_fails_fast(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_SESSION_MAX_RETRY", "1")
        # 池判定置为「无其它引擎」：真实 SQL 会命中其它用例遗留的 tag='all' 引擎，
        # 单测只验本分支的收口语义（requeue 路径由 test_timeout_requeues_to_another_engine 覆盖）
        monkeypatch.setattr(session_guard, "has_other_session_engine", lambda *a, **k: False)
        cid = _insert_credential()
        only = _insert_runtime(cid, "hermes-4346-j")
        try:
            tid = _insert_task(only)
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


# ── 5. 健康分 completed 复位熔断 ──

def test_completed_task_clears_session_circuit(monkeypatch):
    monkeypatch.setenv("RUNTIME_SESSION_TIMEOUT_STREAK", "1")
    cid = _insert_credential()
    rid = _insert_runtime(cid, "hermes-4346-n")
    try:
        session_guard.note_session_failure(rid, session_guard.SESSION_CREATE_BLOCKED)
        assert session_guard.session_circuit_open(rid) is True
        health_scorer.record_task_completion(rid, "completed")
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