"""API 路由集成测试 — 覆盖所有 13 个路由模块"""
import sys, os, json, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock

client = TestClient(app)

# ====== Test Setup ======
_uid = None
_token = ""
_headers = {}

def setup_module():
    global _uid, _token, _headers
    _uid = user_db.create_user("test_api", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_api")
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}

def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])


# ===================== 1. Health & SPA =====================
class TestInfra:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_spa_index(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "Agentic DevOps" in r.text

    def test_spa_static_files(self):
        for f in ["/static/js/app.js", "/static/css/app.css", "/static/js/api.js",
                   "/static/js/pages/dashboard.js", "/static/js/pages/agents.js",
                   "/static/js/pages/backends.js", "/static/js/pages/chat.js"]:
            r = client.get(f)
            assert r.status_code == 200, f"{f} -> {r.status_code}"
            assert len(r.content) > 100, f"{f} too short"


# ===================== 2. Auth =====================
class TestAuth:
    def test_login_success(self):
        r = client.post("/api/auth/login", json={"username": "test_api", "password": "test123"})
        assert r.status_code == 200
        assert "token" in r.json()
        assert r.json()["user"]["username"] == "test_api"

    def test_login_fail(self):
        r = client.post("/api/auth/login", json={"username": "test_api", "password": "wrong"})
        assert r.status_code == 401

    def test_login_missing_fields(self):
        r = client.post("/api/auth/login", json={})
        assert r.status_code == 422

    def test_profile_with_auth(self):
        r = client.get("/api/auth/profile", headers=_headers)
        assert r.status_code == 200
        assert r.json()["username"] == "test_api"

    def test_profile_no_auth(self):
        r = client.get("/api/auth/profile")
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == 401

    def test_profile_fake_token(self):
        r = client.get("/api/auth/profile", headers={"Authorization": "Bearer fake_token_xxx"})
        assert r.status_code == 401

    def test_logout(self):
        global _token, _headers
        r = client.post("/api/auth/logout", headers=_headers)
        assert r.status_code == 200
        r2 = client.get("/api/auth/profile", headers=_headers)
        assert r2.status_code == 401
        _token = user_db.create_session_token(_uid["id"])
        _headers = {"Authorization": f"Bearer {_token}"}

    def test_oauth_gitcode_url(self):
        r = client.get("/api/auth/login/gitcode")
        # May return 400 if OAuth not configured, but should not crash
        assert r.status_code in (200, 400)


# ===================== 3. Repos =====================
class TestRepos:
    def test_member_list(self):
        r = client.get("/api/repos/member", headers=_headers)
        assert r.status_code in (200, 502)  # 502 if GitCode API unreachable
        if r.status_code == 200:
            assert "repos" in r.json()

    def test_managed_list(self):
        r = client.get("/api/repos/managed", headers=_headers)
        assert r.status_code == 200
        assert "repos" in r.json()

    def test_join_invalid(self):
        r = client.post("/api/repos/join", json={"full_name": "bad"}, headers=_headers)
        assert r.status_code == 400

    def test_remove_nonexistent(self):
        r = client.post("/api/repos/remove", json={"full_name": "org/nonexistent"}, headers=_headers)
        assert r.status_code == 404

    def test_join_then_remove(self):
        r = client.post("/api/repos/join", json={"full_name": "org/test-repo"}, headers=_headers)
        assert r.status_code == 200
        r = client.post("/api/repos/remove", json={"full_name": "org/test-repo"}, headers=_headers)
        assert r.status_code == 200
        # Verify removed
        r = client.post("/api/repos/remove", json={"full_name": "org/test-repo"}, headers=_headers)
        assert r.status_code == 404


# ===================== 4. Dashboard =====================
class TestDashboard:
    def test_dashboard_success(self):
        r = client.get("/api/dashboard/org/test", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "metrics" in d
        assert "agents" in d
        assert len(d["agents"]) == 5  # VOD/Issue/Coding/QA/SRE
        assert "activity" in d


# ===================== 5. Pipeline =====================
class TestPipeline:
    def test_pipeline_success(self):
        r = client.get("/api/pipeline/org/test", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "active" in d
        assert "history" in d
        assert "active_count" in d
        assert "today_count" in d


# ===================== 6. Agents =====================
class TestAgents:
    def test_agent_detail_known(self):
        for agent in ["vod", "issue", "coding", "qa", "sre"]:
            r = client.get(f"/api/agents/{agent}/org/test", headers=_headers)
            assert r.status_code == 200, f"{agent} -> {r.status_code}"
            d = r.json()
            assert "display_name" in d
            assert "status" in d
            assert "metrics" in d

    def test_agent_detail_unknown(self):
        r = client.get("/api/agents/unknown/org/test", headers=_headers)
        assert r.status_code == 404

    def test_agent_config(self):
        r = client.put("/api/agents/vod/org/test", json={"backend": "opencode"}, headers=_headers)
        assert r.status_code == 200


# ===================== 7. Events =====================
class TestEvents:
    def test_event_list(self):
        r = client.get("/api/events/org/test", headers=_headers)
        assert r.status_code == 200
        assert "events" in r.json()

    def test_event_list_filter(self):
        r = client.get("/api/events/org/test?status=completed", headers=_headers)
        assert r.status_code == 200

    def test_event_detail_nonexistent(self):
        r = client.get("/api/events/detail/999999", headers=_headers)
        assert r.status_code == 404

    def test_event_detail_and_feedback(self):
        # Insert a test event
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status) VALUES (?,?,?,?,?,?)",
                      ("org/test", "push", "vod", "Test event", "{}", "completed"))
            seq = c.lastrowid
            db.commit()
        r = client.get(f"/api/events/detail/{seq}", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["event_type"] == "push"
        assert d["agent_type"] == "vod"
        # Test feedback
        r = client.post("/api/chat/feedback", json={"seq": seq, "feedback_type": "create_issue", "comment": "test"}, headers=_headers)
        assert r.status_code == 200
        assert "反馈" in r.json()["message"]
        # Cleanup
        with db_lock:
            db.execute("DELETE FROM events WHERE seq=?", (seq,))
            db.commit()


# ===================== 8. Chat =====================
class TestChat:
    def test_send(self):
        r = client.post("/api/chat/send", json={"message": "Hello", "repo_key": "org/test"}, headers=_headers)
        assert r.status_code == 200
        assert "reply" in r.json()

    def test_send_stream(self):
        r = client.post("/api/chat/send/stream", json={"message": "Hi", "repo_key": "org/test"}, headers=_headers)
        assert r.status_code == 200
        # Should be SSE format
        assert "data: " in r.text

    def test_config_get(self):
        r = client.get("/api/chat/config", headers=_headers)
        assert r.status_code == 200
        assert "backend" in r.json()
        assert "im" in r.json()

    def test_config_put(self):
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "test", "app_secret": "test"}}}, headers=_headers)
        assert r.status_code == 200


