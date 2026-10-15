"""Issue #227: 场景编辑页引擎选择弹窗三个 bug 修复测试

验证点：
1. 后端 agent_engines 返回完整引擎列表（含 offline 状态）
2. 后端 agent_engines 对 runtime_id 去重
3. 搜索 hay 不包含 device/cred_name（前端逻辑由 JS 测试覆盖）
"""
import os
import datetime

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.runtime import engine_select as es
from src.runtime.health import derive_runtime_health
from src.user import db as user_db
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
_token = ""
_headers = {}


def _jwt_access_token(user_id, username="test227"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id="", owner_name="tester227"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h227_" + name, name, "dev-" + name, scope, owner_id, owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", owner_id="", last_seen=None, tags=None):
    ls = last_seen if last_seen is not None else _now_iso()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, ls, scope, owner_id))
        rid = c.lastrowid
        if tags:
            for t in tags:
                c.execute(
                    "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                    (rid, t))
        else:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
                (rid,))
        db.commit()
        return rid


def setup_module():
    global _uid, _token, _headers
    _uid = grape_account_db.create_grape_account("test227", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test227")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        subscribe_map = {
            "vod": '["vod_signal"]',
            "issue": '["issue_opened","issue_updated","issue_labeled","issue_comment"]',
            "coding": '["pull_request_opened","pull_request_updated","note_hook_pr"]',
            "qa": "[]",
            "sre": '["pull_request_merged","build_failure","deploy_start","deploy_failure","release_created"]',
        }
        for at, dn, ord_num in [("vod", "VOD Agent", 1), ("issue", "Issue Agent", 2),
                                 ("coding", "Coding Agent", 3), ("qa", "QA Agent", 4),
                                 ("sre", "SRE Agent", 5)]:
            c.execute(
                "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, subscribe_events) "
                "VALUES (?,?,?,?,?,?)",
                (at, dn, f"Description for {at}", "●", ord_num, subscribe_map.get(at, "[]")))
        db.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            ("gitcode:org/test227", "org", "test227", "org/test227"))
        db.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
            (_uid["id"], "gitcode:org/test227"))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


@pytest.fixture(autouse=True)
def _clean_engines():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR id IN (900001)")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (SELECT id FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR runtime_id IN (900001, 900002, 999, 2142, 2141)")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE '%test227%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR id IN (900001)")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (SELECT id FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR runtime_id IN (900001, 900002, 999, 2142, 2141)")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE '%test227%'")
        db.commit()


class TestAgentEnginesReturnsAllStatuses:
    """Issue #227: agent_engines 必须返回全部引擎（含 offline/about_to_gc）。"""

    def test_offline_engine_included(self):
        """offline 引擎应出现在 agent_engines 响应中。"""
        cid = _mk_cred("c227-off", scope="global")
        online_eng = _mk_engine("opencode-online", credential_id=cid, status="online",
                                 tags=["coding", "all"])
        offline_eng = _mk_engine("opencode-offline", credential_id=cid, status="offline",
                                  tags=["coding", "all"],
                                  last_seen="2020-01-01T00:00:00+00:00")
        r = client.get("/api/dashboard/org/test227", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        ae = d.get("agent_engines", {})
        coding_engines = ae.get("coding", [])
        ids = [e["id"] for e in coding_engines]
        assert online_eng in ids, f"online engine {online_eng} missing from {ids}"
        assert offline_eng in ids, f"offline engine {offline_eng} missing from {ids}"
        statuses = {e["id"]: e["status"] for e in coding_engines}
        assert statuses[online_eng] in ("online", "recently_lost")
        assert statuses[offline_eng] in ("offline", "about_to_gc")

    def test_about_to_gc_engine_included(self):
        """about_to_gc 引擎应出现在 agent_engines 响应中。"""
        cid = _mk_cred("c227-gc", scope="global")
        gc_eng = _mk_engine("opencode-gc", credential_id=cid, status="offline",
                             tags=["coding"],
                             last_seen="2020-01-01T00:00:00+00:00")
        r = client.get("/api/dashboard/org/test227", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        coding_engines = d.get("agent_engines", {}).get("coding", [])
        ids = [e["id"] for e in coding_engines]
        assert gc_eng in ids

    def test_all_five_agent_types_have_engine_lists(self):
        """所有 5 个 agent_type 都应有 agent_engines 条目（即使为空列表）。"""
        cid = _mk_cred("c227-types", scope="global")
        _mk_engine("opencode-types", credential_id=cid, status="online", tags=["all"])
        r = client.get("/api/dashboard/org/test227", headers=_headers)
        assert r.status_code == 200
        ae = r.json().get("agent_engines", {})
        for atype in ["vod", "issue", "coding", "qa", "sre"]:
            assert atype in ae, f"agent_type {atype} missing from agent_engines"
            assert isinstance(ae[atype], list)


class TestAgentEnginesDedup:
    """Issue #227: agent_engines 应对 runtime_id 去重，防止跨 scope 重复。"""

    def test_no_duplicate_runtime_ids(self):
        """同一 runtime_id 不应在 agent_engines 列表中重复出现。"""
        cid = _mk_cred("c227-dedup", scope="global", owner_name="test227")
        eng = _mk_engine("opencode-dedup", credential_id=cid, status="online", tags=["coding"])
        r = client.get("/api/dashboard/org/test227", headers=_headers)
        assert r.status_code == 200
        coding_engines = r.json().get("agent_engines", {}).get("coding", [])
        ids = [e["id"] for e in coding_engines]
        assert len(ids) == len(set(ids)), f"duplicate runtime_ids found: {ids}"
        assert eng in ids


class TestSearchHayFields:
    """Issue #227: 搜索 hay 不应包含 device/cred_name。

    验证后端返回的 engine 数据结构中 device/cred_name 字段仍存在（供展示），
    但前端搜索逻辑（JS 测试覆盖）不使用这些字段。
    """

    def test_engine_data_has_device_and_cred_name(self):
        """后端返回的 engine 数据应包含 device/cred_name 字段（供 UI 展示，非搜索）。"""
        cid = _mk_cred("c227-fields", scope="global", owner_name="alice227")
        _mk_engine("opencode-fields", credential_id=cid, status="online", tags=["coding"])
        r = client.get("/api/dashboard/org/test227", headers=_headers)
        assert r.status_code == 200
        coding_engines = r.json().get("agent_engines", {}).get("coding", [])
        assert len(coding_engines) > 0
        e = coding_engines[0]
        assert "device" in e, "device field missing from engine data"
        assert "cred_name" in e, "cred_name field missing from engine data"
        assert "name" in e
        assert "tags" in e
        assert "scope_label" in e
        assert "alias" in e
        assert "status" in e