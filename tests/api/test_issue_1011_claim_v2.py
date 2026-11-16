"""Issue #1011 原子契约测试：claim-by-uid v2 端点（协议层语义）。

覆盖（Issue 正文 UC-A / §2.3 失效语义 / In Scope 1）：
- A1  v2 claim uid 命中 → 按该引擎行认领（service.claim 收到解析后的 runtime_id）
- A2  v2 claim uid 失效 + body.runtime_id 命中 → 回退 id 认领
- A3  v2 claim 双失效 → 404 UNKNOWN_ENGINE_UID（不造行、不静默 200）
- A7  uid 命中但 body.runtime_id 也在 → uid 优先
- A9  v2 claim-by-tag uid 寻址 + 服务层复用（claim_by_tag 收到同一参数）
- A10 v2 claim-bound 归属校验：uid 不属于该凭据 → 403
- A11 v2 capabilities：uid_claim 能力位 True
- A5/A6 heartbeat-v2：engine_uids 优先解析、失效回退 runtime_ids、全失效不造行
- A12 上报 v2（complete/fail/progress）：任务行 engine_uid 与上报 uid 不一致 → 403
- A4  v1 claim 并存期行为不变（委托路径即 v1 函数，参数逐字段透传）

测试 seam：路由函数直调（绕过 HTTP/鉴权），monkeypatch 路由模块与 service 层的
db/db_lock 为临时 SQLite；service.claim / claim_by_tag / complete_task / fail_task /
enqueue_async_report 打桩（路由层只做寻址键转换的契约）。
"""
import sqlite3
import threading

import pytest
from fastapi import HTTPException

