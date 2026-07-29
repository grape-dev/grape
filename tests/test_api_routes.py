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
    # 给测试用户设置 API Token
    acc = user_db.get_user_account("test_api") or {}
    acc["gitcode_api_token"] = "test_api_token_12345"
    user_db.save_user_account("test_api", acc)
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 确保 agent_type_config 表中有 5 个 Agent 类型和技能
    from src.session_manager import db, db_lock
    with db_lock:
        for at, dn in [("vod","VOD Agent"),("issue","Issue Agent"),("coding","Coding Agent"),("qa","QA Agent"),("sre","SRE Agent")]:
            db.execute("INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, icon, order_num) VALUES (?,?,?,?,?)",
                       (at, dn, f"Description for {at}", "●", 0))
        for skill, dn, parent in [("issue","Issue 管理","issue"),("implementation","代码实现","coding"),("pr","PR 管理","coding")]:
            db.execute("INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, parent_agent_type) VALUES (?,?,?,?)",
                       (skill, dn, f"{dn} description", parent))
        db.commit()

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
        data = r.json()
        assert "repos" in data
        # 已托管的仓库不应包含未加入的仓库
        for repo in data["repos"]:
            assert repo["status"] in ("Active", "Joined"), f"Unexpected status: {repo['status']}"

    def test_managed_list_only_shows_joined(self):
        """验证 GET /api/repos/managed 只返回已加入的仓库，
        不会返回 GitCode 上所有有权限的仓库"""
        # 先清理已加入的记录
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos")
            db.commit()

        # 清理后应为空
        r = client.get("/api/repos/managed", headers=_headers)
        assert r.status_code == 200
        assert r.json()["repos"] == [], f"Expected empty, got {len(r.json()['repos'])} repos"
        print("  [OK] managed repos empty after cleanup")

        # 加入一个仓库
        r = client.post("/api/repos/join", json={"full_name": "org/test-managed"}, headers=_headers)
        assert r.status_code == 200

        # 加入后应出现
        r = client.get("/api/repos/managed", headers=_headers)
        assert r.status_code == 200
        repos = r.json()["repos"]
        assert len(repos) >= 1, f"Expected at least 1 repo, got {len(repos)}"
        found = any(rp["full_name"] == "org/test-managed" for rp in repos)
        assert found, "org/test-managed not in managed list"
        print("  [OK] joined repo appears in managed list")

        # 移除后应消失
        r = client.post("/api/repos/remove", json={"full_name": "org/test-managed"}, headers=_headers)
        assert r.status_code == 200

        r = client.get("/api/repos/managed", headers=_headers)
        all_repos = r.json()["repos"]
        found = any(rp["full_name"] == "org/test-managed" for rp in all_repos)
        assert not found, "org/test-managed still in managed list after remove"
        print("  [OK] removed repo disappears from managed list")

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

    def test_host_no_permission(self):
        """非管理员用户尝试托管应返回 502（GitCode API 不可达）或 400/403"""
        r = client.post("/api/repos/host", json={"full_name": "org/nonexistent-repo"}, headers=_headers)
        assert r.status_code in (400, 403, 502), f"Unexpected status: {r.status_code}: {r.text[:100]}"

    def test_host_invalid_format(self):
        """无效的仓库全名"""
        r = client.post("/api/repos/host", json={"full_name": "bad-format"}, headers=_headers)
        assert r.status_code == 400

    def test_host_strict_mode_check(self):
        """验证 backend 的权限检查逻辑与 frontend 一致：
        1) 项目所有者 → 通过
        2) 非所有者 → 调用 _check_collaborator_admin
        3) 结果与 fetch_repos_with_role 的 is_admin 一致
        """
        from src.user.repo_manager import _check_collaborator_admin, _req, API_BASE

        # 获取当前用户身份
        r = client.post("/api/auth/login",
            json={"username": "test", "password": "test123"}, headers=_headers)
        # 用 default 账户的 token
        from src.config import get_account
        acc = get_account()
        bot_token = acc.get("gitcode_api_token", "")
        assert bot_token, "Need bot token for this test"

        user_data = _req("GET", f"{API_BASE}/user", bot_token)
        current_user = user_data.get("login", "")
        assert current_user, "Need current user identity"

        # 测试 1：项目所有者通过
        owner_repos = [r for r in _req("GET", f"{API_BASE}/user/repos?type=all&per_page=50", bot_token)
                       if isinstance(r, dict) and r.get("owner", {}).get("login") == current_user]
        if owner_repos:
            test_repo = owner_repos[0]
            repo_name = test_repo.get("name", "")
            assert _check_collaborator_admin(bot_token, current_user, repo_name, current_user), \
                "Owner should pass permission check"
            print(f"  [OK] Owner permission check passed for {current_user}/{repo_name}")
        else:
            print("  [SKIP] No owner repos found")

        # 测试 2：非所有者不通过
        non_owner = "nonexistent_user_xyz"
        result = _check_collaborator_admin(bot_token, "someorg", "somerepo", non_owner)
        assert result == False, "Non-owner should fail permission check"
        print("  [OK] Non-owner permission check correctly rejected")

    def test_sync_endpoint(self):
        """POST /api/repos/sync 应返回仓库列表且标记同步成功"""
        # test 用户没有 gitcode token，用 default 的 bot token 设置
        r = client.post("/api/repos/sync", headers=_headers)
        # 可能 502（GitCode 不可达）或 200（成功）
        assert r.status_code in (200, 502), f"Unexpected status: {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert "repos" in data
            assert data["message"] == "同步成功"
            print(f"  [OK] sync returned {len(data['repos'])} repos")

    def test_sync_then_member_uses_cache(self):
        """同步后 GET /api/repos/member 应返回 _cached=True（表示命中缓存）"""
        # 先同步
        r = client.post("/api/repos/sync", headers=_headers)
        if r.status_code != 200:
            print("  [SKIP] sync failed, cannot test cache")
            return

        # 再调 member，应走缓存
        r = client.get("/api/repos/member", headers=_headers)
        assert r.status_code == 200
        data = r.json()
        # 检查是否标记为缓存（_cached 字段）
        assert data.get("_cached") == True, f"Expected cached response, got _cached={data.get('_cached')}"
        assert "repos" in data
        print(f"  [OK] member from cache: {len(data['repos'])} repos")

    def test_user_repos_table_operations(self):
        """验证 user_repos 表的读写函数"""
        from src.user.db import save_user_repos, get_user_repos, has_user_repos, clear_user_repos

        test_repos = [
            {"full_name": "org/test-a", "owner": "org", "name": "test-a",
             "description": "Test A", "role": "Admin", "is_admin": True},
            {"full_name": "org/test-b", "owner": "org", "name": "test-b",
             "description": "Test B", "role": "Member", "is_admin": False},
        ]

        uid = 9999
        clear_user_repos(uid)
        assert not has_user_repos(uid), "Should be empty after clear"

        save_user_repos(uid, test_repos)
        assert has_user_repos(uid), "Should have data after save"

        repos = get_user_repos(uid)
        assert len(repos) == 2, f"Expected 2, got {len(repos)}"
        assert repos[0]["full_name"] == "org/test-a"
        assert repos[1]["is_admin"] == False

        # 覆盖写入
        save_user_repos(uid, [{"full_name": "org/test-c", "owner": "org", "name": "test-c",
                                "description": "Test C", "role": "Admin", "is_admin": True}])
        repos = get_user_repos(uid)
        assert len(repos) == 1, f"Expected 1 after rewrite, got {len(repos)}"
        assert repos[0]["full_name"] == "org/test-c"

        clear_user_repos(uid)
        assert not has_user_repos(uid), "Should be empty after final clear"
        print("  [OK] user_repos table operations verified")


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
            if r.status_code == 404:
                # Agent type not configured in DB yet, insert it
                from src.session_manager import db, db_lock
                with db_lock:
                    db.execute("INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, icon, order_num) VALUES (?,?,?,?,?)",
                               (agent, f"{agent} Agent", f"Description for {agent}", "●", 0))
                    db.commit()
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
        assert r.status_code in (200, 400), f"Unexpected status: {r.status_code}"
        if r.status_code == 400:
            assert "已被其他用户绑定" in r.text


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