# ===================== 9. Settings =====================
class TestSettings:
    def test_credentials(self):
        r = client.get("/api/settings/credentials", headers=_headers)
        assert r.status_code == 200
        assert "gitcode" in r.json()

    def test_update_credentials(self):
        r = client.put("/api/settings/credentials", json={"huawei_cloud_ak": "test_ak"}, headers=_headers)
        assert r.status_code == 200

    def test_notifications(self):
        r = client.get("/api/settings/notifications", headers=_headers)
        assert r.status_code == 200

    def test_gitcode_repo(self):
        r = client.get("/api/settings/gitcode-repo/owner/repo", headers=_headers)
        assert r.status_code == 200

    def test_gitcode_repo_update(self):
        r = client.put("/api/settings/gitcode-repo/owner/repo",
                       json={"bot_token": "test", "webhook_token": "test"}, headers=_headers)
        assert r.status_code == 200

    def test_default_bot_token(self):
        r = client.get("/api/settings/gitcode-repo/default", headers=_headers)
        assert r.status_code == 200

    def test_backend_agents(self):
        r = client.get("/api/settings/backend-agents", headers=_headers)
        assert r.status_code == 200
        assert "backends" in r.json()


# ===================== 10. Skills =====================
class TestSkills:
    def test_skills_list(self):
        r = client.get("/api/skills/", headers=_headers)
        assert r.status_code == 200
        assert "skills" in r.json()
        assert len(r.json()["skills"]) > 0

    def test_skills_upload_too_big(self):
        r = client.post("/api/skills/upload",
                        files={"file": ("big.zip", b"x" * 11 * 1024 * 1024)},
                        headers=_headers)
        assert r.status_code == 400

    def test_skills_upload_empty(self):
        import tempfile, zipfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("empty/", "")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("empty.zip", f.read())}, headers=_headers)
        os.unlink(tmp.name)
        # Should validate and fail (no SKILL.md)
        assert r.status_code == 400

    def test_skills_delete_nonexistent(self):
        r = client.delete("/api/skills/nonexistent_skill", headers=_headers)
        assert r.status_code == 404


