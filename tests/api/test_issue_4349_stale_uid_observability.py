"""Issue #4349 原子契约测试：v2 engine_uid 失效（stale）观测治理。

覆盖（Issue #4349 Spec 评论 203688397 的 In Scope / Acceptance）：
- R1 `_resolve_runtime_by_uid()` stale uid：逐请求 WARNING → 一次性 ERROR +
  按 (uid, credential_id) 窗口限频；窗口内静默、窗口翻转恢复；回退命中时
  stale 报告关联回退行（/runtimes 详情 stale 标记来源）。
- R2 `runtime_heartbeat_v2()` stale 段：同款限频 ERROR + 指标计数 + 同请求
  runtime_ids 尽力关联；「stale_uids 非空且 runtime_ids 为空」的心跳丢失
  分支显式计数告警（grape_runtime_v2_heartbeat_uid_lost_total + 限频 ERROR）。
- R3 stale 报告注册表：凭据隔离、TTL 剪枝、快照结构（JSON 友好类型）。
- R4 `/settings/agent-runtimes`（列表+详情共用 get_agent_runtimes）engine_uid
  比对字段：报告关联到本行 → engine_uid_status=stale；否则 matched。
- R5 前端/locales 接线守卫（源级，同 test_vue_shell_and_engine_detail.py 模式）。

测试 seam：路由函数直调 + monkeypatch 路由模块 db/db_lock 为临时 SQLite
（同 test_issue_1011_claim_v2.py）；详情端点用 TestClient + 全局测试库
（同 test_issue_3396_tags_write_permission.py 模式）。
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.domains.runtime.routes.runtime as rt
import src.domains.runtime.engine.service as service_mod
from src.domains.runtime.engine import stale_uid


UID_A = "11111111-1111-5111-8111-111111111111"
UID_B = "22222222-2222-5222-8222-222222222222"
UID_STALE = "deadbeef-dead-5dea-8dea-deaddeaddead"
UID_STALE2 = "cafe1234-cafe-5caf-8caf-cafe12345678"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "v4349.db"), check_same_thread=False)
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
    """)
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (1, 7, 'eng-a', ?)", (UID_A,))
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (2, 7, 'eng-b', ?)", (UID_B,))
    conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) VALUES (9, 8, 'eng-x', '99999999-9999-5999-8999-999999999999')")
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(rt, "db", conn, raising=True)
    monkeypatch.setattr(rt, "db_lock", lock, raising=True)
    monkeypatch.setattr(service_mod, "db", conn, raising=True)
    monkeypatch.setattr(service_mod, "db_lock", lock, raising=True)
    stale_uid.clear()
    yield conn
    stale_uid.clear()


class _FakeClock:
    """可控时钟：绕过真实 time.time 验证限频窗口边界（不用 sleep 拖慢测试）。"""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now


# ---------------------------------------------------------------------------
# R1 · v2 claim resolve：stale uid 限频 ERROR + 回退行关联
# ---------------------------------------------------------------------------

