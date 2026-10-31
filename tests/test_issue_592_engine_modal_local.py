"""Issue #592: 场景详情页编辑模式引擎选择弹窗本机直连引擎显示修复测试

验证点：
1. 本机直连（credential_id=0）引擎状态按探活口径派生（installed+healthy → online），
   不误用远端心跳口径（last_seen 超 100s 不再误判 recently_lost/失联）。
2. 无 engine_capabilities 标签的历史本机直连行仍出现在弹窗 agent_engines 列表
   （数量与实际注册一致，默认按 'all' 广播语义兜底）。
3. 远端引擎（credential_id>0）心跳口径不变（last_seen 过期仍 recently_lost）。
"""
import os
import datetime

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
_headers = {}


def _jwt_access_token(user_id, username="test592"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stale_iso(minutes=30):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes)).isoformat()


def _mk_local_engine(name, *, healthy=1, installed=1, tags=None, last_seen=None):
    """插入本机直连引擎行（credential_id=0）；tags=None 表示不建 capabilities 行（历史遗留）。"""
    ls = last_seen if last_seen is not None else _stale_iso(30)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,0,'online',?,?,?,?,?)",
            (name, name, healthy, installed, ls, "global", ""))
        rid = c.lastrowid
        if tags is not None:
            for t in tags:
                c.execute(
                    "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                    (rid, t))
        db.commit()
        return rid


def _mk_cred(name, scope="global"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h592_" + name, name, "dev-" + name, scope, "", "test592"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_remote_engine(name, cid, *, status="online", last_seen=None):
    ls = last_seen if last_seen is not None else _now_iso()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,1,1,?,?,'')",
            (name, name, cid, status, ls, "global"))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'coding', 1)",
            (rid,))
        db.commit()
        return rid


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test592", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test592")
    _headers = {"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"}
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
            ("gitcode:org/test592", "org", "test592", "org/test592"))
        db.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
            (_uid["id"], "gitcode:org/test592"))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


_CLEAN_NAMES = "test592_"


@pytest.fixture(autouse=True)
def _clean_engines():
    def _purge():
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM engine_capabilities WHERE runtime_id IN "
                "(SELECT id FROM agent_runtimes WHERE name LIKE 'test592_%')")
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test592_%'")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test592_%'")
            c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE '%test592%'")
            c.execute("DELETE FROM events WHERE repo_key LIKE '%test592%'")
            db.commit()
    _purge()
    yield
    _purge()


def _coding_engines():
    r = client.get("/api/dashboard/org/test592", headers=_headers)
    assert r.status_code == 200
    return r.json().get("agent_engines", {}).get("coding", [])


class TestLocalEngineStatusByProbe:
    """Issue #592 根因 A：本机直连引擎状态误用远端心跳口径 → 全显失联。"""

    def test_local_engine_probe_stale_last_seen_shows_online(self):
        """本机直连引擎 installed+healthy、last_seen 超 30min（>100s 心跳阈值）→ online，非失联。"""
        rid = _mk_local_engine("test592_local_ok", healthy=1, installed=1, tags=["all"])
        eng = next((e for e in _coding_engines() if e["id"] == rid), None)
        assert eng is not None, "local engine missing from modal engine list"
        assert eng["status"] == "online", (
            f"local engine with probe-fresh installed/healthy should be online, got {eng['status']}")

    def test_local_engine_unhealthy_shows_offline(self):
        """本机直连引擎探活不健康 → offline（探活真实结果，非心跳口径）。"""
        rid = _mk_local_engine("test592_local_bad", healthy=0, installed=1, tags=["all"])
        eng = next((e for e in _coding_engines() if e["id"] == rid), None)
        assert eng is not None
        assert eng["status"] == "offline"

    def test_local_engine_not_installed_shows_offline(self):
        rid = _mk_local_engine("test592_local_ni", healthy=1, installed=0, tags=["all"])
        eng = next((e for e in _coding_engines() if e["id"] == rid), None)
        assert eng is not None
        assert eng["status"] == "offline"


class TestLocalEngineWithoutTagsVisible:
    """Issue #592 根因 B：无 capabilities 标签的历史本机直连行被 agent_type 过滤 → 数量偏少。"""

    def test_local_engine_without_tags_counted(self):
        """无 engine_capabilities 行的本机直连引擎应出现在弹窗列表（默认 'all' 语义）。"""
        rid = _mk_local_engine("test592_local_notag", healthy=1, installed=1, tags=None)
        engines = _coding_engines()
        ids = [e["id"] for e in engines]
        assert rid in ids, (
            f"legacy local engine without capability tags missing from modal list: {ids}")

    def test_local_engine_with_agent_tag_visible(self):
        rid = _mk_local_engine("test592_local_tagged", healthy=1, installed=1, tags=["coding"])
        ids = [e["id"] for e in _coding_engines()]
        assert rid in ids

    def test_local_count_matches_registered(self):
        """弹窗本机直连数量与插入的注册行数量一致（含无标签历史行）。"""
        rids = [_mk_local_engine(f"test592_local_cnt{i}", healthy=1, installed=1,
                                 tags=None if i % 2 else ["all"])
                for i in range(3)]
        engines = _coding_engines()
        local_ids = [e["id"] for e in engines if e["id"] in rids]
        assert sorted(local_ids) == sorted(rids)


class TestRemoteEngineHeartbeatUnchanged:
    """远端引擎（credential_id>0）心跳口径不受本修复影响。"""

    def test_remote_stale_shows_recently_lost(self):
        cid = _mk_cred("test592_rem")
        rid = _mk_remote_engine("test592_rem_stale", cid, last_seen=_stale_iso(30))
        eng = next((e for e in _coding_engines() if e["id"] == rid), None)
        assert eng is not None
        assert eng["status"] == "recently_lost"

    def test_remote_fresh_shows_online(self):
        cid = _mk_cred("test592_rem2")
        rid = _mk_remote_engine("test592_rem_fresh", cid, last_seen=_now_iso())
        eng = next((e for e in _coding_engines() if e["id"] == rid), None)
        assert eng is not None
        assert eng["status"] == "online"


class TestQueryEnginesLocalDefaultAll:
    """query_engines local 档无标签行默认 'all' 的单元级验证。"""

    def test_query_local_no_tags_returns_engine(self):
        from src.runtime import engine_select as es
        rid = _mk_local_engine("test592_q_no_tag", healthy=1, installed=1, tags=None)
        cand = es.query_engines("org/test592", "local", agent_type="coding")
        ids = [e["runtime_id"] for e in cand]
        assert rid in ids

    def test_query_local_with_tag_returns_engine(self):
        from src.runtime import engine_select as es
        rid = _mk_local_engine("test592_q_tag", healthy=1, installed=1, tags=["coding"])
        cand = es.query_engines("org/test592", "local", agent_type="coding")
        ids = [e["runtime_id"] for e in cand]
        assert rid in ids
