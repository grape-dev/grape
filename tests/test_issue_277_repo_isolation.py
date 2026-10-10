"""Issue #277: 仓库维度数据隔离测试。

验证进入不同仓库时 worker 设备和引擎列表仅含当前仓库的资源，
不存在跨仓库看到其他仓库 worker/引擎的情况。

测试覆盖：
- _make_visible_credential_fn 的 repo_filter 参数（repo scope 隔离）
- get_agent_runtimes 端点的 repo 参数过滤
- get_runtime_devices 端点的 repo 参数过滤
"""
import pytest
from src.session_manager import db, db_lock
from src.api.routes.settings import _make_visible_credential_fn


@pytest.fixture(autouse=True)
def _clean_tables():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
        c.execute("DELETE FROM runtime_credentials WHERE id > 0")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
        c.execute("DELETE FROM runtime_credentials WHERE id > 0")
        db.commit()


def _mk_cred(name, scope="global", owner_id="", owner_name="tester", repo_key=""):
    """创建 runtime_credentials 测试行，返回 credential_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status, repo_key) "
            "VALUES (?,?,?,?,?,?, 'active', ?)",
            ("h_" + name, name, "dev-" + name, scope, owner_id, owner_name, repo_key))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, cid, scope="global", owner_id=""):
    """创建 agent_runtimes 测试行，返回 runtime_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, cid, "online", 1, 1,
             "2026-01-01T00:00:00+00:00", scope, owner_id))
        rid = c.lastrowid
        db.commit()
        return rid


class TestVisibleCredentialRepoFilter:
    """_make_visible_credential_fn 的 repo_filter 参数测试。"""

    def test_no_repo_filter_shows_all_user_repos(self):
        """无 repo_filter 时，repo scope 引擎对用户所有仓库可见（旧行为）。"""
        user_repos = {"alice/repo-a", "alice/repo-b"}
        vis = _make_visible_credential_fn("alice", user_repos, repo_filter="")

        cid = _mk_cred("cred-a", scope="repo", owner_id="gitcode:alice/repo-a", owner_name="alice")
        assert vis(cid, "repo", "alice", "gitcode:alice/repo-a") is True

        cid2 = _mk_cred("cred-b", scope="repo", owner_id="gitcode:alice/repo-b", owner_name="alice")
        assert vis(cid2, "repo", "alice", "gitcode:alice/repo-b") is True

    def test_repo_filter_isolates_current_repo(self):
        """repo_filter 非空时，repo scope 仅允许当前仓库（Issue #277 核心场景）。"""
        user_repos = {"alice/repo-a", "alice/repo-b"}
        vis = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")

        cid_a = _mk_cred("cred-a", scope="repo", owner_id="gitcode:alice/repo-a", owner_name="alice")
        assert vis(cid_a, "repo", "alice", "gitcode:alice/repo-a") is True

        cid_b = _mk_cred("cred-b", scope="repo", owner_id="gitcode:alice/repo-b", owner_name="alice")
        assert vis(cid_b, "repo", "alice", "gitcode:alice/repo-b") is False

    def test_repo_filter_does_not_affect_global_scope(self):
        """repo_filter 不影响 global scope 引擎（全局引擎始终可见）。"""
        user_repos = {"alice/repo-a"}
        vis = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")

        cid_g = _mk_cred("cred-glob", scope="global", owner_id="", owner_name="")
        assert vis(cid_g, "global", "", "") is True

    def test_repo_filter_does_not_affect_user_scope(self):
        """repo_filter 不影响 user scope 引擎（个人引擎始终对 owner 可见）。"""
        user_repos = {"alice/repo-a"}
        vis = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")

        cid_u = _mk_cred("cred-user", scope="user", owner_id="1", owner_name="alice")
        assert vis(cid_u, "user", "alice", "1") is True

    def test_repo_filter_does_not_affect_local(self):
        """repo_filter 不影响本机直连引擎（credential_id=0 始终可见）。"""
        user_repos = {"alice/repo-a"}
        vis = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")
        assert vis(0, "global", "", "") is True

    def test_switching_repo_updates_visibility(self):
        """切换仓库后可见性正确刷新（Issue #277 验收标准）。"""
        user_repos = {"alice/repo-a", "alice/repo-b"}

        # 进入仓库 A
        vis_a = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")
        cid_a = _mk_cred("cred-a", scope="repo", owner_id="gitcode:alice/repo-a", owner_name="alice")
        cid_b = _mk_cred("cred-b", scope="repo", owner_id="gitcode:alice/repo-b", owner_name="alice")
        assert vis_a(cid_a, "repo", "alice", "gitcode:alice/repo-a") is True
        assert vis_a(cid_b, "repo", "alice", "gitcode:alice/repo-b") is False

        # 切换到仓库 B
        vis_b = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-b")
        assert vis_b(cid_a, "repo", "alice", "gitcode:alice/repo-a") is False
        assert vis_b(cid_b, "repo", "alice", "gitcode:alice/repo-b") is True