class TestResolveStaleRateLimitedError:
    def test_stale_fallback_hit_one_error_per_window(self, env, caplog):
        # 两次 stale 命中（回退 rid=2 命中）：窗口内只有第 1 条 ERROR
        with caplog.at_level("ERROR"):
            assert rt._resolve_runtime_by_uid(UID_STALE, 2, 7) == 2
            assert rt._resolve_runtime_by_uid(UID_STALE, 2, 7) == 2
        errs = [r for r in caplog.records
                if r.levelname == "ERROR" and "engine_uid not found (stale?)" in r.getMessage()]
        assert len(errs) == 1, "窗口内第 2 次命中应被限频（不得逐请求刷屏）"
        # 计数/关联不受日志限频影响
        reps = stale_uid.reports_for_credential(7)
        assert len(reps) == 1 and reps[0]["uid"] == UID_STALE
        assert reps[0]["count"] == 2, "指标口径按次累加，不受日志限频影响"
        assert reps[0]["sources"] == ["resolve"]
        assert reps[0]["runtime_ids"] == [2], "回退命中应把报告关联到回退行"

    def test_window_rollover_logs_again(self, env, monkeypatch, caplog):
        clock = _FakeClock()
        monkeypatch.setattr(stale_uid.time, "time", clock.time)
        with caplog.at_level("ERROR"):
            rt._resolve_runtime_by_uid(UID_STALE, 2, 7)
            rt._resolve_runtime_by_uid(UID_STALE, 2, 7)      # 窗口内 → 静默
            clock.now += stale_uid.log_window_s() + 1        # 翻窗 → 新一条 ERROR
            rt._resolve_runtime_by_uid(UID_STALE, 2, 7)
        errs = [r for r in caplog.records
                if r.levelname == "ERROR" and "engine_uid not found (stale?)" in r.getMessage()]
        assert len(errs) == 2, "窗口翻转后应恢复一条 ERROR（首现可见）"
        assert stale_uid.reports_for_credential(7)[0]["count"] == 3

    def test_both_stale_404_no_association(self, env, caplog):
        with caplog.at_level("ERROR"):
            with pytest.raises(HTTPException) as ei:
                rt._resolve_runtime_by_uid(UID_STALE, 55, credential_id=7)
        assert ei.value.status_code == 404
        assert ei.value.detail["code"] == "UNKNOWN_ENGINE_UID"
        rep = stale_uid.reports_for_credential(7)[0]
        assert rep["runtime_ids"] == [], "双失效（回退未命中）不得关联任何 runtime 行"

    def test_uid_hit_no_report(self, env, caplog):
        with caplog.at_level("ERROR"):
            assert rt._resolve_runtime_by_uid(UID_A, 0, 7) == 1
        assert stale_uid.reports_for_credential(7) == [], "uid 命中不应产生 stale 报告"

    def test_credential_isolated(self, env, caplog):
        # 他人凭据视角：UID_A 属凭据 7，在凭据 8 视角 = stale
        with caplog.at_level("ERROR"):
            with pytest.raises(HTTPException):
                rt._resolve_runtime_by_uid(UID_A, 0, credential_id=8)
        assert [r["uid"] for r in stale_uid.reports_for_credential(8)] == [UID_A]
        assert stale_uid.reports_for_credential(7) == [], "报告按凭据隔离"


# ---------------------------------------------------------------------------
# R2 · heartbeat-v2：stale 段限频 + 心跳丢失分支计数告警
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self):
        from types import SimpleNamespace
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _stub_v1_heartbeat(monkeypatch, seen):
    async def fake_hb(body, request, credential_id):
        seen["ids"] = list(body.runtime_ids)
        return {"status": "ok", "cancelled_tasks": [], "latest_version": ""}
    monkeypatch.setattr(rt, "runtime_heartbeat", fake_hb)


class TestHeartbeatStaleObservability:
    async def test_stale_with_fallback_records_association(self, env, monkeypatch, caplog):
        seen = {}
        _stub_v1_heartbeat(monkeypatch, seen)
        with caplog.at_level("ERROR"):
            body = rt.HeartbeatBodyV2(engine_uids=[UID_A, UID_STALE], runtime_ids=[2])
            resp = await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        assert seen["ids"] == [1, 2], "uid 命中 + 回退 runtime_ids 语义不变"
        assert resp["engine_uids_resolved"] == 1
        assert resp["engine_uids_stale"] == 1
        errs = [r for r in caplog.records
                if r.levelname == "ERROR" and "heartbeat-v2: engine_uid not found" in r.getMessage()]
        assert len(errs) == 1, "stale 段一条 ERROR（本请求单 uid 首现）"
        rep = stale_uid.reports_for_credential(7)[0]
        assert rep["sources"] == ["heartbeat"] and rep["runtime_ids"] == [2]
        # 有 runtime_ids 回退 → 不是心跳丢失分支
        assert stale_uid.heartbeat_lost_count() == 0

    async def test_stale_only_no_fallback_lost_alert(self, env, monkeypatch, caplog):
        """验收分支：stale_uids 非空且 runtime_ids 为空 → 指标 + 显式告警。"""
        seen = {}
        _stub_v1_heartbeat(monkeypatch, seen)
        with caplog.at_level("ERROR"):
            body = rt.HeartbeatBodyV2(engine_uids=[UID_STALE, UID_STALE2])
            resp = await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        assert seen["ids"] == [], "全失效不造行/不回退（语义不变）"
        assert resp["engine_uids_stale"] == 2
        assert stale_uid.heartbeat_lost_count() == 1, "心跳丢失分支必须有计数指标"
        lost = [r for r in caplog.records if r.levelname == "ERROR"
                and "heartbeat lost" in r.getMessage()]
        assert len(lost) == 1, "心跳丢失必须显式告警（不再静默丢弃）"

    async def test_lost_alert_rate_limited_but_counted(self, env, monkeypatch, caplog):
        seen = {}
        _stub_v1_heartbeat(monkeypatch, seen)
        body = rt.HeartbeatBodyV2(engine_uids=[UID_STALE])
        with caplog.at_level("ERROR"):
            await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
            await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        lost = [r for r in caplog.records if r.levelname == "ERROR"
                and "heartbeat lost" in r.getMessage()]
        assert len(lost) == 1, "心跳丢失 ERROR 按凭据限频（防再次刷屏）"
        assert stale_uid.heartbeat_lost_count() == 2, "计数不受日志限频影响"

    async def test_hit_only_no_stale_side_effects(self, env, monkeypatch, caplog):
        seen = {}
        _stub_v1_heartbeat(monkeypatch, seen)
        body = rt.HeartbeatBodyV2(engine_uids=[UID_A, UID_B])
        with caplog.at_level("ERROR"):
            resp = await rt.runtime_heartbeat_v2(body, _FakeRequest(), credential_id=7)
        assert resp["engine_uids_resolved"] == 2
        assert stale_uid.reports_for_credential(7) == []
        assert stale_uid.heartbeat_lost_count() == 0