import src.domains.runtime.routes.runtime as rt
import src.domains.runtime.engine.service as service_mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "v2.db"), check_same_thread=False)
    conn.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id INTEGER DEFAULT 0,
        name TEXT NOT NULL,
        engine_uid VARCHAR(36) DEFAULT NULL,
        status TEXT DEFAULT 'offline',
        pending_skill_scan INTEGER DEFAULT 0,
        last_seen_at TEXT DEFAULT NULL,
        busy_slots INTEGER DEFAULT 0
    );
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY,
        engine_uid VARCHAR(36) DEFAULT NULL,
        runtime_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'queued'
    );
    """)
    # 凭据 7 拥有 uid 命中的引擎行（rid=1）；rid=2 供 id 回退；rid=9 属他人凭据
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (1, 7, 'eng-a', '11111111-1111-5111-8111-111111111111')")
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (2, 7, 'eng-b', '22222222-2222-5222-8222-222222222222')")
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (9, 8, 'eng-x', '99999999-9999-5999-8999-999999999999')")
    conn.execute("INSERT INTO agent_tasks (id, engine_uid, runtime_id) VALUES ('t-owned', '11111111-1111-5111-8111-111111111111', 1)")
    conn.execute("INSERT INTO agent_tasks (id, engine_uid, runtime_id) VALUES ('t-legacy', NULL, 2)")
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(rt, "db", conn, raising=True)
    monkeypatch.setattr(rt, "db_lock", lock, raising=True)
    monkeypatch.setattr(service_mod, "db", conn, raising=True)
    monkeypatch.setattr(service_mod, "db_lock", lock, raising=True)
    calls = []

    def fake_claim(runtime_id, expected_task_id=None, expected_event_seq=None):
        calls.append(("claim", runtime_id, expected_task_id, expected_event_seq))
        return {"id": "t1", "skill": ""}

    def fake_claim_by_tag(tag, runtime_id, expected_task_id=None, expected_event_seq=None):
        calls.append(("claim_by_tag", tag, runtime_id))
        return {"id": "t2", "skill": ""}

    monkeypatch.setattr(service_mod, "claim", fake_claim)
    monkeypatch.setattr(service_mod, "claim_by_tag", fake_claim_by_tag)
    return conn, calls


UID_A = "11111111-1111-5111-8111-111111111111"
UID_B = "22222222-2222-5222-8222-222222222222"
UID_STALE = "deadbeef-dead-5dea-8dea-deaddeaddead"


# ---------------------------------------------------------------------------
# A1/A2/A3/A7 · v2 claim 主端点
# ---------------------------------------------------------------------------

class TestClaimV2:
    def test_uid_hit(self, env):
        conn, calls = env
        resp = rt.runtime_claim_v2(UID_A, rt.RuntimeClaimBodyV2(), credential_id=7)
        assert calls == [("claim", 1, None, None)], "uid 未解析到正确引擎行"
        assert resp["task"]["id"] == "t1"

    def test_uid_stale_fallback_runtime_id(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(runtime_id=2)
        resp = rt.runtime_claim_v2(UID_STALE, body, credential_id=7)
        assert calls == [("claim", 2, None, None)], "uid 失效未回退 body.runtime_id"
        assert resp["task"]["id"] == "t1"

    def test_both_stale_404(self, env):
        conn, calls = env
        with pytest.raises(HTTPException) as ei:
            rt.runtime_claim_v2(UID_STALE, rt.RuntimeClaimBodyV2(runtime_id=55), credential_id=7)
        assert ei.value.status_code == 404
        assert ei.value.detail["code"] == "UNKNOWN_ENGINE_UID"
        assert calls == [], "双失效不应触达服务层（不造行）"

    def test_uid_priority_over_body_id(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(runtime_id=2)
        rt.runtime_claim_v2(UID_A, body, credential_id=7)
        assert calls[0][1] == 1, "uid 命中时应优先于 body.runtime_id"

    def test_uid_of_other_credential_stale(self, env):
        # 他人凭据的 uid 在本凭据视角 = 失效（不跨凭据解析）
        conn, calls = env
        with pytest.raises(HTTPException) as ei:
            rt.runtime_claim_v2("99999999-9999-5999-8999-999999999999",
                                rt.RuntimeClaimBodyV2(), credential_id=7)
        assert ei.value.status_code == 404

    def test_expected_fields_passthrough(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(expected_task_id="t9", expected_event_seq=42)
        rt.runtime_claim_v2(UID_A, body, credential_id=7)
        assert calls[0][2] == "t9" and calls[0][3] == 42, "expected 字段未透传服务层"


# ---------------------------------------------------------------------------
# A9/A10/A11 · v2 变体端点
# ---------------------------------------------------------------------------

class TestClaimVariantsV2:
    async def test_claim_by_tag_uid(self, env):
        # async + await 契约（同 heartbeat-v2 家族 bugfix）：v1 claim_by_tag 是 async，
        # v2 裸调返回 coroutine、服务层不被触达（线上 500 / 测试侧 calls 空）。
        import inspect
        assert inspect.iscoroutinefunction(rt.runtime_claim_by_tag), \
            "v1 runtime_claim_by_tag must stay async (to_thread wrapper)"
        assert inspect.iscoroutinefunction(rt.runtime_claim_by_tag_v2), \
            "v2 runtime_claim_by_tag_v2 must be async and await the async v1"
        conn, calls = env
        await rt.runtime_claim_by_tag_v2(UID_A, "coding", rt.RuntimeClaimBodyV2(), credential_id=7)
        assert calls == [("claim_by_tag", "coding", 1)]

    def test_claim_bound_owned_uid(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(expected_task_id="t9", expected_event_seq=42)
        resp = rt.runtime_claim_bound_v2(UID_A, body, credential_id=7)
        assert calls == [("claim", 1, "t9", 42)]

    def test_claim_bound_uid_not_owned_403(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(expected_task_id="t9", expected_event_seq=42)
        with pytest.raises(HTTPException) as ei:
            rt.runtime_claim_bound_v2(UID_STALE, body, credential_id=7)
        assert ei.value.status_code == 403
        assert calls == []

    def test_claim_by_tag_bound_uid(self, env):
        conn, calls = env
        body = rt.RuntimeClaimBodyV2(expected_task_id="t9", expected_event_seq=42)
        rt.runtime_claim_by_tag_bound_v2(UID_A, "coding", body, credential_id=7)
        assert calls == [("claim_by_tag", "coding", 1)]

    def test_capabilities_uid_claim(self, env):
        conn, calls = env
        resp = rt.runtime_claim_capabilities_v2(UID_A, credential_id=7)
        assert resp["capabilities"]["uid_claim"] is True
        assert resp["capabilities"]["bound_claim"] is True
        assert resp["runtime_id"] == 1 and resp["engine_uid"] == UID_A

    def test_capabilities_not_owned_403(self, env):
        with pytest.raises(HTTPException) as ei:
            rt.runtime_claim_capabilities_v2(UID_STALE, credential_id=7)
        assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# A5/A6 · heartbeat-v2
#
# Bugfix 回归守卫（2026-11-16 E2E 实测）：v1 runtime_heartbeat 是 async def，v2
# 曾以 sync def 裸调 → 返回 coroutine → 500（worker.log 6884/6884 全 500）。
# 契约：v2 必须 await v1；fake_hb 与生产同形态为 async（fake sync 会掩盖缺陷，
# 正是本 bug 漏网原因——故 test await 契约用 inspect.iscoroutinefunction 锚死
# 生产函数形态，而非只信 fake）。
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self):
        from types import SimpleNamespace
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")


class TestHeartbeatV2:
    def test_v1_heartbeat_is_async_and_v2_awaits(self, env):
        # 生产契约锚：v1 必须是 async def（to_thread 包装），v2 调用必须 await。
        import inspect
        assert inspect.iscoroutinefunction(rt.runtime_heartbeat), \
            "v1 runtime_heartbeat must stay async (to_thread wrapper)"
        assert inspect.iscoroutinefunction(rt.runtime_heartbeat_v2), \
            "v2 runtime_heartbeat_v2 must be async and await the async v1"

    async def test_uids_priority_and_fallback(self, env, monkeypatch):
        conn, calls = env
        seen = {}
        orig = rt.runtime_heartbeat

        async def fake_hb(body, request, credential_id):
            seen["ids"] = list(body.runtime_ids)
            return {"status": "ok", "cancelled_tasks": [], "latest_version": ""}

        monkeypatch.setattr(rt, "runtime_heartbeat", fake_hb)
        body = rt.HeartbeatBodyV2(engine_uids=[UID_A, UID_STALE], runtime_ids=[2])
        resp = await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        assert seen["ids"] == [1, 2], "uid 优先解析 + 失效回退 runtime_ids"
        assert resp["engine_uids_resolved"] == 1
        assert resp["engine_uids_stale"] == 1

    async def test_all_stale_no_row_created(self, env, monkeypatch):
        conn, _ = env
        before = conn.execute(
            "SELECT COUNT(*) FROM agent_runtimes WHERE engine_uid=?", (UID_STALE,)).fetchone()[0]
        seen = {}

        async def fake_hb(body, request, credential_id):
            seen["ids"] = list(body.runtime_ids)
            return {"status": "ok", "cancelled_tasks": [], "latest_version": ""}

        monkeypatch.setattr(rt, "runtime_heartbeat", fake_hb)
        body = rt.HeartbeatBodyV2(engine_uids=[UID_STALE])
        await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        assert seen["ids"] == []
        after = conn.execute(
            "SELECT COUNT(*) FROM agent_runtimes WHERE engine_uid=?", (UID_STALE,)).fetchone()[0]
        assert after == before, "全失效 uid 不得造行"


# ---------------------------------------------------------------------------
# A12 · 上报 v2（complete/fail/progress 按 uid 校验归属）
# ---------------------------------------------------------------------------

class TestReportV2:
    @pytest.fixture()
    def report_env(self, env, monkeypatch):
        done = {}

        def fake_complete(task_id, body, credential_id):
            done["complete"] = task_id
            return {"status": "completed"}

        def fake_fail(task_id, body, credential_id):
            done["fail"] = task_id
            return {"status": "failed"}

        def fake_progress(task_id, body, credential_id):
            done["progress"] = task_id
            return {"status": "ok"}

        monkeypatch.setattr(rt, "runtime_task_complete", fake_complete)
        monkeypatch.setattr(rt, "runtime_task_fail", fake_fail)
        monkeypatch.setattr(rt, "runtime_task_progress", fake_progress)
        return done

    def test_complete_uid_match(self, env, report_env):
        rt.runtime_task_complete_v2(
            "t-owned", rt.CompleteBodyV2(engine_uid=UID_A), credential_id=7)
        assert report_env["complete"] == "t-owned"

    def test_complete_uid_mismatch_403(self, env, report_env):
        with pytest.raises(HTTPException) as ei:
            rt.runtime_task_complete_v2(
                "t-owned", rt.CompleteBodyV2(engine_uid=UID_B), credential_id=7)
        assert ei.value.status_code == 403
        assert ei.value.detail["code"] == "TASK_ENGINE_UID_MISMATCH"
        assert "complete" not in report_env

    def test_complete_legacy_task_uid_empty_allowed(self, env, report_env):
        rt.runtime_task_complete_v2(
            "t-legacy", rt.CompleteBodyV2(engine_uid=UID_A), credential_id=7)
        assert report_env["complete"] == "t-legacy"

    def test_fail_uid_mismatch_403(self, env, report_env):
        with pytest.raises(HTTPException) as ei:
            rt.runtime_task_fail_v2(
                "t-owned", rt.FailBodyV2(engine_uid=UID_B), credential_id=7)
        assert ei.value.status_code == 403

    def test_progress_uid_mismatch_403(self, env, report_env):
        with pytest.raises(HTTPException) as ei:
            rt.runtime_task_progress_v2(
                "t-owned", rt.ProgressBodyV2(engine_uid=UID_B), credential_id=7)
        assert ei.value.status_code == 403
