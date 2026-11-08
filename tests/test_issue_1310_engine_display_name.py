"""Issue #1310: 后端引擎下拉数据源补「凭据名」字段测试

验证点：
1. dashboard /agent_engines 条目含 engine_name（引擎类型名）与 worker_name（凭据名）
2. dashboard /agent_engines 本机直连（credential_id=0）worker_name=host_name（兜底口径）
3. agents 详情 pool 含 worker_name（凭据名）与 instance_name（标识不变）
4. runtime-devices 设备子列表 runtimes 含 worker_name（凭据名；本机直连=host_name）
"""
import os
import datetime
import json

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
_token = ""
_headers = {}


def _jwt_access_token(user_id, username="t1310"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_name="tester1310"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h1310_" + name, name, "dev-" + name, scope, "", owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", tags=None, host_name=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id, host_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, _now_iso(), scope, "", host_name))
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
    _uid = grape_account_db.create_grape_account("t1310", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("t1310")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        for at, dn in [("vod", "VOD Agent"), ("issue", "Issue Agent"), ("coding", "Coding Agent"),
                       ("qa", "QA Agent"), ("sre", "SRE Agent")]:
            c.execute(
                "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, subscribe_events) "
                "VALUES (?,?,?,?,?,'[]')",
                (at, dn, f"Description for {at}", "●", 1))
        db.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            ("gitcode:org/test1310", "org", "test1310", "org/test1310"))
        db.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
            (_uid["id"], "gitcode:org/test1310"))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


@pytest.fixture(autouse=True)
def _clean_rows():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'e1310_%' OR id IN (SELECT id FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'c1310_%'))")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'c1310_%'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (SELECT id FROM agent_runtimes WHERE name LIKE 'e1310_%')")
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id='scene-1310'")
        c.execute("UPDATE managed_repos SET dev_scene='' WHERE repo_key='gitcode:org/test1310'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key='gitcode:org/test1310'")
        c.execute("DELETE FROM events WHERE repo_key LIKE '%1310%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'e1310_%' OR id IN (SELECT id FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'c1310_%'))")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'c1310_%'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN (SELECT id FROM agent_runtimes WHERE name LIKE 'e1310_%')")
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id='scene-1310'")
        c.execute("UPDATE managed_repos SET dev_scene='' WHERE repo_key='gitcode:org/test1310'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key='gitcode:org/test1310'")
        c.execute("DELETE FROM events WHERE repo_key LIKE '%1310%'")
        db.commit()


class TestDashboardEnginesWorkerName:
    def test_agent_engines_carry_engine_name_and_worker_name(self):
        """dashboard agent_engines 条目含 engine_name（类型名）+ worker_name（凭据名），name 仍为 instance_name。"""
        cid = _mk_cred("c1310-dash", scope="global", owner_name="tester1310")
        rid = _mk_engine("e1310-opencode", credential_id=cid, status="online", tags=["coding"])
        r = client.get("/api/dashboard/org/test1310", headers=_headers)
        assert r.status_code == 200
        coding = r.json().get("agent_engines", {}).get("coding", [])
        it = next((e for e in coding if e["id"] == rid), None)
        assert it is not None, f"engine {rid} missing from agent_engines: {[e['id'] for e in coding]}"
        assert it["engine_name"] == "e1310-opencode", it
        assert it["worker_name"] == "c1310-dash", it
        # name 仍为 instance_name（标识/类型推断兼容不变）
        assert it["name"].startswith("e1310-opencode-"), it
        assert it["name"] != "e1310-opencode"

    def test_agent_engines_engine_name_uses_display_mapping(self):
        """engine_name 经 to_display：内部名 hwcloud → AISHELL（与其余界面一致）。"""
        cid = _mk_cred("c1310-hwcl", scope="global", owner_name="tester1310")
        rid = _mk_engine("hwcloud", credential_id=cid, status="online", tags=["coding"])
        r = client.get("/api/dashboard/org/test1310", headers=_headers)
        assert r.status_code == 200
        coding = r.json().get("agent_engines", {}).get("coding", [])
        it = next((e for e in coding if e["id"] == rid), None)
        assert it is not None, f"engine {rid} missing"
        assert it["engine_name"] == "AISHELL", it

    def test_local_direct_engine_worker_name_is_host_name(self):
        """本机直连（credential_id=0）引擎 worker_name=host_name（兜底口径）。"""
        rid = _mk_engine("e1310-local", credential_id=0, status="online", tags=["coding"],
                         host_name="host-1310-a")
        r = client.get("/api/dashboard/org/test1310", headers=_headers)
        assert r.status_code == 200
        coding = r.json().get("agent_engines", {}).get("coding", [])
        it = next((e for e in coding if e["id"] == rid), None)
        assert it is not None, f"local engine {rid} missing"
        assert it["worker_name"] == "host-1310-a", it


class TestAgentsDetailPoolWorkerName:
    def test_pool_contains_worker_name_and_instance_name(self):
        """agents 详情 pool 条目含 worker_name（凭据名）与 instance_name（标识不变）。"""
        cid = _mk_cred("c1310-pool", scope="global", owner_name="tester1310")
        rid = _mk_engine("e1310-hermes", credential_id=cid, status="online", tags=["coding"])
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) VALUES (?,?,?,?,1)",
                ("scene-1310", "scene-1310", "gitcode:org/test1310",
                 json.dumps([{"id": "coding", "tag": "coding", "engine_runtime_ids": [rid]}])))
            c.execute("UPDATE managed_repos SET dev_scene='scene-1310' WHERE repo_key='gitcode:org/test1310'")
            db.commit()
        r = client.get("/api/agents/coding/org/test1310", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "pool" in d, d.keys()
        assert d["pool"], "pool 应为非空"
        p = d["pool"][0]
        assert p["worker_name"] == "c1310-pool", p
        assert p["runtime_id"] == rid, p
        assert p["instance_name"].startswith("e1310-hermes-"), p
        assert p["name"] == "e1310-hermes", p


class TestRuntimeDevicesWorkerName:
    def test_device_runtimes_carry_worker_name(self):
        """runtime-devices 设备子列表 runtimes 含 worker_name（凭据名）。"""
        cid = _mk_cred("c1310-dev", scope="global", owner_name="tester1310")
        rid = _mk_engine("e1310-codex", credential_id=cid, status="online", tags=["coding"])
        r = client.get("/api/settings/runtime-devices", headers=_headers)
        assert r.status_code == 200
        devices = r.json().get("devices", [])
        target = next((d for d in devices if d.get("credential_id") == cid), None)
        assert target is not None, f"device cred {cid} missing: {[d.get('credential_id') for d in devices]}"
        rt = next((x for x in target.get("runtimes", []) if x["id"] == rid), None)
        assert rt is not None, "engine missing from device runtimes"
        assert rt["worker_name"] == "c1310-dev", rt

    def test_local_device_runtimes_worker_name_is_host_name(self):
        """本机直连（credential_id=0）设备子列表 worker_name=host_name。"""
        rid = _mk_engine("e1310-localdev", credential_id=0, status="online", tags=["coding"],
                         host_name="host-1310-b")
        r = client.get("/api/settings/runtime-devices", headers=_headers)
        assert r.status_code == 200
        devices = r.json().get("devices", [])
        # 本机引擎挂在 host_name 命名的设备下，或 __local__ 设备
        found = False
        for d in devices:
            for rt in d.get("runtimes", []):
                if rt["id"] == rid:
                    assert rt["worker_name"] == "host-1310-b", rt
                    found = True
        assert found, "local engine missing from any device runtimes"