# ---------------------------------------------------------------------------
# R3 · 报告注册表：TTL 剪枝与快照口径
# ---------------------------------------------------------------------------

class TestStaleUidRegistry:
    def test_ttl_prune(self, env, monkeypatch):
        clock = _FakeClock()
        monkeypatch.setattr(stale_uid.time, "time", clock.time)
        stale_uid.record_stale(UID_STALE, 7, "resolve")
        clock.now += stale_uid.retention_s() + 1
        assert stale_uid.reports_for_credential(7) == [], "超过 TTL 的报告应剪枝"

    def test_snapshot_json_friendly(self, env):
        stale_uid.record_stale(UID_STALE, 7, "resolve", runtime_ids=(2,))
        reps = stale_uid.reports_for_credential(7)
        assert json.dumps(reps), "快照必须 JSON 可序列化"
        assert set(reps[0]) == {"uid", "credential_id", "count", "first_seen",
                                "last_seen", "sources", "runtime_ids"}

    def test_window_env_override(self, env, monkeypatch):
        monkeypatch.setenv("GRAPE_STALE_UID_LOG_WINDOW", "abc")
        assert stale_uid.log_window_s() == 3600.0, "非法窗口配置回退默认"
        monkeypatch.setenv("GRAPE_STALE_UID_LOG_WINDOW", "0")
        assert stale_uid.log_window_s() == 3600.0, "非正窗口配置回退默认"
        monkeypatch.setenv("GRAPE_STALE_UID_LOG_WINDOW", "120")
        assert stale_uid.log_window_s() == 120.0


# ---------------------------------------------------------------------------
# R4 · /settings/agent-runtimes engine_uid 比对字段（列表+详情共用出口）
# ---------------------------------------------------------------------------

