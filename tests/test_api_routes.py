"""API 路由集成测试 — 覆盖所有 13 个路由模块"""
import sys, os, json, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 强制使用独立测试库，防止 TestClient 误连生产库（双保险，即使 conftest 未加载）
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

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
    # 确保 agent_type 表中有 5 个 Agent 类型和技能
    from src.session_manager import db, db_lock
    with db_lock:
        # 订阅配置与 configs/agent-description.json 保持一致，避免污染语义
        subscribe_map = {
            "vod": '["vod_signal"]',
            "issue": '["issue_opened","issue_updated","issue_labeled","issue_comment"]',
            "coding": '["pull_request_opened","pull_request_updated","note_hook_pr"]',
            "qa": "[]",
            "sre": '["pull_request_merged","build_failure","deploy_start","deploy_failure","release_created"]',
        }
        for at, dn, ord_num in [("vod","VOD Agent",1),("issue","Issue Agent",2),("coding","Coding Agent",3),("qa","QA Agent",4),("sre","SRE Agent",5)]:
            db.execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?)",
                       (at, dn, f"Description for {at}", "●", ord_num, subscribe_map.get(at, "[]")))
        for skill, dn, parent in [("issue","Issue 管理","issue"),("implementation","代码实现","coding"),("pr","PR 管理","qa")]:
            db.execute("INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope) VALUES (?,?,?,?,?,'official')",
                       (skill, dn, f"{dn} description", parent, skill))
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
        assert "Grape" in r.text

    def test_spa_static_files(self):
        for f in ["/static/js/app.js", "/static/css/app.css", "/static/js/api.js",
                   "/static/js/pages/dashboard.js", "/static/js/pages/agents.js",
                   "/static/js/pages/runtimes.js", "/static/js/pages/chat.js"]:
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
        # nickname 字段存在（无 OAuth 昵称时回退 username）
        assert r.json()["nickname"] == "test_api"

    def test_profile_nickname_roundtrip(self):
        """save_user_account 写 nickname 后 profile 返回该昵称"""
        # 给测试用户设置一个昵称
        acc = user_db.get_user_account("test_api") or {}
        acc["nickname"] = "测试昵称"
        user_db.save_user_account("test_api", acc)
        try:
            r = client.get("/api/auth/profile", headers=_headers)
            assert r.status_code == 200
            assert r.json()["nickname"] == "测试昵称"
        finally:
            acc2 = user_db.get_user_account("test_api") or {}
            acc2["nickname"] = ""
            user_db.save_user_account("test_api", acc2)

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
        """验证 backend 的权限检查逻辑与 frontend 一致（mock GitCode API，不依赖真实账号 token）：
        1) 项目所有者 → 直接通过（无需网络）
        2) 非所有者 → 按 /collaborators/{user}/permission 的 admin 权限判定
        """
        from src.user.repo_manager import _check_collaborator_admin
        from unittest.mock import patch

        # 1) 所有者直接通过，不触发网络调用
        with patch("src.user.repo_manager._req") as mock_req:
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "owner_user") is True
            mock_req.assert_not_called()

        # 2) 非所有者：permission.admin=True → 通过
        with patch("src.user.repo_manager._req", return_value={"permissions": {"admin": True}}):
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "somebody") is True

        # 3) 非所有者：permission.admin=False → 拒绝
        with patch("src.user.repo_manager._req", return_value={"permissions": {"admin": False}}):
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "somebody") is False

        # 4) 空用户 / API 异常 → 拒绝
        assert _check_collaborator_admin("tok", "owner_user", "repo1", "") is False
        with patch("src.user.repo_manager._req", side_effect=RuntimeError("boom")):
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "somebody") is False

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

    def test_user_repos_synced_at(self):
        """save_user_repos 后 synced_at 应写入时间戳，get_user_repos_synced_at 应能读取"""
        from src.user.db import save_user_repos, clear_user_repos, get_user_repos_synced_at
        uid = 10001
        clear_user_repos(uid)
        assert get_user_repos_synced_at(uid) == "", "No cache -> empty synced_at"
        save_user_repos(uid, [{"full_name": "org/ttl-a", "owner": "org", "name": "ttl-a",
                               "description": "", "role": "Admin", "is_admin": True}])
        ts = get_user_repos_synced_at(uid)
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", ts), f"bad synced_at: {ts!r}"
        clear_user_repos(uid)


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

    def test_pipeline_queries_prefixed_repo_key(self):
        """get_pipeline 应按带前缀 repo_key 查 events（repo_key 整改后 events 值带 gitcode: 前缀）"""
        from src.session_manager import db, db_lock
        repo_key = "gitcode:pipeline/repo-prefix"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
            c.execute("INSERT INTO events (repo_key, event_type, agent_type, issue_id, title, summary, status, payload) VALUES (?,?,?,?,?,?,?,?)",
                      (repo_key, "issue_opened", "issue", "#1", "pipeline-test", "s", "completed", '{}'))
            db.commit()
        try:
            r = client.get("/api/pipeline/pipeline/repo-prefix", headers=_headers)
            assert r.status_code == 200
            d = r.json()
            # 带前缀 events 应被查中 → 生成 pipeline
            assert d["active"] is not None, f"应命中带前缀 events，active 不应为空: {d}"
            assert d["active"]["issue_id"] == "#1"
            # 返回给前端的 repo 无前缀（前端拼 GitCode 链接）
            assert d["repo"] == "pipeline/repo-prefix"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
                db.commit()


