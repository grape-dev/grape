"""Agent 路由测试（src/api/routes/agents.py）。

覆盖：类型列表、Agent 详情（session/事件/VOD 群）、clean、配置（backend/pool/vod/技能）。
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock
from src.user import db as user_db

UID = 98901
USERNAME = "agent_tester"


@pytest.fixture(autouse=True)
def _client():
    app.dependency_overrides[get_current_user] = lambda: {"id": UID, "username": USERNAME}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": UID, "username": USERNAME}
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        c2 = db.cursor()
        c2.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:org/ag%' OR repo_key LIKE 'gitcode:org/a%'")
        c2.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:org/ag%' OR repo_key LIKE 'gitcode:org/a%'")
        c2.execute("DELETE FROM agent_type WHERE agent_type IN ('vod','issue','coding','qa','sre')")
        c2.execute("DELETE FROM skill WHERE skill_name LIKE 'agsk%'")
        c2.execute("DELETE FROM agent_runtimes WHERE name LIKE 'ag_rt%'")
        c2.execute("DELETE FROM runtime_credentials WHERE name LIKE 'ag_cred%'")
        db.commit()


def _seed_agent_types():
    with db_lock:
        c = db.cursor()
        for at, dn in [("vod", "VOD Agent"), ("issue", "Issue Agent"), ("coding", "Coding Agent"),
                       ("qa", "QA Agent"), ("sre", "SRE Agent")]:
            c.execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, enable) VALUES (?,?,?,?,?,1)",
                      (at, dn, f"desc {at}", "●", {"vod": 1, "issue": 2, "coding": 3, "qa": 4, "sre": 5}[at]))
        db.commit()


def _seed_session(repo_key, agent_type, rid=0, im_app_id=""):
    sid = "agsess-%s" % uuid.uuid4().hex[:8]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, runtime_session_id, repo_gitcode_token, im_app_id, enable, subscribe_events) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,'[]')",
            (sid, repo_key, agent_type, rid or None, "askill", "idle", "sess_x", "tok", im_app_id))
        db.commit()
        return sid


def _seed_engine():
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                  "VALUES ('h%s','ag_cred','ag-dev','global','tester','active')" % uuid.uuid4().hex)
        cid = c.lastrowid
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                  "VALUES ('ag_rt', 'ag_rt', ?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        db.commit()
        return c.lastrowid


class TestTypes:
    def test_list_types(self, _client):
        _seed_agent_types()
        r = _client.get("/api/agents/types")
        assert r.status_code == 200
        types = r.json()["types"]
        assert len(types) == 5
        assert types[0]["enable"] is True


class TestDetail:
    def test_unknown_agent_404(self, _client):
        assert _client.get("/api/agents/foo/org/repo").status_code == 404

    def test_missing_meta_404(self, _client):
        assert _client.get("/api/agents/vod/org/ag1").status_code == 404

    def test_detail_with_session_and_events(self, _client):
        _seed_agent_types()
        rk = "gitcode:org/agia1"
        _seed_session(rk, "issue")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status) VALUES (?,?,?,?,?,?)",
                      (rk, "issue_opened", "issue", "t", "{}", "completed"))
            db.commit()
        r = _client.get("/api/agents/issue/org/agia1")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_type"] == "issue"
        assert data["status"] == "idle"
        assert data["metrics"]["total_events"] == 1
        assert data["repo_gitcode_token"] == "tok"

    def test_detail_vod_conversations(self, _client):
        _seed_agent_types()
        rk = "gitcode:org/agvod1"
        sid = _seed_session(rk, "vod", im_app_id="cli_vod")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO vod_conversations (agent_session_id, im_chat_id, status) VALUES (?,?,?)", (sid, "oc_v", "active"))
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES ('cli_vod','vod_bot')")
            db.commit()
        r = _client.get("/api/agents/vod/org/agvod1")
        assert r.status_code == 200
        data = r.json()
        assert data["im"] is not None
        assert data["im"]["connected"] is True
        assert len(data["im"]["conversations"]) == 1
        assert data["im"]["conversations"][0]["chat_id"] == "oc_v"


class TestClean:
    def test_unknown_agent_404(self, _client):
        assert _client.post("/api/agents/foo/org/repo/clean").status_code == 404

    def test_clean_clears_session(self, _client):
        _seed_agent_types()
        rk = "gitcode:org/agc1"
        _seed_session(rk, "issue")
        with db_lock:
            db.cursor().execute("UPDATE devops_agent_session SET runtime_session_id='sess_keep' WHERE repo_key=? AND agent_type='issue'", (rk,))
            db.commit()
        r = _client.post(f"/api/agents/issue/org/agc1/clean")
        assert r.status_code == 200
        assert r.json()["cleaned"] is True
        with db_lock:
            row = db.cursor().execute("SELECT runtime_session_id FROM devops_agent_session WHERE repo_key=? AND agent_type='issue'", (rk,)).fetchone()
        assert row[0] == ""


class TestUpdateConfig:
    def test_unknown_agent_404(self, _client):
        assert _client.put("/api/agents/foo/org/repo", json={"backend": "x"}).status_code == 404

    def test_backend_single(self, _client):
        _seed_agent_types()
        _seed_engine()
        r = _client.put("/api/agents/issue/org/agupd1", json={"backend": "ag_rt", "skill": ""})
        assert r.status_code == 200, r.text
        assert r.json()["session_id"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_id, skill FROM devops_agent_session WHERE repo_key=? AND agent_type='issue'",
                ("gitcode:org/agupd1",)).fetchone()
        assert row and row[0] and row[0] > 0

    def test_pool_mode_unknown_engine_400(self, _client):
        r = _client.put("/api/agents/issue/org/repo", json={"backends": ["nope_engine"]})
        assert r.status_code == 400

    def test_pool_mode_binds(self, _client):
        _seed_agent_types()
        rid = _seed_engine()
        r = _client.put("/api/agents/issue/org/agpl1", json={"backends": ["ag_rt"]})
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type='issue'",
                ("gitcode:org/agpl1",)).fetchone()
        assert row and row[0] == rid

    def test_vod_chat_bot_conflict_400(self, _client):
        _seed_agent_types()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES ('cli_chatx','chat_bot')")
            db.commit()
        r = _client.put("/api/agents/vod/org/repo", json={"im_app_id": "cli_chatx", "im_app_secret": "s"})
        assert r.status_code == 400
        assert "Chat Agent" in r.json()["detail"]["message"]

    def test_vod_success_adds_bot(self, _client):
        _seed_agent_types()
        bm = MagicMock()
        bm.api = MagicMock()
        bm.add_bot = AsyncMock()
        bm.remove_bot = AsyncMock()
        with patch("src.api.main.bot_manager", bm):
            r = _client.put("/api/agents/vod/org/agv1", json={"im_app_id": "cli_vodx", "im_app_secret": "sec"})
        assert r.status_code == 200, r.text
        bm.add_bot.assert_awaited_once()

    def test_skill_repo_resolution(self, _client):
        _seed_agent_types()
        sname = "agsk_%s" % uuid.uuid4().hex[:4]
        rk = "gitcode:org/agupd2"
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, is_active) "
                      "VALUES (?,?,'d','issue',?,'repo',?,1)", (sname, sname, sname, rk))
            sid = c.lastrowid
            db.commit()
        r = _client.put(f"/api/agents/issue/org/agupd2", json={"skill": sname, "skill_scope": "repo"})
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute("SELECT skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type='issue'", (rk,)).fetchone()
        assert row and row[0] == sid

    def test_vod_placeholder_secret_reuses_stored(self, _client):
        _seed_agent_types()
        rk = "gitcode:org/agv2"
        _seed_session(rk, "vod", im_app_id="cli_vodx2")
        with db_lock:
            db.cursor().execute("UPDATE devops_agent_session SET im_app_secret='stored_sec' WHERE repo_key=? AND agent_type='vod'", (rk,))
            db.commit()
        bm = MagicMock()
        bm.api = MagicMock()
        bm.add_bot = AsyncMock()
        bm.remove_bot = AsyncMock()
        with patch("src.api.main.bot_manager", bm):
            r = _client.put("/api/agents/vod/org/agv2", json={"im_app_id": "cli_vodx2", "im_app_secret": "********"})
        assert r.status_code == 200, r.text
        bm.add_bot.assert_awaited_once()