# ===================== 11. Search =====================
class TestSearch:
    def test_search_empty(self):
        r = client.get("/api/search/?q=test&type=all", headers=_headers)
        assert r.status_code == 200
        assert "events" in r.json()
        assert "repos" in r.json()
        assert "agents" in r.json()

    def test_search_no_query(self):
        # q is required by pattern: min_length=1, so empty gives 422
        r = client.get("/api/search/", headers=_headers)
        assert r.status_code == 422


# ===================== 12. Webhook & A2A =====================
class TestWebhook:
    def test_webhook_push(self):
        from src.config import GITCODE_WEBHOOK_TOKEN
        r = client.post("/webhook/gitcode", content=b'{}',
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": GITCODE_WEBHOOK_TOKEN})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_webhook_invalid_token(self):
        r = client.post("/webhook/gitcode", content=b'{}',
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": "wrong_token"})
        assert r.status_code == 403

    def test_a2a_reject_external(self):
        """A2A should reject requests from non-localhost IPs"""
        r = client.post("/api/a2a", json={"agent": "issue", "payload": {"repository": "org/test"}})
        assert r.status_code == 403

    def test_a2a_missing_agent(self):
        r = client.post("/api/a2a", json={}, headers=_headers)
        assert r.status_code == 400

    def test_a2a_missing_repo(self):
        r = client.post("/api/a2a", json={"agent": "issue", "payload": {}}, headers=_headers)
        assert r.status_code == 400


# ===================== 13. Token Three-Layer =====================
class TestTokenLayers:
    def test_bot_token_fallback(self):
        from src.api.deps import get_bot_token
        token = get_bot_token("nonexistent_user")
        # Should return empty string or default
        assert isinstance(token, str)

    def test_webhook_token_fallback(self):
        from src.api.deps import get_webhook_token
        token = get_webhook_token("org", "nonexistent")
        assert isinstance(token, str)


# ===================== 14. Event Pipeline (unit) =====================
class TestEventPipeline:
    def test_append_and_query(self):
        from src.api.event_store import append_event, get_events, get_event_count
        from src.session_manager import db, db_lock
        repo = "gitcode:test/pipeline"
        with db_lock:
            db.execute("DELETE FROM events WHERE repo_key=?", (repo,))
            db.commit()
        s1 = append_event(repo, "test_type", "Test", issue_id="#1", agent_type="vod", status="completed")
        assert s1 > 0
        assert get_event_count(repo) == 1
        evts = get_events(repo, limit=10)
        assert len(evts) == 1
        assert evts[0]["event_type"] == "test_type"
        with db_lock:
            db.execute("DELETE FROM events WHERE repo_key=?", (repo,))
            db.commit()

    def test_issue_summary(self):
        from src.api.event_store import append_event, get_issue_summary
        from src.session_manager import db, db_lock
        repo = "gitcode:test/summary"
        with db_lock:
            db.execute("DELETE FROM events WHERE repo_key=?", (repo,))
            db.commit()
        append_event(repo, "push", "Push event", issue_id="#42", agent_type="vod", status="completed")
        append_event(repo, "pr_open", "PR", issue_id="#42", agent_type="coding", status="running")
        summary = get_issue_summary(repo)
        assert "#42" in summary
        assert "vod" in summary or "coding" in summary
        with db_lock:
            db.execute("DELETE FROM events WHERE repo_key=?", (repo,))
            db.commit()

    def test_routing_db(self):
        from src.api.routing import get_domain_skill, get_agent_skill_map
        skill = get_domain_skill("issue_opened")
        assert skill == "issue" or skill != ""
        sm = get_agent_skill_map()
        assert len(sm) > 0

    def test_session_independent(self):
        from src.api.session_store import get_or_create_agent_session, delete_agent_session
        repo = "gitcode:test/sess"
        delete_agent_session(repo, "vod"); delete_agent_session(repo, "coding")
        sv = get_or_create_agent_session(repo, "vod", "opencode")
        assert sv["new"]
        sc = get_or_create_agent_session(repo, "coding", "opencode")
        assert sc["id"] != sv["id"]  # Independent sessions
        delete_agent_session(repo, "vod"); delete_agent_session(repo, "coding")