# ===================== 6. Agents =====================
class TestAgents:
    def test_agent_detail_known(self):
        for agent in ["vod", "issue", "coding", "qa", "sre"]:
            r = client.get(f"/api/agents/{agent}/org/test", headers=_headers)
            if r.status_code == 404:
                # Agent type not configured in DB yet, insert it
                from src.session_manager import db, db_lock
                with db_lock:
                    db.execute("INSERT OR IGNORE INTO agent_type (agent_type, display_name, description, icon, order_num) VALUES (?,?,?,?,?)",
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

    def test_agent_config_repo_skill(self):
        """DevOps Agent 保存仓库技能时 skill_id 正确解析（仓库级 skill 配置）"""
        import uuid
        from src.session_manager import db, db_lock

        uniq = uuid.uuid4().hex[:8]
        skill_name = f"agentskill{uniq}"
        repo_key = f"gitcode:org/agent-repo-{uniq}"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"agentskill{uniq}%",))
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
            # 仓库技能（agent_type='sre'）
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, "agent-skill", "d", "sre", skill_name, "repo", repo_key))
            db.commit()
        try:
            owner, repo = repo_key.split("/", 1)
            r = client.put(f"/api/agents/sre/{owner}/{repo}", json={"backend": "opencode", "skill": skill_name}, headers=_headers)
            assert r.status_code == 200, f"save repo skill failed: {r.text[:200]}"
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'", (repo_key,)).fetchone()
            assert row and row[0] == skill_name, f"skill not saved: {row}"
            assert row and row[1] and row[1] > 0, f"repo skill_id should be resolved, got {row}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"agentskill{uniq}%",))
                c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
                db.commit()


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
            assert "已被其他用户" in r.text or "已被 VOD Agent" in r.text

    def test_config_put_default_backend_no_ghost(self):
        """前端回退 '<option>Default</option>' 提交 backend='Default'：不得创建 default-cred0 幽灵 runtime，
        GET /chat/config 应回退 opencode（见 TestGhostEnginePrevention 根因说明）。"""
        import uuid as _uuid
        # 自给自足：确保存在健康本机 opencode 引擎（回退目标），避免依赖共享库残留状态
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name) != 'opencode'")
            row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode'").fetchone()
            if row:
                c.execute("UPDATE agent_runtimes SET installed=1, healthy=1, status='online' WHERE id=?", (row[0],))
            else:
                c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                          "VALUES ('opencode','opencode',0,1,1,'online','global','')")
            db.commit()
        user = user_db.create_user("chat_def_" + _uuid.uuid4().hex[:6], "pw123")
        assert user
        tok = user_db.create_session_token(user["id"])
        hd = {"Authorization": f"Bearer {tok}"}
        # 清理该用户历史 chat 配置，保证干净
        with db_lock:
            db.cursor().execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (user["id"],))
            db.commit()
        before = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='default'").fetchone()[0]
        r = client.put("/api/chat/config", json={"backend": "Default"}, headers=hd)
        assert r.status_code == 200, f"save failed: {r.text[:200]}"
        with db_lock:
            after = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='default'").fetchone()[0]
        assert after == before, "backend='Default' 不应创建 default-cred0 幽灵 runtime 行"
        cfg = client.get("/api/chat/config", headers=hd).json()
        assert cfg["backend"]["type"] == "opencode"
        with db_lock:
            db.cursor().execute("DELETE FROM chat_agent_session WHERE user_id=?", (user["id"],))
            db.commit()
        user_db.delete_user(user["id"])

    def test_chat_im_user_isolation(self):
        """Chat IM 配置每用户独立：A 配置后，B 的 GET /chat/config 应为空 IM"""
        import uuid
        from src.session_manager import db, db_lock

        app_a = "cli_" + uuid.uuid4().hex[:12]
        # 清理测试用户配置，保证干净
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id IN (?,?) AND repo_key=''", (_uid["id"], 7777))
            db.commit()

        # 用户 A（当前登录）配置 IM
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_a, "app_secret": "sec_a"}}}, headers=_headers)
        assert r.status_code == 200, f"A save failed: {r.text[:200]}"

        # 用户 A 自己能读到
        r = client.get("/api/chat/config", headers=_headers)
        assert r.status_code == 200
        assert r.json()["im"]["feishu"]["app_id"] == app_a

        # 用户 B（新用户，无配置）读不到 A 的 app_id
        user_b = user_db.get_user_by_username("chat_iso_b") or user_db.create_user("chat_iso_b", "pw123")
        token_b = user_db.create_session_token(user_b["id"])
        headers_b = {"Authorization": f"Bearer {token_b}"}
        r = client.get("/api/chat/config", headers=headers_b)
        assert r.status_code == 200
        feishu = r.json()["im"]["feishu"]
        assert feishu["app_id"] == "", f"user B should see empty app_id, got {feishu['app_id']!r}"
        assert feishu["app_secret_exists"] is False

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE im_app_id=? OR user_id IN (?,?)", (app_a, _uid["id"], user_b["id"]))
            c.execute("DELETE FROM user_accounts WHERE id=?", (user_b["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_a,))
            db.commit()

    def test_chat_im_app_id_global_unique(self):
        """强约束：同一 app_id 被其他用户保存时应 400"""
        import uuid
        from src.session_manager import db, db_lock

        app_id = "cli_" + uuid.uuid4().hex[:12]

        # 用户 A 绑定该 app_id
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec_b"}}}, headers=_headers)
        assert r.status_code == 200, f"A save failed: {r.text[:200]}"

        # 用户 B 尝试绑定同一 app_id → 400
        user_b = user_db.get_user_by_username("chat_uniq_b") or user_db.create_user("chat_uniq_b", "pw123")
        token_b = user_db.create_session_token(user_b["id"])
        headers_b = {"Authorization": f"Bearer {token_b}"}
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec_x"}}}, headers=headers_b)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:200]}"
        assert "已被其他用户" in r.text

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE im_app_id=? OR user_id IN (?,?)", (app_id, _uid["id"], user_b["id"]))
            c.execute("DELETE FROM user_accounts WHERE id=?", (user_b["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_id,))
            db.commit()

    def test_chat_im_vod_conflict(self):
        """强约束：app_id 已被 VOD Agent 占用时，Chat 保存应 400"""
        import uuid
        from src.session_manager import db, db_lock

        app_id = "cli_" + uuid.uuid4().hex[:12]
        # 登记一个 vod_bot
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?, 'vod_bot')", (app_id,))
            db.commit()

        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec"}}}, headers=_headers)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:200]}"
        assert "已被 VOD Agent 使用" in r.text

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (_uid["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (app_id,))
            db.commit()

    def test_chat_im_release_old_app_id(self):
        """用户更换 app_id 后，旧 app_id 应从 global_im_bot_config 释放"""
        import uuid
        from src.session_manager import db, db_lock

        old_app = "cli_" + uuid.uuid4().hex[:12]
        new_app = "cli_" + uuid.uuid4().hex[:12]

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (_uid["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id IN (?,?)", (old_app, new_app))
            db.commit()

        # 用户先绑定旧 app_id
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": old_app, "app_secret": "sec_old"}}}, headers=_headers)
        assert r.status_code == 200, f"old save failed: {r.text[:200]}"
        with db_lock:
            c = db.cursor()
            assert c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (old_app,)).fetchone()[0] == 1

        # 再绑定新 app_id（换绑）
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": new_app, "app_secret": "sec_new"}}}, headers=_headers)
        assert r.status_code == 200, f"new save failed: {r.text[:200]}"
        with db_lock:
            c = db.cursor()
            assert c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (old_app,)).fetchone()[0] == 0, "old app_id should be released"
            assert c.execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (new_app,)).fetchone()[0] == 1

        # 用户读到新 app_id
        r = client.get("/api/chat/config", headers=_headers)
        assert r.json()["im"]["feishu"]["app_id"] == new_app

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (_uid["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id IN (?,?)", (old_app, new_app))
            db.commit()

    def test_chat_config_skills_include_user_repo(self):
        """Chat GET /config：带 repo 时 skill 列表含官方+个人+仓库；不带 repo 时含官方+个人"""
        import uuid
        from src.session_manager import db, db_lock

        uid = _uid["id"]
        uniq = uuid.uuid4().hex[:8]
        repo_key = f"org/chat-skill-{uniq}"
        # 造一个个人 chat 技能 + 一个仓库 chat 技能（agent_type='chat'）
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatskill{uniq}%",))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (f"chatskill{uniq}-user", f"chat-skill-user-{uniq}", "desc", "chat", f"chatskill{uniq}-user", "user", str(uid)))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (f"chatskill{uniq}-repo", f"chat-skill-repo-{uniq}", "desc", "chat", f"chatskill{uniq}-repo", "repo", repo_key))
            db.commit()

        try:
            # 无 repo：只含官方 + 个人（不含仓库）
            r = client.get("/api/chat/config", headers=_headers)
            assert r.status_code == 200
            names = [s["name"] for s in r.json()["_skills"]]
            assert f"chatskill{uniq}-user" in names, f"user skill should appear without repo, got {names}"
            assert f"chatskill{uniq}-repo" not in names, f"repo skill should NOT appear without repo param, got {names}"

            # 带 repo：含官方 + 个人 + 仓库
            r2 = client.get(f"/api/chat/config?repo={repo_key}", headers=_headers)
            assert r2.status_code == 200
            names2 = [s["name"] for s in r2.json()["_skills"]]
            assert f"chatskill{uniq}-user" in names2, f"user skill should appear with repo, got {names2}"
            assert f"chatskill{uniq}-repo" in names2, f"repo skill should appear with repo param, got {names2}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatskill{uniq}%",))
                db.commit()

    def test_chat_config_put_skill_id_resolution(self):
        """Chat PUT /config 保存个人/仓库技能时 skill_id 正确解析"""
        import uuid
        from src.session_manager import db, db_lock

        uid = _uid["id"]
        uniq = uuid.uuid4().hex[:8]
        repo_key = f"org/chat-skillid-{uniq}"
        user_skill = f"chatsid{uniq}-user"
        repo_skill = f"chatsid{uniq}-repo"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatsid{uniq}%",))
            # 个人 chat 技能 + 仓库 chat 技能
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (user_skill, "u", "d", "chat", user_skill, "user", str(uid)))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (repo_skill, "r", "d", "chat", repo_skill, "repo", repo_key))
            db.commit()

        try:
            # 保存个人技能（无 repo 也能解析 user）
            r = client.put("/api/chat/config", json={"skill": user_skill}, headers=_headers)
            assert r.status_code == 200, f"save user skill failed: {r.text[:200]}"
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT skill, skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,)).fetchone()
            assert row and row[0] == user_skill, f"skill not saved: {row}"
            assert row and row[1] and row[1] > 0, f"skill_id should be resolved, got {row}"

            # 保存仓库技能（带 repo）
            r2 = client.put(f"/api/chat/config?repo={repo_key}", json={"skill": repo_skill}, headers=_headers)
            assert r2.status_code == 200, f"save repo skill failed: {r2.text[:200]}"
            with db_lock:
                c = db.cursor()
                row2 = c.execute("SELECT skill, skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,)).fetchone()
            assert row2 and row2[0] == repo_skill, f"repo skill not saved: {row2}"
            assert row2 and row2[1] and row2[1] > 0, f"repo skill_id should be resolved, got {row2}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatsid{uniq}%",))
                db.commit()

    def test_chat_config_put_skill_scope_precise(self):
        """同名技能时 skill_scope 精确定位（官方/仓库同名选对的那个）"""
        import uuid
        from src.session_manager import db, db_lock

        uid = _uid["id"]
        uniq = uuid.uuid4().hex[:8]
        repo_key = f"org/scope-{uniq}"
        same_name = f"dupname{uniq}"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"dupname{uniq}%",))
            # 同名：一个官方 + 一个仓库
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (same_name, "official-dup", "o", "chat", same_name, "official", ""))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (same_name, "repo-dup", "r", "chat", same_name, "repo", repo_key))
            db.commit()
        try:
            # 不带 skill_scope：repo 优先 → 命中仓库技能
            r = client.put(f"/api/chat/config?repo={repo_key}", json={"skill": same_name}, headers=_headers)
            assert r.status_code == 200, f"save failed: {r.text[:200]}"
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT skill, skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,)).fetchone()
            repo_id = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?", (same_name, repo_key)).fetchone()
            assert row and row[1] == repo_id[0], f"default should resolve repo skill (repo-first), got {row}"

            # 显式 skill_scope=official → 解析官方技能（精确匹配）
            r2 = client.put(f"/api/chat/config?repo={repo_key}", json={"skill": same_name, "skill_scope": "official"}, headers=_headers)
            assert r2.status_code == 200, f"save official failed: {r2.text[:200]}"
            with db_lock:
                c = db.cursor()
                row2 = c.execute("SELECT skill, skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,)).fetchone()
            official_id = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='official'", (same_name,)).fetchone()
            assert row2 and row2[1] == official_id[0], f"skill_scope=official should resolve official skill, got {row2}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"dupname{uniq}%",))
                db.commit()


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
                       json={"bot_token": "test"}, headers=_headers)
        assert r.status_code == 200

    def test_agent_runtimes(self):
        r = client.get("/api/settings/agent-runtimes", headers=_headers)
        assert r.status_code == 200
        assert "runtimes" in r.json()
        # 兼容旧路径 /backend-agents 仍可用
        r_old = client.get("/api/settings/backend-agents", headers=_headers)
        assert r_old.status_code == 200
        assert "backends" in r_old.json()


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
                        data={"agent_type": "vod"},
                        headers=_headers)
        assert r.status_code == 400

    def test_skills_upload_empty(self):
        import tempfile, zipfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("empty/", "")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("empty.zip", f.read())},
                            data={"agent_type": "vod"}, headers=_headers)
        os.unlink(tmp.name)
        # Should validate and fail (no SKILL.md)
        assert r.status_code == 400

    def test_skills_upload_missing_agent_type(self):
        """不传 agent_type 应返回 400"""
        import tempfile, zipfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("skill-x/SKILL.md", "---\nname: skill-x\nversion: 1.0.0\ndescription: test\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("skill-x.zip", f.read())},
                            headers=_headers)
        os.unlink(tmp.name)
        assert r.status_code == 400

    def test_skills_upload_invalid_agent_type(self):
        """非法 agent_type 应返回 400"""
        import tempfile, zipfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("skill-x/SKILL.md", "---\nname: skill-x\nversion: 1.0.0\ndescription: test\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("skill-x.zip", f.read())},
                            data={"agent_type": "unknown-type"}, headers=_headers)
        os.unlink(tmp.name)
        assert r.status_code == 400

    def test_skills_upload_valid_agent_type(self):
        """合法 agent_type（chat 等）上传应成功"""
        import tempfile, zipfile, os, sqlite3
        from src.session_manager import db, db_lock
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("skill-uploadtest/SKILL.md", "---\nname: skill-uploadtest\nversion: 1.0.0\ndescription: test skill\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("skill-uploadtest.zip", f.read())},
                            data={"agent_type": "chat"}, headers=_headers)
        os.unlink(tmp.name)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        # 验证 agent_type 与 description（从 SKILL.md Front Matter 解析）均已保存
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT agent_type, description FROM skill WHERE skill_name='skill-uploadtest' AND scope='user'").fetchone()
        assert row and row[0] == "chat", f"agent_type should be chat, got {row}"
        assert row and row[1] == "test skill", f"description should be parsed from front matter, got {row}"

    def test_skills_upload_no_sections_allowed(self):
        """放宽校验：无 场景/知识/步骤 三章节的自定义技能也应能上传（与官方技能格式一致）"""
        import tempfile, zipfile, os
        from src.session_manager import db, db_lock
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("skill-nosections/SKILL.md", "---\nname: skill-nosections\nversion: 1.0.0\ndescription: 无三章节自定义技能\n---\n\n# 技能说明\n\n处理特定流程。")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("skill-nosections.zip", f.read())},
                            data={"agent_type": "issue"}, headers=_headers)
        os.unlink(tmp.name)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT agent_type FROM skill WHERE skill_name='skill-nosections' AND scope='user'").fetchone()
        assert row and row[0] == "issue", f"agent_type should be issue, got {row}"

    def test_skills_template_download(self):
        """GET /api/skills/template 应返回可用的 ZIP 技能模板"""
        import zipfile, io as _io
        r = client.get("/api/skills/template", headers=_headers)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        assert r.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(_io.BytesIO(r.content)) as z:
            names = z.namelist()
            valid = z.testzip() is None
        assert any(n.endswith("SKILL.md") for n in names), f"模板应包含 SKILL.md, got {names}"
        assert valid

    def test_skills_upload_repo_scope_via_form(self):
        """scope=repo 通过 form 提交时应正确落到仓库技能目录（此前无 Form() 标注会被当成 query 参数而失效）"""
        import tempfile, zipfile, os, shutil
        from src.session_manager import db, db_lock
        from src.user import db as user_db
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("skill-repoupload/SKILL.md", "---\nname: skill-repoupload\nversion: 1.0.0\ndescription: repo skill\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        tmp.close()
        with open(tmp.name, "rb") as f:
            r = client.post("/api/skills/upload", files={"file": ("skill-repoupload.zip", f.read())},
                            data={"scope": "repo", "repo": "owner/repo-x", "agent_type": "sre"}, headers=_headers)
        os.unlink(tmp.name)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT agent_type, scope, owner_id, description, id FROM skill WHERE skill_name='skill-repoupload'").fetchone()
        assert row, "repo skill should be created"
        # owner_id 存归一化 repo_key（OBS 化后带平台前缀，见 repo_key.to_repo_key）
        assert row[0] == "sre" and row[1] == "repo", f"got {row}"
        assert row[2].endswith("owner/repo-x"), f"owner_id 应为 owner/repo-x（可带平台前缀），got {row}"
        assert row[3] == "repo skill", f"description should be parsed from front matter, got {row}"
        # 无 repo 参数也应能查看仓库技能详情（此前只查 official+user，导致 404 加载失败；按 id 精确查询）
        sid = row[4]
        detail = client.get(f"/api/skills/by-id/{sid}", headers=_headers)
        assert detail.status_code == 200, f"Got {detail.status_code}: {detail.text[:200]}"
        assert detail.json().get("scope") == "repo"
        assert "skill-repoupload" in detail.json().get("content", "")
        # 清理磁盘目录，避免污染后续测试
        target = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", "owner/repo-x", "skill-repoupload")
        if os.path.exists(target):
            shutil.rmtree(os.path.dirname(os.path.dirname(target)), ignore_errors=True)

    def test_skills_delete_nonexistent(self):
        r = client.delete("/api/skills/nonexistent_skill", headers=_headers)
        assert r.status_code == 404

    def test_skills_convert_user_to_repo(self):
        """个人技能复制为仓库技能：目录复制 + DB 记录 + 原个人技能保留"""
        import uuid, os, shutil
        from src.session_manager import db, db_lock
        from src.user import db as user_db

        uniq = uuid.uuid4().hex[:8]
        skill_name = f"convskill{uniq}"
        repo_key = f"org/convert-{uniq}"

        # 造一个个人技能（目录 + DB）
        user_dir = os.path.join(user_db.SKILL_DIR, str(_uid["id"]), skill_name)
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: " + skill_name + "\nversion: 1.0.0\ndescription: conv test\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convskill{uniq}%",))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "conv test", "vod", skill_name, "user", str(_uid["id"])))
            db.commit()

        try:
            # 转换
            r = client.post("/api/skills/convert", json={"skill_name": skill_name, "repo": repo_key}, headers=_headers)
            assert r.status_code == 200, f"convert failed: {r.text[:300]}"
            assert r.json()["scope"] == "repo"

            # DB：仓库技能记录创建，原个人技能保留
            with db_lock:
                c = db.cursor()
                repo_row = c.execute("SELECT scope, owner_id, agent_type, description FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?", (skill_name, repo_key)).fetchone()
                user_row = c.execute("SELECT scope FROM skill WHERE skill_name=? AND scope='user' AND owner_id=?", (skill_name, str(_uid["id"]))).fetchone()
            assert repo_row, "repo skill record should exist"
            assert repo_row[0] == "repo" and repo_row[1] == repo_key
            assert repo_row[2] == "vod", f"agent_type should carry over, got {repo_row[2]}"
            assert user_row, "original user skill should be preserved"

            # 目录：仓库目录存在 + 个人目录仍存在
            repo_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", repo_key, skill_name)
            assert os.path.isdir(repo_dir), "repo skill dir should exist"
            assert os.path.isdir(user_dir), "original user skill dir should exist"
        finally:
            # 清理
            repo_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", repo_key)
            if os.path.isdir(repo_dir):
                shutil.rmtree(repo_dir, ignore_errors=True)
            if os.path.isdir(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convskill{uniq}%",))
                db.commit()

    def test_skills_convert_duplicate_repo_skill(self):
        """目标仓库已有同名技能时转换返回 409"""
        import uuid
        from src.session_manager import db, db_lock

        uniq = uuid.uuid4().hex[:8]
        skill_name = f"convdup{uniq}"
        repo_key = f"org/dup-{uniq}"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convdup{uniq}%",))
            # 造同名仓库技能 + 该用户拥有的个人技能
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "d", "vod", skill_name, "repo", repo_key))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "d", "vod", skill_name, "user", str(_uid["id"])))
            db.commit()
        try:
            r = client.post("/api/skills/convert", json={"skill_name": skill_name, "repo": repo_key}, headers=_headers)
            assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text[:200]}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convdup{uniq}%",))
                db.commit()

    def test_skills_convert_not_owner(self):
        """个人技能不属于当前用户时转换返回 404"""
        import uuid
        from src.session_manager import db, db_lock

        uniq = uuid.uuid4().hex[:8]
        skill_name = f"convother{uniq}"
        repo_key = f"org/other-{uniq}"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convother{uniq}%",))
            # 造属于其他用户（99999）的个人技能
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "d", "vod", skill_name, "user", "99999"))
            db.commit()
        try:
            r = client.post("/api/skills/convert", json={"skill_name": skill_name, "repo": repo_key}, headers=_headers)
            assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convother{uniq}%",))
                db.commit()


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
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, gitcode_webhook_token) VALUES (?,?,?,?,?)",
                       ("gitcode:org/repo", "org", "repo", "org/repo", "test_webhook_token_123"))
            db.commit()
        payload = '{"project": {"path_with_namespace": "org/repo"}, "ref": "refs/heads/main"}'
        r = client.post("/webhook/gitcode", content=payload.encode(),
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": "test_webhook_token_123"})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        assert r.json()["status"] == "ok"

    def test_webhook_invalid_token(self):
        payload = '{"project": {"path_with_namespace": "org/repo"}, "ref": "refs/heads/main"}'
        r = client.post("/webhook/gitcode", content=payload.encode(),
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": "wrong_token"})
        assert r.status_code == 403

    def test_webhook_unmanaged_repo_rejected(self):
        """未托管仓库（不在 managed_repos）的 webhook 应被 404 拒绝，不进入 run_pipeline"""
        payload = '{"project": {"path_with_namespace": "ghost/not-managed"}, "ref": "refs/heads/main"}'
        r = client.post("/webhook/gitcode", content=payload.encode(),
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": "any_token"})
        assert r.status_code == 404, f"Got {r.status_code}: {r.text[:200]}"
        assert r.json()["message"] == "repository not managed"

    def test_webhook_unmanaged_repo_even_with_token(self):
        """未托管仓库即使带了 token 也被拒绝（token 校验前先查托管状态）"""
        payload = '{"project": {"path_with_namespace": "ghost/not-managed"}, "ref": "refs/heads/main"}'
        r = client.post("/webhook/gitcode", content=payload.encode(),
                        headers={"X-GitCode-Event": "push", "X-GitCode-Token": "some_valid_looking_token"})
        assert r.status_code == 404, f"Got {r.status_code}: {r.text[:200]}"
        # 确认未生成事件
        with db_lock:
            from src.session_manager import db as _db
            cur = _db.cursor()
            cur.execute("SELECT COUNT(*) FROM events WHERE repo_key='ghost/not-managed'")
            assert cur.fetchone()[0] == 0, "unmanaged repo should not create events"

    def test_a2a_reject_wrong_token(self):
        """A2A 鉴权：仓库配置 bot token 后，无/错 token → 403"""
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_gitcode_token) VALUES (?,?,?,?,?)",
                       ("gitcode:org/test", "org", "test", "org/test", "test_bot_token_123"))
            db.commit()
        r = client.post("/api/a2a", json={"agent": "issue", "payload": {"repository": "org/test", "issue_number": "1"}})
        assert r.status_code == 403  # 无 token
        r2 = client.post("/api/a2a", json={"agent": "issue", "payload": {"repository": "org/test", "issue_number": "1"}},
                         headers={"Authorization": "Bearer wrong_token"})
        assert r2.status_code == 403  # 错 token

    def test_a2a_missing_agent(self):
        r = client.post("/api/a2a", json={}, headers=_headers)
        assert r.status_code == 400

    def test_a2a_missing_repo(self):
        r = client.post("/api/a2a", json={"agent": "issue", "payload": {}}, headers=_headers)
        assert r.status_code == 400

    def test_a2a_with_pr_number(self):
        """A2A 接受可选 pr_number（正确 bot token 通过鉴权，非 400）"""
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_gitcode_token) VALUES (?,?,?,?,?)",
                       ("gitcode:org/test", "org", "test", "org/test", "test_bot_token_123"))
            db.commit()
        r = client.post("/api/a2a", json={
            "agent": "qa",
            "payload": {
                "repository": "org/test",
                "account": "org",
                "issue_number": "42",
                "pr_number": "7",
                "context": "PR #7 created, please review"
            }
        }, headers={"Authorization": "Bearer test_bot_token_123"})
        assert r.status_code != 400, f"Payload validation failed: {r.text}"

    def test_a2a_pr_number_alone_rejected(self):
        """B1 语义：pr_number 可选但不能替代 issue_number——只有 pr_number 而无 issue_number 必须 400"""
        r = client.post("/api/a2a", json={
            "agent": "qa",
            "payload": {
                "repository": "org/test",
                "account": "org",
                "pr_number": "7",
                "context": "PR #7 created, please review"
            }
        }, headers=_headers)
        assert r.status_code == 400, f"Should reject missing issue_number: {r.text}"
        assert "issue_number" in r.json()["detail"]["message"]


# ===================== 13. Token Three-Layer =====================
class TestTokenLayers:
    def test_bot_token_fallback(self):
        from src.api.deps import get_bot_token
        token = get_bot_token("nonexistent_user")
        # Should return empty string or default
        assert isinstance(token, str)

    def test_webhook_token_fallback(self):
        from src.user.db import get_webhook_token_by_repo
        token = get_webhook_token_by_repo("org/nonexistent")
        # 未托管仓库应返回空串（无 webhook token）
        assert isinstance(token, str)
        assert token == ""


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
        from src.api.routing import get_domain_agent, get_agent_skill_map
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?)",
                       ("issue", "Issue Agent", "Test", "●", 2, '["issue_opened"]'))
            db.commit()
        skill = get_domain_agent("issue_opened")
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
