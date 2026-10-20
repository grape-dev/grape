"""API 路由集成测试 — 覆盖所有 13 个路由模块"""
import sys, os, json, io

# 强制使用独立测试库，防止 TestClient 误连生产库（双保险，即使 conftest 未加载）
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock
from src.auth.grape_account_db import (
    create_grape_account, get_grape_account_by_username,
    get_token_version as grape_get_token_version,
    update_profile as grape_update_profile,
)

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    # D32: 直接查 grape_accounts.token_version（不依赖 grape_account_db 模块的 db 引用）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    ver = row[0] if row else 0
    return create_access_token(user_id, username, ver)

client = TestClient(app)

# ====== Test Setup ======
_uid = None
_token = ""
_headers = {}

def setup_module():
    global _uid, _token, _headers
    # D32: 直接用 SQL 创建 grape_accounts 用户（不依赖 grape_account_db 模块的 db 引用）
    from src.auth.password import hash_password
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username='test_api'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0], "username": "test_api"}
        else:
            c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) VALUES (?,?,?,?,1,0)",
                      ("test_api", None, hash_password("test123"), "test_api"))
            db.commit()
            _uid = {"id": c.lastrowid, "username": "test_api"}
    # D32: token 写入 external_identities（get_user_token_with_refresh 从此表读）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'gitcode', ?, ?, ?)",
                  (_uid["id"], "gc_test_api", "test_api", "test_api_token_12345"))
        db.commit()
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 确保 agent_type 表中有 5 个 Agent 类型和技能
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
        # 确保 test_api 用户有权访问 org/test 仓库（check_repo_access 需要 managed_repos + user_repos 关联）
        db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                   ("gitcode:org/test", "org", "test", "org/test"))
        db.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                   (_uid["id"], "gitcode:org/test"))
        db.commit()

def teardown_module():
    # D32: 清理 grape_accounts 中的测试用户
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
            db.commit()


# ===================== 0. 测试辅助 =====================
def monkeypatch_fn(module, attr, fake):
    """简易 monkeypatch：替换 module.attr，返回恢复函数（try/finally 用）。

    只对本文件内的 VCS 权限 mock 使用，避免引入 pytest fixture 的类外依赖。
    """
    import importlib
    _orig = getattr(module, attr)
    setattr(module, attr, fake)
    # 路由内是 from ... import ... 局部导入，改模块属性即生效（导入发生在调用时）
    def _restore():
        setattr(module, attr, _orig)
        importlib.reload(module)
    return _restore


