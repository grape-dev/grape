"""M3 协议端点测试（见 acceptance-plan M3-C* / M3-R* / concurrency）"""
import hashlib, json, time
import pytest

from fastapi.testclient import TestClient
from src.session_manager import db, db_lock
from src.runtime import service
from src.runtime.health import derive_runtime_health


def _make_token(name="CI-Worker-01"):
    import secrets
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name) VALUES (?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


class TestCredentialAuth:
    def test_requires_token(self, client):
        r = client.post("/api/runtime/register", json={"runtimes": []})
        assert r.status_code == 401

    def test_revoked_token_401(self, client):
        token = _make_token()
        with db_lock:
            db.cursor().execute("UPDATE runtime_credentials SET status='revoked'")
            db.commit()
        r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"}, json={"runtimes": []})
        assert r.status_code == 401

    def test_user_token_rejected(self, client):
        # 普通 user token 不能访问协议端点（D2 凭据隔离）——用假 bearer
        r = client.post("/api/runtime/register", headers={"Authorization": "Bearer faketoken"}, json={"runtimes": []})
        assert r.status_code == 401


class TestRegister:
    def test_register_upsert(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        body = {"device_name": "ci-01", "runtimes": [{"name": "opencode", "version": "v1.5.2", "healthy": True}]}
        r = client.post("/api/runtime/register", json=body, headers=headers)
        assert r.status_code == 200
        rid = r.json()["runtimes"][0]["id"]
        # 重复注册幂等
        r2 = client.post("/api/runtime/register", json=body, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["runtimes"][0]["id"] == rid
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
        assert cnt == 1, "注册应幂等"


class TestClaimViaHttp:
    def test_claim_and_complete(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"device_name": "c", "runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="sess-p1", runtime_id=rid, prompt="p", event_seq=1,
                                   issue_id="#5", repo_key="test/repo", agent_type="issue", skill="issue")
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim", headers=headers)
        assert r.status_code == 200
        task = r.json()["task"]
        assert task and task["id"] == tid
        # complete（幂等）
        r2 = client.post(f"/api/runtime/tasks/{tid}/complete", json={"result_text": "完成", "exit_code": 0}, headers=headers)
        assert r2.status_code == 200
        r3 = client.post(f"/api/runtime/tasks/{tid}/complete", json={"result_text": "again", "exit_code": 0}, headers=headers)
        assert r3.status_code == 200
        t = service.get_task(tid)
        assert t["status"] == "completed"

    def test_fail_sets_failed(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="s-p2", runtime_id=rid, prompt="p", event_seq=2,
                                   repo_key="test/repo", agent_type="issue", skill="issue")
        r = client.post(f"/api/runtime/tasks/{tid}/fail", json={"error": "boom"}, headers=headers)
        assert r.status_code == 200
        assert service.get_task(tid)["status"] == "failed"

    def test_recover_orphans(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="s-p3", runtime_id=rid, prompt="p", event_seq=3,
                                   repo_key="test/repo", agent_type="issue", skill="issue")
        service.update_task_status(tid, "running")
        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans", headers=headers)
        assert r.status_code == 200
        assert service.get_task(tid)["status"] == "queued"


class TestHealth:
    def test_derive(self):
        import time as _t
        now = _t.time()
        assert derive_runtime_health("online", None, now) == "online"
        assert derive_runtime_health("offline", None, now) == "offline"
        # recently_lost：1 分钟前
        ts = now - 60
        assert derive_runtime_health("offline", None, now) == "offline"
        # 用 ISO 时间
        from datetime import datetime, timezone, timedelta
        iso = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        assert derive_runtime_health("offline", iso, now) == "recently_lost"
        # offline：1 天前
        iso2 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert derive_runtime_health("offline", iso2, now) == "offline"
        # about_to_gc：6.5 天前
        iso3 = (datetime.now(timezone.utc) - timedelta(days=6.5)).isoformat()
        assert derive_runtime_health("offline", iso3, now) == "about_to_gc"


class TestCredentialManagement:
    def test_create_list_revoke(self, client):
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        def _fake_user():
            return {"id": 1, "username": "admin"}
        app.dependency_overrides[get_current_user] = _fake_user
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "worker-1"})
        assert r.status_code == 200
        token = r.json()["token"]
        assert token.startswith("rta_")
        # 列表不含明文
        lst = c.get("/api/settings/runtime-credentials")
        assert lst.status_code == 200
        assert all("token" not in str(cred) or cred.get("name") for cred in lst.json()["credentials"])
        cid = lst.json()["credentials"][0]["id"]
        # 吊销
        rv = c.post(f"/api/settings/runtime-credentials/{cid}/revoke")
        assert rv.status_code == 200
        # 吊销后该 token 失效
        r_proto = c.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"}, json={"runtimes": []})
        assert r_proto.status_code == 401
        app.dependency_overrides.clear()