def _mk_credential_engine(owner_name="owner_4349", scope="user", owner_id="4349"):
    """全局测试库建凭据+引擎行（同 test_issue_3396 模式），返回 (cid, rid)。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        token = "rta_" + secrets.token_urlsafe(16)
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status, org_id) "
            "VALUES (?,?,?,?,?,?,'active','')",
            (hashlib.sha256(token.encode()).hexdigest(),
             "b4349-cred-" + secrets.token_hex(4), "dev-b4349", scope, owner_id, owner_name))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, scope, owner_id, org_id, engine_uid, agent_tags) "
            "VALUES ('opencode','b4349',?,'online',1,1,?,?, '', ?, '[]')",
            (cid, scope, owner_id, UID_B))
        rid_assoc = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, scope, owner_id, org_id, engine_uid, agent_tags) "
            "VALUES ('hermes','b4349x',?,'online',1,1,?,?, '', ?, '[]')",
            (cid, scope, owner_id, UID_A))
        rid_clean = c.lastrowid
        db.commit()
    return cid, rid_assoc, rid_clean


def _clean_4349_rows():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id IN "
                  "(SELECT id FROM runtime_credentials WHERE name LIKE 'b4349-%')")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'b4349-%'")
        db.commit()


@pytest.fixture()
def detail_env(monkeypatch):
    from src.api.main import app
    from src.api.deps import require_org_member, _resolve_user_or_platform_admin
    stale_uid.clear()
    cid, rid_assoc, rid_clean = _mk_credential_engine()
    # 模拟该凭据下引擎上报失效 uid（claim 回退命中 rid_assoc 的场景）
    stale_uid.record_stale(UID_STALE, cid, "resolve", runtime_ids=(rid_assoc,))
    user = {"id": 4349, "username": "owner_4349", "auth_type": "jwt", "is_platform_admin": False}
    app.dependency_overrides[_resolve_user_or_platform_admin] = lambda: user
    app.dependency_overrides[require_org_member] = lambda: user
    client = TestClient(app)
    yield client, cid, rid_assoc, rid_clean
    app.dependency_overrides.pop(_resolve_user_or_platform_admin, None)
    app.dependency_overrides.pop(require_org_member, None)
    _clean_4349_rows()
    stale_uid.clear()


class TestRuntimeDetailEngineUidComparison:
    def test_detail_exposes_stale_marker(self, detail_env):
        """报告关联到的引擎行标 stale，其余同行 matched（详情可见比对结果）。"""
        client, cid, rid_assoc, rid_clean = detail_env
        r = client.get(f"/api/settings/agent-runtimes/{rid_assoc}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["engine_uid"] == UID_B, "DB 行 engine_uid 应透出"
        assert d["engine_uid_status"] == "stale"
        assert any(rep["uid"] == UID_STALE for rep in d["engine_uid_stale_reports"])
        # 同凭据未关联到报告的引擎行：报告列表仍可见（凭据维度），本行状态 matched
        r2 = client.get(f"/api/settings/agent-runtimes/{rid_clean}")
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["engine_uid_status"] == "matched"
        assert d2["engine_uid_stale_reports"], "凭据级报告随详情带出供比对"
        # 列表端点同字段（同 get_agent_runtimes 出口）
        r3 = client.get("/api/settings/agent-runtimes")
        assert r3.status_code == 200, r3.text
        by_id = {row.get("id"): row for row in r3.json()["runtimes"]}
        assert by_id[rid_assoc]["engine_uid_status"] == "stale"
        assert by_id[rid_clean]["engine_uid_status"] == "matched"

    def test_detail_without_reports_matched(self, detail_env, monkeypatch):
        """无 stale 报告的凭据：所有引擎行 matched，报告列表为空。"""
        stale_uid.clear()
        client, cid, rid_assoc, rid_clean = detail_env
        r = client.get(f"/api/settings/agent-runtimes/{rid_clean}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["engine_uid_status"] == "matched"
        assert d["engine_uid_stale_reports"] == []


# ---------------------------------------------------------------------------
# R5 · 前端 / locales 接线守卫（源级）
# ---------------------------------------------------------------------------

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_frontend_detail_modal_wiring():
    """引擎详情弹窗展示 engine_uid 比对结果（stale 标记 + tooltip 文案）。"""
    vue = _read(os.path.join(ROOT, "src/api/frontend/app/src/views/RuntimesView.vue"))
    assert "engine_uid_status === 'stale'" in vue
    assert "engineUidStaleTitle" in vue
    assert "rtv-uid-stale" in vue


def test_frontend_locale_keys_present():
    for lang in ("zh", "en"):
        doc = json.loads(_read(os.path.join(
            ROOT, "src/api/frontend/shell/locales", lang, "runtimes.json")))
        for key in ("engine_uid", "engine_uid_stale", "engine_uid_stale_tip"):
            assert doc["label"].get(key), f"{lang} locales 缺少 label.{key}"


def test_metrics_and_lost_branch_wiring():
    """runtime.py 心跳丢失分支与指标接线守卫（防回归静默化）。"""
    src = _read(os.path.join(ROOT, "src/domains/runtime/routes/runtime.py"))
    assert "record_heartbeat_lost(" in src
    assert "grape_runtime_v2_stale_uid_total" in _read(
        os.path.join(ROOT, "src/domains/runtime/engine/stale_uid.py"))