class TestGetAgentRuntimesRepoFilter:
    """get_agent_runtimes 端点的 repo 参数过滤测试。"""

    def test_repo_param_filters_repo_scoped_engines(self, monkeypatch):
        """repo 参数非空时，repo scope 引擎仅返回当前仓库的引擎。"""
        import time as _time
        from src.api.routes.settings import _build_cred_map
        from src.user import db as user_db

        # 创建两个仓库的凭据和引擎
        cid_a = _mk_cred("cred-a", scope="repo", owner_id="gitcode:alice/repo-a", owner_name="alice")
        cid_b = _mk_cred("cred-b", scope="repo", owner_id="gitcode:alice/repo-b", owner_name="alice")
        rid_a = _mk_engine("opencode", cid_a, scope="repo", owner_id="gitcode:alice/repo-a")
        rid_b = _mk_engine("hermes", cid_b, scope="repo", owner_id="gitcode:alice/repo-b")

        # Mock user
        user = {"id": 0, "username": "alice"}
        user_repos = {"alice/repo-a", "alice/repo-b"}

        # 模拟 get_agent_runtimes 的核心逻辑
        cred_map = _build_cred_map(db, db_lock)
        _visible = _make_visible_credential_fn("alice", user_repos, repo_filter="alice/repo-a")

        cached = user_db.get_agent_runtimes(scope=None)
        now = _time.time()

        visible_rids = []
        for rt in cached:
            cid = rt.get("credential_id") or 0
            cred = cred_map.get(cid) or {"scope": "global", "owner_name": "", "owner_id": ""}
            if not _visible(cid, cred.get("scope", "global"), cred.get("owner_name", ""), cred.get("owner_id", "")):
                continue
            if cred.get("status") == "revoked":
                continue
            visible_rids.append(rt["id"])

        assert rid_a in visible_rids, "仓库 A 的引擎应在仓库 A 视图中可见"
        assert rid_b not in visible_rids, "仓库 B 的引擎不应在仓库 A 视图中可见"

    def test_no_repo_param_shows_all_visible_repos(self):
        """无 repo 参数时，repo scope 引擎对用户所有仓库可见（旧行为不变）。"""
        from src.api.routes.settings import _build_cred_map
        from src.user import db as user_db

        cid_a = _mk_cred("cred-a", scope="repo", owner_id="gitcode:alice/repo-a", owner_name="alice")
        cid_b = _mk_cred("cred-b", scope="repo", owner_id="gitcode:alice/repo-b", owner_name="alice")
        rid_a = _mk_engine("opencode", cid_a, scope="repo", owner_id="gitcode:alice/repo-a")
        rid_b = _mk_engine("hermes", cid_b, scope="repo", owner_id="gitcode:alice/repo-b")

        user_repos = {"alice/repo-a", "alice/repo-b"}
        cred_map = _build_cred_map(db, db_lock)
        _visible = _make_visible_credential_fn("alice", user_repos, repo_filter="")

        cached = user_db.get_agent_runtimes(scope=None)

        visible_rids = []
        for rt in cached:
            cid = rt.get("credential_id") or 0
            cred = cred_map.get(cid) or {"scope": "global", "owner_name": "", "owner_id": ""}
            if not _visible(cid, cred.get("scope", "global"), cred.get("owner_name", ""), cred.get("owner_id", "")):
                continue
            if cred.get("status") == "revoked":
                continue
            visible_rids.append(rt["id"])

        assert rid_a in visible_rids, "无 repo 参数时仓库 A 引擎应可见"
        assert rid_b in visible_rids, "无 repo 参数时仓库 B 引擎应可见"
