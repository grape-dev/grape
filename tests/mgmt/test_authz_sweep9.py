"""扫雷⑨ authz 五高危修复回归测试（d105h-authz-audit-sweep9 H1-H5）。

每处补一条负向（他人/未授权 → 403 或非 200）+ 一条正向（本人/管理员 → 200）：
- H1 GET /repos/{owner}/{repo}/project-info：JWT 补 check_repo_access
- H2 POST /settings/runtime-credentials/{cid}/revoke：补 owner 校验
- H3 PUT /runtime/runtimes/{rid}/tags|max-concurrent|scene-tags|alias：补归属校验
- H4 /runtime/admin/*：require_no_session_token → require_admin
- H5 GET /task-logs/{task_id}/info|messages|stream：补任务归属校验

模式仿 test_repo_access_coverage.py：alice(uid=1) 拥有 gitcode:alice/repo1，
bob(uid=2) 拥有 gitcode:bob/repo2；dependency_overrides 注入 JWT 身份。
"""
import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock

ALICE = {"id": "1", "username": "s9_alice", "auth_type": "jwt"}
BOB = {"id": "2", "username": "s9_bob", "auth_type": "jwt"}
ALICE_REPO = "gitcode:s9alice/repo1"

_current = {"user": ALICE}


def _fake_user():
    return dict(_current["user"])


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user_or_api_key, None)


@pytest.fixture(autouse=True)
def _seed():
    from src.domains.mgmt.user import db as user_db
    user_db.add_managed_repo(ALICE_REPO, "s9alice", "repo1")
    user_db.mark_user_repo_join(ALICE["id"], ALICE_REPO, is_join=True, is_admin=True)
    with db_lock:
        c = db.cursor()
        # 清理本模块遗留
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 's9_%' OR credential_id IN "
                  "(SELECT id FROM runtime_credentials WHERE name LIKE 's9_%')")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 's9_%'")
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 's9_%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 's9_%' OR credential_id IN "
                  "(SELECT id FROM runtime_credentials WHERE name LIKE 's9_%')")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 's9_%'")
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 's9_%'")
        db.commit()
    from src.domains.mgmt.user import db as user_db2
    user_db2.remove_managed_repo(ALICE_REPO)


# ---------- H1 project-info ----------

class TestH1ProjectInfo:
    def test_other_user_403(self, client):
        _current["user"] = BOB
        r = client.get("/api/repos/s9alice/repo1/project-info")
        assert r.status_code == 403, r.text

    def test_owner_200(self, client):
        _current["user"] = ALICE
        r = client.get("/api/repos/s9alice/repo1/project-info")
        assert r.status_code == 200, r.text
        assert "files" in r.json() and "meta" in r.json()


# ---------- H2 credential revoke ----------

def _mk_cred(owner_name):
    token = "rta_" + secrets.token_urlsafe(24)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,'active')",
            (hashlib.sha256(token.encode()).hexdigest(), owner_name, "user", "", owner_name))
        cid = c.lastrowid
        db.commit()
    return cid


class TestH2CredRevoke:
    def test_other_user_403(self, client):
        cid = _mk_cred("s9_alice")
        _current["user"] = BOB
        r = client.post(f"/api/settings/runtime-credentials/{cid}/revoke")
        assert r.status_code == 403, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT 1 FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
        assert row  # 未被误删

    def test_owner_200(self, client):
        cid = _mk_cred("s9_alice")
        _current["user"] = ALICE
        r = client.post(f"/api/settings/runtime-credentials/{cid}/revoke")
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT 1 FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
        assert not row


# ---------- H3 runtime config PUTs ----------

def _mk_runtime(scope="user", owner_id="1", repo_key=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, scope, owner_id, repo_key, status, "
            "healthy, installed, last_seen_at) VALUES (0, 's9_rt', ?, ?, ?, 'online', 1, 1, "
            "'2026-11-01T00:00:00+00:00')",
            (scope, owner_id, repo_key))
        rid = c.lastrowid
        db.commit()
    return rid


class TestH3RuntimeConfig:
    def test_other_user_403_all_four(self, client):
        rid = _mk_runtime()
        _current["user"] = BOB
        for path, body in (
            (f"/api/runtime/runtimes/{rid}/tags", {"tags": ["vod"]}),
            (f"/api/runtime/runtimes/{rid}/max-concurrent", {"max_concurrent": 1}),
            (f"/api/runtime/runtimes/{rid}/scene-tags", {"scene_tags": ["default"]}),
            (f"/api/runtime/runtimes/{rid}/alias", {"alias": "x"}),
        ):
            r = client.put(path, json=body)
            assert r.status_code == 403, (path, r.text)

    def test_owner_200_all_four(self, client):
        rid = _mk_runtime()
        _current["user"] = ALICE
        for path, body, key in (
            (f"/api/runtime/runtimes/{rid}/tags", {"tags": ["vod"]}, "tags"),
            (f"/api/runtime/runtimes/{rid}/max-concurrent", {"max_concurrent": 5}, "max_concurrent"),
            (f"/api/runtime/runtimes/{rid}/scene-tags", {"scene_tags": ["default"]}, "scene_tags"),
            (f"/api/runtime/runtimes/{rid}/alias", {"alias": "s9"}, "alias"),
        ):
            r = client.put(path, json=body)
            assert r.status_code == 200, (path, r.text)


# ---------- H4 /runtime/admin/* ----------

class TestH4AdminEndpoints:
    def test_unauthenticated_not_200(self):
        from src.api.main import app
        # 真实 require_admin：无 token → 401/403（绝不能 200）
        with TestClient(app) as cli:
            r = cli.get("/api/runtime/admin/machine-health")
            assert r.status_code in (401, 403), r.text
            r2 = cli.post("/api/runtime/admin/batch-recover")
            assert r2.status_code in (401, 403), r2.text

    def test_admin_200(self):
        from src.api.main import app
        from src.api.deps import require_admin
        app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
        try:
            with TestClient(app) as cli:
                r = cli.get("/api/runtime/admin/machine-health")
                assert r.status_code == 200, r.text
                r2 = cli.get("/api/runtime/admin/event-bus")
                assert r2.status_code == 200, r2.text
        finally:
            app.dependency_overrides.pop(require_admin, None)


# ---------- H5 task-logs ----------

def _mk_task(task_id="s9_t1", user_id="1", repo_key=ALICE_REPO):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status, user_id, repo_key) "
            "VALUES (?, 's9_s', 0, 'p', 'running', ?, ?)",
            (task_id, user_id, repo_key))
        db.commit()


class TestH5TaskLogs:
    def test_other_user_403(self, client):
        _mk_task()
        _current["user"] = BOB
        for path in ("/api/task-logs/s9_t1/info",
                     "/api/task-logs/s9_t1/messages",
                     "/api/task-logs/s9_t1/stream"):
            r = client.get(path)
            assert r.status_code == 403, (path, r.text)

    def test_owner_200(self, client):
        _mk_task()
        _current["user"] = ALICE
        r = client.get("/api/task-logs/s9_t1/info")
        assert r.status_code == 200, r.text
        r2 = client.get("/api/task-logs/s9_t1/messages")
        assert r2.status_code == 200, r2.text

    def test_missing_task_404(self, client):
        _current["user"] = ALICE
        r = client.get("/api/task-logs/s9_no_such/info")
        assert r.status_code == 404, r.text