# ===================== 1. Health & SPA =====================
class TestInfra:
    def test_health(self):
        r = client.get("/health", headers={"X-Forwarded-For": "127.0.0.1"})
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
        """update_profile 写 nickname 后 profile 返回该昵称"""
        # D32: 直接用 SQL 更新 grape_accounts.nickname
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET nickname=? WHERE id=?", ("测试昵称", _uid["id"]))
            db.commit()
        try:
            r = client.get("/api/auth/profile", headers=_headers)
            assert r.status_code == 200
            assert r.json()["nickname"] == "测试昵称"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE grape_accounts SET nickname=? WHERE id=?", ("", _uid["id"]))
                db.commit()

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
        assert r2.status_code == 401  # logout 后 token_version+1 → token 失效
        # logout 后 token_version+1——需要用新版本重新生成 token
        # D32: 直接查 grape_accounts.token_version
        with db_lock:
            c = db.cursor()
            c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (_uid["id"],))
            _tv_row = c.fetchone()
        _new_ver = _tv_row[0] if _tv_row else 0
        _token = _jwt_access_token(_uid["id"], _uid["username"])
        # _jwt_access_token 默认 ver=0——logout 后需要 ver=新版本
        from src.api.jwt_utils import create_access_token
        _token = create_access_token(_uid["id"], _uid["username"], _new_ver)
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
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
            db.commit()

        # 清理后应为空
        r = client.get("/api/repos/managed", headers=_headers)
        assert r.status_code == 200
        assert r.json()["repos"] == [], f"Expected empty, got {len(r.json()['repos'])} repos"
        print("  [OK] managed repos empty after cleanup")

        # 加入一个仓库（「加入」已下线，改用 DB 直插的托管登记等价物）
        user_db.add_managed_repo("gitcode:org/test-managed", "org", "test-managed", webhook_id=0)
        user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test-managed", is_join=True)

        # 加入后应出现
        r = client.get("/api/repos/managed", headers=_headers)
        assert r.status_code == 200
        repos = r.json()["repos"]
        assert len(repos) >= 1, f"Expected at least 1 repo, got {len(repos)}"
        found = any(rp["full_name"] == "org/test-managed" for rp in repos)
        assert found, "org/test-managed not in managed list"
        print("  [OK] joined repo appears in managed list")

        # 移除后应消失（救火闸门：mock VCS 权限为 maintainer）
        _restore_rm = self._mock_vcs_perm(40)
        try:
            r = client.post("/api/repos/remove", json={"full_name": "org/test-managed"}, headers=_headers)
            assert r.status_code == 200
        finally:
            _restore_rm()

        r = client.get("/api/repos/managed", headers=_headers)
        all_repos = r.json()["repos"]
        found = any(rp["full_name"] == "org/test-managed" for rp in all_repos)
        assert not found, "org/test-managed still in managed list after remove"
        print("  [OK] removed repo disappears from managed list")

    def test_remove_nonexistent(self):
        r = client.post("/api/repos/remove", json={"full_name": "org/nonexistent"}, headers=_headers)
        assert r.status_code == 404

    @staticmethod
    def _mock_vcs_perm(access_level):
        """mock get_member_permission 返回指定 access_level（测试环境无真实 VCS 凭据）"""
        from src.user import repo_manager
        def _fake(token, owner, repo, username, **kw):
            return {"access_level": access_level, "role_name": "mocked", "permission": "write"}
        return monkeypatch_fn(repo_manager, "get_member_permission", _fake)

    def test_db_add_then_remove(self):
        """DB 直插托管登记后：remove 一次成功、再 remove 报 404（等价原 join→remove 链路）。

        救火闸门：mock VCS 权限为 maintainer（access_level=40）→ 放行。
        """
        _restore = self._mock_vcs_perm(40)
        try:
            user_db.add_managed_repo("gitcode:org/test-repo", "org", "test-repo", webhook_id=0)
            user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test-repo", is_join=True)
            r = client.post("/api/repos/remove", json={"full_name": "org/test-repo"}, headers=_headers)
            assert r.status_code == 200, f"maintainer remove should pass: {r.text[:200]}"
            r = client.post("/api/repos/remove", json={"full_name": "org/test-repo"}, headers=_headers)
            assert r.status_code == 404
        finally:
            _restore()

    def test_remove_denied_developer(self):
        """救火闸门：VCS 权限为 Developer（access_level=30）→ 403 拒绝，托管行保留"""
        _restore = self._mock_vcs_perm(30)
        try:
            user_db.add_managed_repo("gitcode:org/test-dev-deny", "org", "test-dev-deny", webhook_id=0)
            user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test-dev-deny", is_join=True)
            r = client.post("/api/repos/remove", json={"full_name": "org/test-dev-deny"}, headers=_headers)
            assert r.status_code == 403, f"developer remove should be denied: {r.text[:200]}"
            # 托管行未被删除
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM managed_repos WHERE repo_key='gitcode:org/test-dev-deny'")
                assert c.fetchone() is not None, "managed_repos row must survive a denied remove"
            print("  [OK] developer denied, repo still managed")
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key='gitcode:org/test-dev-deny'")
                db.cursor().execute("DELETE FROM user_repos WHERE repo_key='gitcode:org/test-dev-deny'")
                db.commit()
            _restore()

    def test_remove_denied_no_vcs_auth(self):
        """救火闸门：mock 掉 GitCode 用户 token（模拟未授权）→ 403 拒绝"""
        from src.api import deps
        _restore = monkeypatch_fn(deps, "get_user_token_with_refresh", lambda *a, **k: "")
        try:
            user_db.add_managed_repo("gitcode:org/test-noauth-deny", "org", "test-noauth-deny", webhook_id=0)
            user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test-noauth-deny", is_join=True)
            r = client.post("/api/repos/remove", json={"full_name": "org/test-noauth-deny"}, headers=_headers)
            assert r.status_code == 403, f"no-auth remove should be denied: {r.text[:200]}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key='gitcode:org/test-noauth-deny'")
                db.cursor().execute("DELETE FROM user_repos WHERE repo_key='gitcode:org/test-noauth-deny'")
                db.commit()
            _restore()

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

        # 2b) 真实 GitCode 格式：维护者 access_level=40（注意 permissions.admin 恒为 false，
        #     真实权限看 access_level / 单数 permission）→ 通过
        with patch("src.user.repo_manager._req", return_value={
                "access_level": 40, "role_name": "Maintainer",
                "permissions": {"admin": False}, "permission": "admin"}):
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "somebody") is True

        # 2c) 真实 GitCode 格式：开发者 access_level=30 → 拒绝
        with patch("src.user.repo_manager._req", return_value={
                "access_level": 30, "role_name": "Developer",
                "permissions": {"admin": False}, "permission": "write"}):
            assert _check_collaborator_admin("tok", "owner_user", "repo1", "somebody") is False

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
        # 确保测试用户有权访问 org/test（前面的测试可能清了 managed_repos）
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                       ("gitcode:org/test", "org", "test", "org/test"))
            db.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                       (_uid["id"], "gitcode:org/test"))
            db.commit()
        r = client.get("/api/dashboard/org/test", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "metrics" in d
        assert "agents" in d
        assert len(d["agents"]) == 5  # VOD/Issue/Coding/QA/SRE
        assert "agent_engines" in d  # Scene Center: engine 列表替代 activity

    def test_dashboard_agent_engines_includes_offline(self):
        """Issue #227: agent_engines 应返回全部引擎（含 offline），不能按状态过滤。"""
        import datetime as _dt
        from src.session_manager import db, db_lock
        _tz = _dt.timezone.utc
        _fresh = (_dt.datetime.now(_tz) - _dt.timedelta(seconds=10)).isoformat()
        _stale = (_dt.datetime.now(_tz) - _dt.timedelta(minutes=20)).isoformat()
        _ids = []
        with db_lock:
            c = db.cursor()
            # 清理可能残留的同名引擎
            for n in ("test-offline-eng", "test-online-eng", "test-recentlylost-eng"):
                c.execute("SELECT id FROM agent_runtimes WHERE name=?", (n,))
                for row in c.fetchall():
                    c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (row[0],))
                c.execute("DELETE FROM agent_runtimes WHERE name=?", (n,))
            for n in ("cred-offline", "cred-online", "cred-recentlylost"):
                c.execute("DELETE FROM runtime_credentials WHERE name=?", (n,))
            # 3 个凭证（global scope, active）
            creds = {}
            for cn in ("cred-online", "cred-recentlylost", "cred-offline"):
                c.execute(
                    "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
                    "VALUES (?,?,?,?,?,?, 'active')",
                    ("h"+cn, cn, "dev-"+cn, "global", "", "tester"))
                creds[cn] = c.lastrowid
            # online 引擎：status=online + 心跳新鲜
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("test-online-eng", "test-online-eng", creds["cred-online"], "online", 1, 1, _fresh, "global", ""))
            _ids.append(c.lastrowid)
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (_ids[-1],))
            # recently_lost 引擎：status=online + 心跳超时
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("test-recentlylost-eng", "test-recentlylost-eng", creds["cred-recentlylost"], "online", 1, 1, _stale, "global", ""))
            _ids.append(c.lastrowid)
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (_ids[-1],))
            # offline 引擎：status=offline + 无心跳
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("test-offline-eng", "test-offline-eng", creds["cred-offline"], "offline", 0, 1, "", "global", ""))
            _ids.append(c.lastrowid)
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (_ids[-1],))
            db.commit()
        try:
            r = client.get("/api/dashboard/org/test", headers=_headers)
            assert r.status_code == 200
            d = r.json()
            ae = d["agent_engines"]
            # tag="all" 的引擎应出现在每个 agent_type 下
            all_engines = []
            for atype in ("vod", "issue", "coding", "qa", "sre"):
                all_engines.extend(ae.get(atype, []))
            statuses = {e["status"] for e in all_engines}
            # 必须包含 offline（Issue #227 核心验收点）
            assert "offline" in statuses, f"offline engines missing from agent_engines, got statuses: {statuses}"
            # 三种状态都应出现
            assert "online" in statuses, f"online engines missing, got: {statuses}"
            assert "recently_lost" in statuses, f"recently_lost engines missing, got: {statuses}"
        finally:
            with db_lock:
                c = db.cursor()
                for rid in _ids:
                    c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                    c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                for cn in ("cred-online", "cred-recentlylost", "cred-offline"):
                    c.execute("DELETE FROM runtime_credentials WHERE name=?", (cn,))
                db.commit()

    def test_dashboard_agent_engines_dedup(self):
        """Issue #227: 同一 runtime_id 跨 scope 查询时应去重。"""
        import datetime as _dt
        from src.session_manager import db, db_lock
        _tz = _dt.timezone.utc
        _fresh = (_dt.datetime.now(_tz) - _dt.timedelta(seconds=10)).isoformat()
        _ids = []
        with db_lock:
            c = db.cursor()
            for n in ("test-dedup-eng",):
                c.execute("SELECT id FROM agent_runtimes WHERE name=?", (n,))
                for row in c.fetchall():
                    c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (row[0],))
                c.execute("DELETE FROM agent_runtimes WHERE name=?", (n,))
            for n in ("cred-dedup",):
                c.execute("DELETE FROM runtime_credentials WHERE name=?", (n,))
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
                "VALUES (?,?,?,?,?,?, 'active')",
                ("hcred-dedup", "cred-dedup", "dev-dedup", "global", "", "tester"))
            cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("test-dedup-eng", "test-dedup-eng", cid, "online", 1, 1, _fresh, "global", ""))
            _ids.append(c.lastrowid)
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (_ids[-1],))
            db.commit()
        try:
            r = client.get("/api/dashboard/org/test", headers=_headers)
            assert r.status_code == 200
            d = r.json()
            ae = d["agent_engines"]
            for atype in ("vod", "issue", "coding", "qa", "sre"):
                engines = ae.get(atype, [])
                ids = [e["id"] for e in engines]
                # 同一 runtime_id 不应重复出现
                assert len(ids) == len(set(ids)), f"Duplicate engine ids found for {atype}: {ids}"
        finally:
            with db_lock:
                c = db.cursor()
                for rid in _ids:
                    c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                    c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-dedup",))
                db.commit()


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
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                      (repo_key, "pipeline", "repo-prefix", "pipeline/repo-prefix"))
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                      (_uid["id"], repo_key))
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
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM user_repos WHERE repo_key=?", (repo_key,))
            # 仓库技能（agent_type='sre'）
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, "agent-skill", "d", "sre", skill_name, "repo", repo_key))
            # TEST-14: check_repo_access 需 managed_repos + user_repos 关联
            owner, repo = repo_key.split("/", 1)
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                      (repo_key, owner, repo, repo, 1, 'b'))
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                      (_uid["id"], repo_key, owner, repo, 1, 1))
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
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
                c.execute("DELETE FROM user_repos WHERE repo_key=?", (repo_key,))
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
        # D32: 直接用 SQL 创建 grape_accounts 用户
        from src.auth.password import hash_password as _hp
        _chat_uname = "chat_def_" + _uuid.uuid4().hex[:6]
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) VALUES (?,?,?,?,1,0)",
                      (_chat_uname, None, _hp("pw123"), _chat_uname))
            db.commit()
            user = {"id": c.lastrowid, "username": _chat_uname}
        assert user
        tok = _jwt_access_token(user["id"], user["username"])
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
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (user["id"],))
            db.commit()

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
        from src.auth.password import hash_password as _hp2
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM grape_accounts WHERE username='chat_iso_b'")
            _row = c.fetchone()
            if _row:
                user_b = {"id": _row[0], "username": "chat_iso_b"}
            else:
                c.execute("INSERT INTO grape_accounts (username, email, password_hash, status, token_version) VALUES (?,?,?,1,0)",
                          ("chat_iso_b", None, _hp2("pw123")))
                db.commit()
                user_b = {"id": c.lastrowid, "username": "chat_iso_b"}
        token_b = _jwt_access_token(user_b["id"], user_b["username"])
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
            c.execute("DELETE FROM grape_accounts WHERE id=?", (user_b["id"],))
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
        from src.auth.password import hash_password as _hp3
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM grape_accounts WHERE username='chat_uniq_b'")
            _row = c.fetchone()
            if _row:
                user_b = {"id": _row[0], "username": "chat_uniq_b"}
            else:
                c.execute("INSERT INTO grape_accounts (username, email, password_hash, status, token_version) VALUES (?,?,?,1,0)",
                          ("chat_uniq_b", None, _hp3("pw123")))
                db.commit()
                user_b = {"id": c.lastrowid, "username": "chat_uniq_b"}
        token_b = _jwt_access_token(user_b["id"], user_b["username"])
        headers_b = {"Authorization": f"Bearer {token_b}"}
        r = client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec_x"}}}, headers=headers_b)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:200]}"
        assert "已被其他用户" in r.text

        # 清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE im_app_id=? OR user_id IN (?,?)", (app_id, _uid["id"], user_b["id"]))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (user_b["id"],))
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
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        # 造一个个人 chat 技能 + 一个仓库 chat 技能（agent_type='chat'）
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatskill{uniq}%",))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (f"chatskill{uniq}-user", f"chat-skill-user-{uniq}", "desc", "chat", f"chatskill{uniq}-user", "user", str(uid)))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (f"chatskill{uniq}-repo", f"chat-skill-repo-{uniq}", "desc", "chat", f"chatskill{uniq}-repo", "repo", rk))
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
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"chatsid{uniq}%",))
            # 个人 chat 技能 + 仓库 chat 技能
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (user_skill, "u", "d", "chat", user_skill, "user", str(uid)))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (repo_skill, "r", "d", "chat", repo_skill, "repo", rk))
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
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,))
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"dupname{uniq}%",))
            # 同名：一个官方 + 一个仓库
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (same_name, "official-dup", "o", "chat", same_name, "official", ""))
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (same_name, "repo-dup", "r", "chat", same_name, "repo", rk))
            db.commit()
        try:
            # 不带 skill_scope：repo 优先 → 命中仓库技能
            r = client.put(f"/api/chat/config?repo={repo_key}", json={"skill": same_name}, headers=_headers)
            assert r.status_code == 200, f"save failed: {r.text[:200]}"
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT skill, skill_id FROM chat_agent_session WHERE user_id=? AND repo_key=''", (uid,)).fetchone()
            repo_id = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?", (same_name, rk)).fetchone()
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

    def test_clear_credentials_empty_string(self):
        """Issue #237：传空字符串应清除已有凭据"""
        # D32: 凭据存于 user_credentials 表，用 get_credential 读
        # 1. 先写入 AK/SK
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "real_ak_123", "huawei_cloud_sk": "real_sk_456"
        }, headers=_headers)
        cred = user_db.get_credential(_uid["id"], "huawei_cloud") or {}
        assert cred.get("ak") == "real_ak_123"
        assert cred.get("sk") == "real_sk_456"
        # 2. 传空字符串清除
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "", "huawei_cloud_sk": ""
        }, headers=_headers)
        cred = user_db.get_credential(_uid["id"], "huawei_cloud") or {}
        assert cred.get("ak", "") == ""
        assert cred.get("sk", "") == ""
        # 3. GET 端点反映清除后的状态
        r = client.get("/api/settings/credentials", headers=_headers)
        hw = r.json()["huawei_cloud"]
        assert hw["ak_exists"] is False
        assert hw["sk_exists"] is False

    def test_credentials_not_modified_when_omitted(self):
        """Issue #237：未传字段不应修改已有值"""
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "keep_this_ak"
        }, headers=_headers)
        # 只传 SK，不传 AK → AK 应保持不变
        client.put("/api/settings/credentials", json={
            "huawei_cloud_sk": "new_sk"
        }, headers=_headers)
        cred = user_db.get_credential(_uid["id"], "huawei_cloud") or {}
        assert cred.get("ak") == "keep_this_ak"
        assert cred.get("sk") == "new_sk"

    def test_placeholder_rejected_on_clear(self):
        """Issue #237：占位符值应被拒绝，不覆盖已有值"""
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "existing_ak"
        }, headers=_headers)
        # 传占位符 → 应被拒绝，已有值保持不变
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "AK-demo-access-key-xxxx"
        }, headers=_headers)
        cred = user_db.get_credential(_uid["id"], "huawei_cloud") or {}
        assert cred.get("ak") == "existing_ak"

    def test_clear_github_token(self):
        """Issue #237：传空字符串清除 GitHub token"""
        # D32: 先写入 github token 到 external_identities
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'github', 'gh_test', 'testuser', 'ghp_test_token')",
                      (_uid["id"],))
            db.commit()
        # 传空字符串清除
        client.put("/api/settings/credentials", json={
            "github_token": ""
        }, headers=_headers)
        # D32: 清除后 external_identities 中应无 github 绑定
        with db_lock:
            c = db.cursor()
            c.execute("SELECT access_token FROM external_identities WHERE grape_user_id=? AND provider='github'", (_uid["id"],))
            row = c.fetchone()
        assert not row or row[0] == ""

    def test_clear_gitcode_token(self):
        """对称测试：传空字符串清除 GitCode token"""
        # 先写入 gitcode token 到 external_identities
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'gitcode', 'gc_test', 'testuser', 'glpat_test_token')",
                      (_uid["id"],))
            db.commit()
        # 传空字符串清除
        client.put("/api/settings/credentials", json={
            "gitcode_token": ""
        }, headers=_headers)
        # 清除后 external_identities 中应无 gitcode 绑定
        with db_lock:
            c = db.cursor()
            c.execute("SELECT access_token FROM external_identities WHERE grape_user_id=? AND provider='gitcode'", (_uid["id"],))
            row = c.fetchone()
        assert not row or row[0] == ""

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

    def test_agent_runtimes_pagination(self):
        """Issue #185: agent-runtimes 分页参数与 total 返回验证"""
        # 不带分页参数 → 全量返回（向后兼容），total 存在
        r = client.get("/api/settings/agent-runtimes", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "total" in d
        assert d["total"] == len(d["runtimes"])

        # 带分页参数 page=1&page_size=2
        r2 = client.get("/api/settings/agent-runtimes?page=1&page_size=2", headers=_headers)
        assert r2.status_code == 200
        d2 = r2.json()
        assert "runtimes" in d2
        assert "total" in d2
        assert "page" in d2
        assert "page_size" in d2
        assert d2["page"] == 1
        assert d2["page_size"] == 2
        # 分页结果不超过 page_size
        assert len(d2["runtimes"]) <= 2
        # total 与全量一致
        assert d2["total"] == d["total"]

        # page=2 应返回第二页
        if d2["total"] > 2:
            r3 = client.get("/api/settings/agent-runtimes?page=2&page_size=2", headers=_headers)
            assert r3.status_code == 200
            d3 = r3.json()
            assert d3["page"] == 2
            assert len(d3["runtimes"]) <= 2
            # 第一页和第二页不应有重复
            ids_p1 = {rt["id"] for rt in d2["runtimes"]}
            ids_p2 = {rt["id"] for rt in d3["runtimes"]}
            assert not (ids_p1 & ids_p2), "分页结果不应有重复项"

    def test_agent_runtime_detail(self):
        """Issue #364: 单引擎详情端点——可见引擎返回完整弹窗字段"""
        import datetime as _dt
        from src.session_manager import db, db_lock
        _tz = _dt.timezone.utc
        _fresh = (_dt.datetime.now(_tz) - _dt.timedelta(seconds=10)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM agent_runtimes WHERE name=?", ("test-detail-eng",))
            for row in c.fetchall():
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (row[0],))
            c.execute("DELETE FROM agent_runtimes WHERE name=?", ("test-detail-eng",))
            c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-detail",))
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
                "VALUES (?,?,?,?,?,?, 'active')",
                ("hcred-detail", "cred-detail", "dev-detail", "global", "", "tester"))
            cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id, alias) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("test-detail-eng", "test-detail-eng", cid, "online", 1, 1, _fresh, "global", "", "detail-alias"))
            rid = c.lastrowid
            db.commit()
        try:
            r = client.get(f"/api/settings/agent-runtimes/{rid}", headers=_headers)
            assert r.status_code == 200
            d = r.json()
            assert d["id"] == rid
            assert d["name"] == "test-detail-eng"
            assert d["alias"] == "detail-alias"
            # 弹窗所需完整字段（Issue #364 验收）
            for k in ("health", "instance_name", "worker_name", "worker_ip",
                      "scope", "owner", "health_score", "tags", "local_skills"):
                assert k in d, f"详情端点缺少字段 {k}"
            # 不存在的 id → 404
            r404 = client.get("/api/settings/agent-runtimes/999999999", headers=_headers)
            assert r404.status_code == 404
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-detail",))
                db.commit()

    def test_agent_runtime_detail_invisible_user_scope(self):
        """Issue #364: user scope 他人引擎对非 owner 不可见 → 404（可见性校验复用列表口径）"""
        import datetime as _dt
        from src.session_manager import db, db_lock
        _tz = _dt.timezone.utc
        _fresh = (_dt.datetime.now(_tz) - _dt.timedelta(seconds=10)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM agent_runtimes WHERE name=?", ("test-invis-eng",))
            for row in c.fetchall():
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (row[0],))
            c.execute("DELETE FROM agent_runtimes WHERE name=?", ("test-invis-eng",))
            c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-invis",))
            # scope=user 且 owner_name 为他人（非当前测试用户）
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
                "VALUES (?,?,?,?,?,?, 'active')",
                ("hcred-invis", "cred-invis", "dev-invis", "user", "", "someone-else"))
            cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("test-invis-eng", "test-invis-eng", cid, "online", 1, 1, _fresh, "user", ""))
            rid = c.lastrowid
            db.commit()
        try:
            # 列表中也不可见（对照断言）
            r_list = client.get("/api/settings/agent-runtimes?page=1&page_size=0", headers=_headers)
            assert rid not in {rt["id"] for rt in r_list.json()["runtimes"]}
            # 详情端点同样 404
            r = client.get(f"/api/settings/agent-runtimes/{rid}", headers=_headers)
            assert r.status_code == 404
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-invis",))
                db.commit()

    def test_runtime_devices_endpoint(self):
        """Issue #185: runtime-devices 分页端点验证"""
        r = client.get("/api/settings/runtime-devices?page=1&page_size=10", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert "devices" in d
        assert "total" in d
        assert "page" in d
        assert "page_size" in d
        assert d["page"] == 1
        assert d["page_size"] == 10
        assert len(d["devices"]) <= 10

    def test_runtime_devices_filter_scope(self):
        """Issue #245: scope 筛选后 total 与筛选结果一致"""
        # 先取全量 total
        r_all = client.get("/api/settings/runtime-devices?page=1&page_size=100", headers=_headers)
        assert r_all.status_code == 200
        d_all = r_all.json()
        total_all = d_all["total"]
        # avail_scopes 应存在
        assert "avail_scopes" in d_all
        avail = d_all["avail_scopes"] or []
        if not avail:
            return  # 测试库无设备，跳过
        # 选一个存在的 scope 过滤
        target_scope = avail[0]
        r_f = client.get(f"/api/settings/runtime-devices?page=1&page_size=100&scope={target_scope}",
                         headers=_headers)
        assert r_f.status_code == 200
        d_f = r_f.json()
        # 筛选后 total <= 全量 total
        assert d_f["total"] <= total_all
        # 每个返回设备的 scope 应匹配（local → credential_id==0）
        for dev in d_f["devices"]:
            if target_scope == "local":
                assert dev["credential_id"] == 0
            else:
                assert (dev.get("scope") or "global") == target_scope
        # avail_scopes 取自全部可见设备（不依赖当前筛选），target_scope 仍应存在
        assert target_scope in (d_f["avail_scopes"] or [])

    def test_runtime_devices_filter_type(self):
        """Issue #245: type 筛选后 total 与筛选结果一致"""
        r_all = client.get("/api/settings/runtime-devices?page=1&page_size=100", headers=_headers)
        d_all = r_all.json()
        # 从全量设备中提取一个引擎 type 关键字
        types = set()
        for dev in d_all["devices"]:
            for rt in dev.get("runtimes", []):
                n = (rt.get("name") or "").lower()
                for kw in ("opencode", "hermes", "codearts", "codex"):
                    if kw in n:
                        types.add(kw)
        if not types:
            return  # 测试库无引擎，跳过
        target_type = next(iter(types))
        r_f = client.get(f"/api/settings/runtime-devices?page=1&page_size=100&type={target_type}",
                         headers=_headers)
        assert r_f.status_code == 200
        d_f = r_f.json()
        # 筛选后 total <= 全量 total
        assert d_f["total"] <= d_all["total"]
        # 每个返回设备应至少有一个引擎名包含 type 关键字
        for dev in d_f["devices"]:
            names = " ".join((rt.get("name") or "").lower() for rt in dev.get("runtimes", []))
            assert target_type in names

    def test_runtime_devices_filter_q(self):
        """Issue #245: q 搜索筛选后 total 与筛选结果一致"""
        r_all = client.get("/api/settings/runtime-devices?page=1&page_size=100", headers=_headers)
        d_all = r_all.json()
        if not d_all["devices"]:
            return
        # 取第一个设备的 name 作为搜索关键字
        sample = d_all["devices"][0]
        keyword = (sample.get("name") or sample.get("device_name") or "").strip()
        if not keyword:
            return
        # 用 name 的一部分做模糊搜索
        q = keyword[:3] if len(keyword) >= 3 else keyword
        r_f = client.get(f"/api/settings/runtime-devices?page=1&page_size=100&q={q}", headers=_headers)
        assert r_f.status_code == 200
        d_f = r_f.json()
        # 筛选后 total <= 全量 total
        assert d_f["total"] <= d_all["total"]
        assert d_f["total"] >= 1
        # 每个返回设备应匹配关键字
        for dev in d_f["devices"]:
            hay = " ".join([
                dev.get("name") or "", dev.get("device_name") or "",
                dev.get("owner") or "", dev.get("worker_ip") or "",
                " ".join((rt.get("name") or "") for rt in dev.get("runtimes", []))
            ]).lower()
            assert q.lower() in hay

    def test_runtime_devices_filter_total_consistency(self):
        """Issue #245: 筛选后 total 与分页切片数据一致性（跨页验证）"""
        r_all = client.get("/api/settings/runtime-devices?page=1&page_size=100", headers=_headers)
        d_all = r_all.json()
        if d_all["total"] <= 1:
            return
        # 用 page_size=1 分页，验证 total 跨页不变
        r_p1 = client.get("/api/settings/runtime-devices?page=1&page_size=1", headers=_headers)
        r_p2 = client.get("/api/settings/runtime-devices?page=2&page_size=1", headers=_headers)
        assert r_p1.status_code == 200 and r_p2.status_code == 200
        d_p1, d_p2 = r_p1.json(), r_p2.json()
        assert d_p1["total"] == d_all["total"] == d_p2["total"]
        assert len(d_p1["devices"]) == 1
        # 第一页和第二页不应是同一设备
        if d_all["total"] >= 2:
            id1 = d_p1["devices"][0].get("credential_id")
            id2 = d_p2["devices"][0].get("credential_id")
            assert id1 != id2


# ===================== 10. Skills =====================
class TestSkills:
    # TEST-14: mock OBS storage for upload/convert tests
    _fake_storage = None

    def setup_method(self):
        """为每个测试方法创建 FakeStorage mock（仅 OBS 依赖测试需要）。"""
        from src.storage import StorageBackend
        class FakeStorage(StorageBackend):
            def backend_name(self): return "fake"
            def save_bytes(self, key, data): return key
            def save_stream(self, key, source): return key
            def read_bytes(self, key): return b""
            def read_file(self, key, local_path): return True
            def exists(self, key): return True
            def list_keys(self, prefix): return []
            def delete_prefix(self, prefix): return 0
            def delete_object(self, key): return True
            def copy_object(self, src, dst): return True
            def temporary_url(self, key, expires=300): return f"https://fake.example.com/{key}"
            def presigned_url(self, key, expires): return f"https://fake.example.com/{key}"
            def stat(self, key): return {"size": 0}
            default_expires = 300
        TestSkills._fake_storage = FakeStorage()
        # autouse: 全局 patch get_storage，OBS 依赖测试自动走 fake
        from unittest.mock import patch as _patch
        self._storage_patch = _patch("src.storage.get_storage", return_value=TestSkills._fake_storage)
        self._storage_patch.__enter__()

    def teardown_method(self):
        if hasattr(self, '_storage_patch'):
            self._storage_patch.__exit__(None, None, None)

    def test_skills_list(self):
        r = client.get("/api/skills", headers=_headers)
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
        """GET /api/skills/template — OBS 模式返回 {url, expires_in} JSON"""
        r = client.get("/api/skills/template", headers=_headers)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:200]}"
        # OBS 模式返回 JSON {url, expires_in}；local 模式返回 zip
        ct = r.headers.get("content-type", "")
        if ct.startswith("application/json"):
            data = r.json()
            assert "url" in data, f"OBS mode should return url, got {data}"
            assert "expires_in" in data
        else:
            # local 模式：直接返回 zip
            assert ct.startswith("application/zip")
            import zipfile, io as _io
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
        # 自隔离：清除历史残留（含 gitcode: 前缀变体行 + 磁盘目录），保证本用例独占
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name='skill-repoupload'")
            db.commit()
        _repo_dir_root = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo")
        for _sub in ("owner/repo-x", "gitcode:owner/repo-x"):
            _t = os.path.join(_repo_dir_root, _sub, "skill-repoupload")
            if os.path.exists(_t):
                shutil.rmtree(_t, ignore_errors=True)
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
            row = c.execute("SELECT agent_type, scope, owner_id, description, id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?",
                            ("skill-repoupload", "gitcode:owner/repo-x")).fetchone()
        assert row, "repo skill should be created"
        # owner_id 存归一化 repo_key（OBS 化后带平台前缀，见 repo_key.to_repo_key）
        assert row[0] == "sre" and row[1] == "repo", f"got {row}"
        assert row[2].endswith("owner/repo-x"), f"owner_id 应为 owner/repo-x（可带平台前缀），got {row}"
        assert row[3] == "repo skill", f"description should be parsed from front matter, got {row}"
        # by-id 权限校验需要 user_repos 中有该仓库记录
        from src.repo_key import to_repo_key
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                      (_uid["id"], to_repo_key("owner/repo-x"), "owner", "repo-x", 1, 1))
            db.commit()
        # 无 repo 参数也应能查看仓库技能详情（此前只查 official+user，导致 404 加载失败；按 id 精确查询）
        sid = row[4]
        detail = client.get(f"/api/skills/by-id/{sid}", headers=_headers)
        assert detail.status_code == 200, f"Got {detail.status_code}: {detail.text[:200]}"
        assert detail.json().get("scope") == "repo"
        assert "skill-repoupload" in detail.json().get("content", "")
        # 清理磁盘目录 + user_repos 记录
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (_uid["id"], to_repo_key("owner/repo-x")))
            db.commit()
        target = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", "owner/repo-x", "skill-repoupload")
        if os.path.exists(target):
            shutil.rmtree(os.path.dirname(os.path.dirname(target)), ignore_errors=True)

    def test_skills_delete_nonexistent(self):
        r = client.delete("/api/skills/by-id/999999", headers=_headers)
        assert r.status_code == 404

    def test_skills_convert_user_to_repo(self):
        """个人技能复制为仓库技能：目录复制 + DB 记录 + 原个人技能保留"""
        import uuid, os, shutil
        from src.session_manager import db, db_lock
        from src.user import db as user_db

        uniq = uuid.uuid4().hex[:8]
        skill_name = f"convskill{uniq}"
        repo_key = f"org/convert-{uniq}"
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)

        # 造一个个人技能（目录 + DB）
        user_dir = os.path.join(user_db.SKILL_DIR, str(_uid["id"]), skill_name)
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: " + skill_name + "\nversion: 1.0.0\ndescription: conv test\n---\n\n## 场景\n## 知识\n## 步骤\n- step")
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convskill{uniq}%",))
            # TEST-14: file_path 设 fake OBS 对象键，convert 路由检查此字段
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, file_path) VALUES (?,?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "conv test", "vod", skill_name, "user", str(_uid["id"]), f"custom/{_uid['id']}/{skill_name}"))
            db.commit()

        try:
            # 转换
            r = client.post("/api/skills/convert", json={"skill_name": skill_name, "repo": repo_key}, headers=_headers)
            assert r.status_code == 200, f"convert failed: {r.text[:300]}"
            assert r.json()["scope"] == "repo"

            # DB：仓库技能记录创建，原个人技能保留
            with db_lock:
                c = db.cursor()
                repo_row = c.execute("SELECT scope, owner_id, agent_type, description FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?", (skill_name, rk)).fetchone()
                user_row = c.execute("SELECT scope FROM skill WHERE skill_name=? AND scope='user' AND owner_id=?", (skill_name, str(_uid["id"]))).fetchone()
            assert repo_row, "repo skill record should exist"
            assert repo_row[0] == "repo" and repo_row[1] == rk
            assert repo_row[2] == "vod", f"agent_type should carry over, got {repo_row[2]}"
            assert user_row, "original user skill should be preserved"
            # OBS 模式：不创建本地目录，技能存储在 OBS（FakeStorage copy_object 已 mock）
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
        from src.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name LIKE ?", (f"convdup{uniq}%",))
            # 造同名仓库技能 + 该用户拥有的个人技能
            c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,?,?)",
                      (skill_name, skill_name, "d", "vod", skill_name, "repo", rk))
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
        r = client.get("/api/search?q=test&type=all", headers=_headers)
        assert r.status_code == 200
        assert "events" in r.json()
        assert "repos" in r.json()
        assert "agents" in r.json()

    def test_search_no_query(self):
        # q is required by pattern: min_length=1, so empty gives 422
        r = client.get("/api/search", headers=_headers)
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
        # 自给自足：预置托管行，保证任何执行顺序下 token 校验路径一致（不依赖 test_webhook_push 先跑）
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, gitcode_webhook_token) VALUES (?,?,?,?,?)",
                       ("gitcode:org/repo", "org", "repo", "org/repo", "test_webhook_token_123"))
            db.commit()
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



