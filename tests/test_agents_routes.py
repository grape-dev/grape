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
    app.dependency_overrides[get_current_user] = lambda: {"id": UID, "username": USERNAME, "auth_type": "api_key"}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": UID, "username": USERNAME, "auth_type": "api_key"}
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
        assert data["repo_gitcode_token_exists"] == True
        assert data["highlight_issue_id"] == ""

    def test_detail_with_issue_number_highlight(self, _client):
        """Issue #108: issue_number 参数归一化为 #N 并在响应中返回"""
        _seed_agent_types()
        rk = "gitcode:org/agih1"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, issue_id) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t1", "{}", "completed", "#108"))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, issue_id) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t2", "{}", "completed", "#99"))
            db.commit()
        r = _client.get("/api/agents/coding/org/agih1?issue_number=108")
        assert r.status_code == 200
        data = r.json()
        assert data["highlight_issue_id"] == "#108"
        assert len(data["recent_events"]) == 2

    def test_detail_with_issue_number_strips_hash(self, _client):
        """Issue #108: issue_number 带不带 # 都能归一化"""
        _seed_agent_types()
        rk = "gitcode:org/agih2"
        _seed_session(rk, "qa")
        r = _client.get("/api/agents/qa/org/agih2?issue_number=%2342")
        assert r.status_code == 200
        assert r.json()["highlight_issue_id"] == "#42"

    def test_detail_without_issue_number_no_highlight(self, _client):
        """Issue #108: 不传 issue_number 时 highlight_issue_id 为空（向后兼容）"""
        _seed_agent_types()
        rk = "gitcode:org/agih3"
        _seed_session(rk, "sre")
        r = _client.get("/api/agents/sre/org/agih3")
        assert r.status_code == 200
        assert r.json()["highlight_issue_id"] == ""

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

    # ── Issue #127: 搜索 + 分页测试 ──

    def test_detail_pagination_default(self, _client):
        """默认 page=1, page_size=10，返回 events_total"""
        _seed_agent_types()
        rk = "gitcode:org/agpg1"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            for i in range(15):
                c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                          (rk, "a2a_event", "coding", f"evt-{i}", "{}", "completed", 100 + i))
            db.commit()
        r = _client.get("/api/agents/coding/org/agpg1")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 15
        assert data["events_page"] == 1
        assert data["events_page_size"] == 10
        assert len(data["recent_events"]) == 10

    def test_detail_pagination_custom_page_size(self, _client):
        """page_size=5 返回 5 条，page=2 返回后 5 条"""
        _seed_agent_types()
        rk = "gitcode:org/agpg2"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            for i in range(12):
                c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                          (rk, "a2a_event", "coding", f"evt-{i}", "{}", "completed", 200 + i))
            db.commit()
        r = _client.get("/api/agents/coding/org/agpg2?page=2&page_size=5")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 12
        assert data["events_page"] == 2
        assert data["events_page_size"] == 5
        assert len(data["recent_events"]) == 5

    def test_detail_keyword_search_by_title(self, _client):
        """keyword 搜索 title 字段"""
        _seed_agent_types()
        rk = "gitcode:org/agkw1"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "fix login bug", "{}", "completed", 300))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "add feature", "{}", "completed", 301))
            db.commit()
        r = _client.get("/api/agents/coding/org/agkw1?keyword=login")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 1
        assert len(data["recent_events"]) == 1
        assert data["recent_events"][0]["title"] == "fix login bug"

    def test_detail_keyword_search_by_seq(self, _client):
        """keyword 搜索 seq 字段（数字模糊匹配）"""
        _seed_agent_types()
        rk = "gitcode:org/agkw2"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t1", "{}", "completed", 400))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t2", "{}", "completed", 401))
            db.commit()
        r = _client.get("/api/agents/coding/org/agkw2?keyword=400")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 1
        assert data["recent_events"][0]["seq"] == 400

    def test_detail_keyword_search_by_issue_id(self, _client):
        """keyword 搜索 issue_id 字段"""
        _seed_agent_types()
        rk = "gitcode:org/agkw3"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq, issue_id) VALUES (?,?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t1", "{}", "completed", 500, "#127"))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq, issue_id) VALUES (?,?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "t2", "{}", "completed", 501, "#99"))
            db.commit()
        r = _client.get("/api/agents/coding/org/agkw3?keyword=127")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 1
        assert data["recent_events"][0]["issue_id"] == "#127"

    def test_detail_keyword_search_combined_with_pagination(self, _client):
        """搜索 + 分页组合：keyword 过滤后分页"""
        _seed_agent_types()
        rk = "gitcode:org/agkw4"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            for i in range(8):
                c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq, issue_id) VALUES (?,?,?,?,?,?,?,?)",
                          (rk, "a2a_event", "coding", f"search-match-{i}", "{}", "completed", 600 + i, f"#{i}"))
            for i in range(5):
                c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                          (rk, "a2a_event", "coding", f"other-{i}", "{}", "completed", 700 + i))
            db.commit()
        r = _client.get("/api/agents/coding/org/agkw4?keyword=search&page=1&page_size=5")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 8
        assert len(data["recent_events"]) == 5
        # 第二页
        r2 = _client.get("/api/agents/coding/org/agkw4?keyword=search&page=2&page_size=5")
        data2 = r2.json()
        assert len(data2["recent_events"]) == 3

    def test_detail_keyword_no_result(self, _client):
        """keyword 无匹配时返回空列表 + total=0"""
        _seed_agent_types()
        rk = "gitcode:org/agkw5"
        _seed_session(rk, "coding")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status, seq) VALUES (?,?,?,?,?,?,?)",
                      (rk, "a2a_event", "coding", "visible", "{}", "completed", 800))
            db.commit()
        r = _client.get("/api/agents/coding/org/agkw5?keyword=nonexistent")
        assert r.status_code == 200
        data = r.json()
        assert data["events_total"] == 0
        assert len(data["recent_events"]) == 0


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

    def test_vod_unbind_clears_im_only(self, _client):
        """Issue #294：VOD 解绑（im_unbind）→ IM 三字段清空 + 释放注册表 + 删群会话，引擎/技能保留。"""
        _seed_agent_types()
        rid = _seed_engine()
        app_id = "cli_vodub_%s" % uuid.uuid4().hex[:6]
        rk = "gitcode:org/agunb1"
        sid = _seed_session(rk, "vod", im_app_id=app_id)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_agent_session SET im_app_secret='sec', im_provider='feishu', runtime_id=?, skill='askill', skill_id=7 WHERE id=?",
                      (rid, sid))
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?, 'vod_bot')", (app_id,))
            c.execute("INSERT INTO vod_conversations (agent_session_id, im_chat_id, status) VALUES (?, 'oc_unb', 'active')", (sid,))
            db.commit()
        bm = MagicMock()
        bm.api = MagicMock()
        bm.remove_bot = AsyncMock()
        bm.add_bot = AsyncMock()
        try:
            with patch("src.api.main.bot_manager", bm):
                r = _client.put("/api/agents/vod/org/agunb1", json={"im_unbind": True})
            assert r.status_code == 200, r.text
            assert r.json()["session_id"] == sid
            bm.remove_bot.assert_awaited_once_with(app_id)
            bm.add_bot.assert_not_awaited()
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT im_app_id, im_app_secret, im_provider, runtime_id, skill_id FROM devops_agent_session WHERE id=?", (sid,)).fetchone()
                reg = c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (app_id,)).fetchone()[0]
                conv = c.execute("SELECT COUNT(*) FROM vod_conversations WHERE agent_session_id=?", (sid,)).fetchone()[0]
            assert tuple(row[:3]) == ("", "", ""), "解绑后 im_app_id/secret/provider 应为空（显式传空串生效）"
            assert row[3] == rid and row[4] == 7, "解绑不应重置引擎/技能配置"
            assert reg == 0, "解绑后 vod_bot 注册行应被删除（无其他引用）"
            assert conv == 0, "解绑后该仓库群会话应被清理"
            # 解绑后同一凭证可重新绑定
            with patch("src.api.main.bot_manager", bm):
                r2 = _client.put("/api/agents/vod/org/agunb1", json={"im_app_id": app_id, "im_app_secret": "sec"})
            assert r2.status_code == 200, r2.text
            with db_lock:
                c = db.cursor()
                row2 = c.execute("SELECT im_app_id, im_provider FROM devops_agent_session WHERE id=?", (sid,)).fetchone()
                reg2 = c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=?", (app_id,)).fetchone()
            assert tuple(row2) == (app_id, "feishu"), "解绑后应可重新绑定同一凭证"
            assert reg2 and reg2[0] == "vod_bot"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_id,))
                c.execute("DELETE FROM vod_conversations WHERE agent_session_id=?", (sid,))
                c.execute("DELETE FROM devops_agent_session WHERE id=?", (sid,))
                db.commit()

    def test_vod_plain_save_keeps_im(self, _client):
        """Issue #294：不带 IM 字段/解绑标记的普通保存不得误清既有 IM 绑定。"""
        _seed_agent_types()
        _seed_engine()
        app_id = "cli_vodkeep_%s" % uuid.uuid4().hex[:6]
        rk = "gitcode:org/agkeep1"
        sid = _seed_session(rk, "vod", im_app_id=app_id)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_agent_session SET im_app_secret='sec', im_provider='feishu' WHERE id=?", (sid,))
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?, 'vod_bot')", (app_id,))
            db.commit()
        try:
            r = _client.put("/api/agents/vod/org/agkeep1", json={"backends": ["ag_rt"]})
            assert r.status_code == 200, r.text
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT im_app_id, im_app_secret, im_provider FROM devops_agent_session WHERE id=?", (sid,)).fetchone()
                reg = c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (app_id,)).fetchone()[0]
            assert tuple(row) == (app_id, "sec", "feishu"), "普通保存不应清空既有 IM 绑定"
            assert reg == 1, "普通保存不应删除注册表行"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_id,))
                c.execute("DELETE FROM devops_agent_session WHERE id=?", (sid,))
                db.commit()