# ===================== 15. Knowledge (Issue #199) =====================
class TestKnowledge:
    """知识 CRUD + 单接口创建+绑定关联 + 校验 + N+1 优化验证"""

    def _create_entry(self, **extra):
        """辅助：创建知识条目，返回 response"""
        import time
        body = {
            "repo": "org/test",
            "content": "测试内容",
            "description": "测试描述",
            "name": "测试知识-" + str(time.time())
        }
        body.update(extra)
        return client.post("/api/knowledge/entry", json=body, headers=_headers)

    def _cleanup_kid(self, kid):
        if kid:
            client.delete(f"/api/knowledge/{kid}", headers=_headers)

    def test_create_entry_basic(self):
        """创建知识条目（无关联）"""
        r = self._create_entry()
        assert r.status_code == 200, f"create failed: {r.text[:200]}"
        kid = r.json().get("id")
        assert kid
        self._cleanup_kid(kid)

    def test_create_entry_with_agent_types(self):
        """单接口创建+绑定 Agent（Issue #199）"""
        r = self._create_entry(agent_types=["vod", "qa"])
        assert r.status_code == 200, f"create+bind failed: {r.text[:200]}"
        kid = r.json().get("id")
        assert kid
        try:
            d = client.get(f"/api/knowledge/{kid}", headers=_headers)
            assert d.status_code == 200
            refs = d.json().get("refs", [])
            agent_refs = [r for r in refs if r.get("agent_type") and not r.get("scene_id")]
            assert len(agent_refs) == 2, f"expected 2 agent refs, got {len(agent_refs)}"
            agent_types_set = {r["agent_type"] for r in agent_refs}
            assert agent_types_set == {"vod", "qa"}
        finally:
            self._cleanup_kid(kid)

    def test_create_entry_invalid_agent_type(self):
        """非法 agent_type 校验"""
        r = self._create_entry(agent_types=["invalid_type"])
        assert r.status_code == 400
        assert "不支持的 Agent 类型" in r.json().get("detail", {}).get("message", "")

    def test_create_entry_invalid_scene_id(self):
        """非法 scene_id 校验"""
        r = self._create_entry(scene_ids=["nonexistent-scene-uuid"])
        assert r.status_code == 400
        assert "场景不存在" in r.json().get("detail", {}).get("message", "")

    def test_create_entry_empty_name(self):
        """name 必填校验"""
        r = self._create_entry(name="")
        assert r.status_code == 400
        assert "name 必填" in r.json().get("detail", {}).get("message", "")

    def test_create_entry_empty_content(self):
        """content 必填校验"""
        r = self._create_entry(content="")
        assert r.status_code == 400
        assert "content 必填" in r.json().get("detail", {}).get("message", "")

    def test_create_entry_no_repo(self):
        """repo 必填校验"""
        r = self._create_entry(repo="")
        assert r.status_code == 400
        assert "必须指定 repo" in r.json().get("detail", {}).get("message", "")

    def test_detail_n_plus_1_fixed(self):
        """详情接口返回 pipeline_chain（N+1 改 JOIN 验证）"""
        r = self._create_entry(agent_types=["vod"])
        kid = r.json().get("id")
        try:
            d = client.get(f"/api/knowledge/{kid}", headers=_headers)
            assert d.status_code == 200
            refs = d.json().get("refs", [])
            assert len(refs) >= 1
            for ref in refs:
                assert "scene_pipeline" in ref
        finally:
            self._cleanup_kid(kid)

    def test_delete_entry_cascades_refs(self):
        """删除知识条目后 refs 也删除"""
        r = self._create_entry(agent_types=["vod", "qa"])
        kid = r.json().get("id")
        client.delete(f"/api/knowledge/{kid}", headers=_headers)
        r = client.get(f"/api/knowledge/{kid}", headers=_headers)
        assert r.status_code == 404
        from src.session_manager import db, db_lock
        with db_lock:
            count = db.cursor().execute(
                "SELECT COUNT(*) FROM scene_knowledge_ref WHERE knowledge_id=?", (kid,)
            ).fetchone()[0]
        assert count == 0, f"refs not cascaded, still {count} rows"

    def test_update_entry(self):
        """编辑知识内容"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            r = client.put(f"/api/knowledge/{kid}", json={
                "content": "更新后的内容",
                "description": "更新后的描述",
                "name": "更新后的名称"
            }, headers=_headers)
            assert r.status_code == 200
            d = client.get(f"/api/knowledge/{kid}", headers=_headers)
            assert d.status_code == 200
            assert d.json()["name"] == "更新后的名称"
            assert d.json()["content"] == "更新后的内容"
        finally:
            self._cleanup_kid(kid)

    def test_toggle_agent_ref(self):
        """绑定+解绑 Agent"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            r = client.post(f"/api/knowledge/{kid}/refs/batch",
                           json={"scene_ids": [], "agent_types": ["vod"]}, headers=_headers)
            assert r.status_code == 200
            assert r.json()["added"] == 1
            d = client.get(f"/api/knowledge/{kid}", headers=_headers)
            agent_refs = [r for r in d.json()["refs"] if r.get("agent_type") == "vod" and r.get("enabled")]
            assert len(agent_refs) == 1
            ref_id = agent_refs[0]["id"]
            r = client.delete(f"/api/knowledge/ref/{ref_id}", headers=_headers)
            assert r.status_code == 200
            d = client.get(f"/api/knowledge/{kid}", headers=_headers)
            agent_refs = [r for r in d.json()["refs"] if r.get("agent_type") == "vod" and r.get("enabled")]
            assert len(agent_refs) == 0
        finally:
            self._cleanup_kid(kid)

    # ===== 边界校验测试 =====

    def test_create_entry_name_too_long(self):
        """name 超过 50 字符"""
        r = self._create_entry(name="x" * 51)
        assert r.status_code == 400
        assert "name 不能超过" in r.json().get("detail", {}).get("message", "")

    def test_create_entry_content_too_large(self):
        """content 超过默认上限 100KB（Issue #358 后上限可配置，默认 100KB）"""
        r = self._create_entry(content="x" * (100 * 1024 + 1))
        assert r.status_code == 400
        assert "content 不能超过" in r.json().get("detail", {}).get("message", "")

    # ---- Issue #358: 知识正文大小上限可配置（默认 100KB） ----

    def test_create_entry_content_between_10kb_and_100kb(self):
        """超过旧 10KB 但在默认 100KB 内的知识可成功入库"""
        r = self._create_entry(content="知" * (60 * 1024 // 3))  # ~60KB UTF-8
        assert r.status_code == 200, f"create failed: {r.text[:200]}"
        kid = r.json().get("id")
        self._cleanup_kid(kid)

    def test_create_entry_content_over_100kb_rejected(self):
        """超过默认 100KB 上限时返回 400 且报错含实际上限"""
        r = self._create_entry(content="x" * (100 * 1024 + 1))
        assert r.status_code == 400
        msg = r.json().get("detail", {}).get("message", "")
        assert "content 不能超过 100KB" in msg

    def test_update_entry_content_between_10kb_and_100kb(self):
        """更新超过旧 10KB 但在默认 100KB 内的知识可成功"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            u = client.put(f"/api/knowledge/{kid}", json={
                "name": "测试知识-更新", "content": "y" * (80 * 1024),
            }, headers=_headers)
            assert u.status_code == 200, f"update failed: {u.text[:200]}"
        finally:
            self._cleanup_kid(kid)

    def test_update_entry_content_over_100kb_rejected(self):
        """更新超过默认 100KB 上限时返回 400 且报错含实际上限"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            u = client.put(f"/api/knowledge/{kid}", json={
                "name": "测试知识-更新", "content": "z" * (100 * 1024 + 1),
            }, headers=_headers)
            assert u.status_code == 400
            msg = u.json().get("detail", {}).get("message", "")
            assert "content 不能超过 100KB" in msg
        finally:
            self._cleanup_kid(kid)

    def test_stats_returns_content_limit(self):
        """stats 接口返回 content_limit_bytes 和 content_limit_display"""
        r = client.get("/api/knowledge/stats?repo=org/test", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert d.get("content_limit_bytes") == 100 * 1024
        assert d.get("content_limit_display") == "100KB"

    def test_max_content_bytes_env_override(self):
        """环境变量 KNOWLEDGE_MAX_CONTENT_BYTES 覆盖默认上限（子进程读取模块常量）"""
        import subprocess
        code = (
            "from src.api.routes.knowledge import MAX_CONTENT_BYTES; "
            "print(MAX_CONTENT_BYTES)"
        )
        env = dict(os.environ, KNOWLEDGE_MAX_CONTENT_BYTES="2097152")
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), env=env,
        )
        assert out.returncode == 0, out.stderr[:300]
        assert out.stdout.strip() == "2097152"

    def test_max_content_bytes_default(self):
        """未设置环境变量时默认 100KB（子进程读取模块常量）"""
        import subprocess
        code = (
            "from src.api.routes.knowledge import MAX_CONTENT_BYTES; "
            "print(MAX_CONTENT_BYTES)"
        )
        env = {k: v for k, v in os.environ.items() if k != "KNOWLEDGE_MAX_CONTENT_BYTES"}
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), env=env,
        )
        assert out.returncode == 0, out.stderr[:300]
        assert out.stdout.strip() == str(100 * 1024)

    def test_max_content_bytes_invalid_falls_back_to_default(self):
        """环境变量为非数字时回退默认 100KB"""
        import subprocess
        code = (
            "from src.api.routes.knowledge import MAX_CONTENT_BYTES; "
            "print(MAX_CONTENT_BYTES)"
        )
        env = dict(os.environ, KNOWLEDGE_MAX_CONTENT_BYTES="abc")
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), env=env,
        )
        assert out.returncode == 0, out.stderr[:300]
        assert out.stdout.strip() == str(100 * 1024)

    def test_max_content_bytes_nonpositive_clamped_to_min(self):
        """环境变量为 0/负数时钳制到下限 1024"""
        import subprocess
        code = (
            "from src.api.routes.knowledge import MAX_CONTENT_BYTES; "
            "print(MAX_CONTENT_BYTES)"
        )
        for bad_value in ("0", "-4096"):
            env = dict(os.environ, KNOWLEDGE_MAX_CONTENT_BYTES=bad_value)
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), env=env,
            )
            assert out.returncode == 0, out.stderr[:300]
            assert out.stdout.strip() == "1024"

    def test_create_entry_desc_too_long(self):
        """description 超过 200 字符"""
        r = self._create_entry(description="x" * 201)
        assert r.status_code == 400
        assert "description 不能超过" in r.json().get("detail", {}).get("message", "")

    # ===== batch 接口校验测试 =====

    def test_batch_invalid_agent_type(self):
        """batch 接口非法 agent_type 校验"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            r = client.post(f"/api/knowledge/{kid}/refs/batch",
                           json={"scene_ids": [], "agent_types": ["invalid_type"]},
                           headers=_headers)
            assert r.status_code == 400
            assert "不支持的 Agent 类型" in r.json().get("detail", {}).get("message", "")
        finally:
            self._cleanup_kid(kid)

    def test_batch_invalid_scene_id(self):
        """batch 接口非法 scene_id 校验"""
        r = self._create_entry()
        kid = r.json().get("id")
        try:
            r = client.post(f"/api/knowledge/{kid}/refs/batch",
                           json={"scene_ids": ["nonexistent-uuid"], "agent_types": []},
                           headers=_headers)
            assert r.status_code == 400
            assert "场景不存在" in r.json().get("detail", {}).get("message", "")
        finally:
            self._cleanup_kid(kid)

    def test_batch_idempotent_skip(self):
        """batch 接口幂等跳过已存在"""
        r = self._create_entry(agent_types=["vod"])
        kid = r.json().get("id")
        try:
            # 再次绑定 vod，应该跳过
            r = client.post(f"/api/knowledge/{kid}/refs/batch",
                           json={"scene_ids": [], "agent_types": ["vod"]},
                           headers=_headers)
            assert r.status_code == 200
            assert r.json().get("added") == 0
        finally:
            self._cleanup_kid(kid)

    # ===== 404 测试 =====

    def test_detail_not_found(self):
        """查询不存在的知识"""
        r = client.get("/api/knowledge/999999", headers=_headers)
        assert r.status_code == 404

    def test_delete_ref_not_found(self):
        """删除不存在的关联记录"""
        r = client.delete("/api/knowledge/ref/999999", headers=_headers)
        assert r.status_code == 404

    # ===== 权限测试 =====

    def test_update_system_knowledge_forbidden(self):
        """编辑系统知识返回 403"""
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, "
                "content_hash, status, source_seq, created_by, source, description, name) "
                "VALUES ('project','gitcode:org/test','',0,'','','',"
                "'accepted',0,'system','system','测试系统知识','sys-test-update')",
            )
            kid = c.lastrowid
            db.commit()
        try:
            r = client.put(f"/api/knowledge/{kid}", json={
                "content": "hacked", "name": "hacked", "description": ""
            }, headers=_headers)
            assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id=?", (kid,))
                db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE id=?", (kid,))
                db.commit()

    def test_delete_system_knowledge_forbidden(self):
        """删除系统知识返回 403"""
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, "
                "content_hash, status, source_seq, created_by, source, description, name) "
                "VALUES ('project','gitcode:org/test','',0,'','','',"
                "'accepted',0,'system','system','测试系统知识','sys-test-delete')",
            )
            kid = c.lastrowid
            db.commit()
        try:
            r = client.delete(f"/api/knowledge/{kid}", headers=_headers)
            assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id=?", (kid,))
                db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE id=?", (kid,))
                db.commit()

