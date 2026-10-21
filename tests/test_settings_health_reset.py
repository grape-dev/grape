"""Issue #394: 仓库级/个人级 worker 设备 runtime 引擎「重置健康分」接口测试。

覆盖：
- 401：未认证访问
- 404：不存在 / 不可见引擎
- 403：global scope 引擎（平台级，不开放重置）
- 200：user scope（个人设备 owner 本人）成功重置 + health_reset_at 落库
- 200：repo scope（仓库成员）成功重置
- 503：底层 reset_health 失败
"""
import hashlib
import secrets
from unittest.mock import patch

import pytest

from src.session_manager import db, db_lock


def _mk_cred(name, scope="user", owner_name="me", status="active", repo_key=""):
    token = "rta_" + secrets.token_urlsafe(24)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, owner_id, owner_name, repo_key, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), name, scope, "", owner_name,
             repo_key or "", status))
        cid = c.lastrowid
        db.commit()
    return cid


def _mk_runtime(cid, name="opencode", scope="user"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, scope, owner_id, status, healthy, installed, "
            "last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, name, scope, "", "online", 1, 1, "2026-10-20T12:00:00+00:00"))
        rid = c.lastrowid
        db.commit()
    return rid


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'hr_%' "
                            "OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'hr_%')")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'hr_%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'hr_%' "
                            "OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'hr_%')")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'hr_%'")
        db.commit()


@pytest.fixture
def org_client():
    """注入当前用户（组织成员）的 TestClient。"""
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app
    app.dependency_overrides[require_org_member] = lambda: {
        "id": 1, "username": "me", "org_id": 0, "org_role": "owner", "auth_type": "jwt"}
    try:
        with TestClient(app) as cli:
            yield cli
    finally:
        app.dependency_overrides.pop(require_org_member, None)


@pytest.fixture
def anon_client():
    """无认证 TestClient（不注入用户依赖）。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    with TestClient(app) as cli:
        yield cli


class TestHealthResetAPI:
    def test_unauthenticated_returns_401(self, anon_client):
        """未认证 → 401。"""
        cid = _mk_cred("hr_unauth", scope="user", owner_name="me")
        rid = _mk_runtime(cid, name="hr_unauth_rt", scope="user")
        r = anon_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset")
        assert r.status_code == 401

    def test_missing_runtime_returns_404(self, org_client):
        """不存在的引擎 → 404。"""
        r = org_client.post("/api/settings/agent-runtimes/99999999/health-reset")
        assert r.status_code == 404

    def test_invisible_runtime_returns_404(self, org_client):
        """他人个人设备引擎（不可见）→ 404。"""
        cid = _mk_cred("hr_other", scope="user", owner_name="someone-else", status="active")
        rid = _mk_runtime(cid, name="hr_other_rt", scope="user")
        r = org_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset")
        assert r.status_code == 404, r.text

    def test_global_runtime_returns_403(self, org_client):
        """global scope 引擎（平台级）→ 403，不开放重置。"""
        cid = _mk_cred("hr_global", scope="global", owner_name="me", status="active")
        rid = _mk_runtime(cid, name="hr_global_rt", scope="global")
        r = org_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset")
        assert r.status_code == 403, r.text

    def test_user_scope_reset_success(self, org_client):
        """个人级（user scope）设备引擎 → 200，health_reset_at 写入并返回新健康分。"""
        cid = _mk_cred("hr_user", scope="user", owner_name="me", status="active")
        rid = _mk_runtime(cid, name="hr_user_rt", scope="user")
        r = org_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["runtime_id"] == rid
        assert body["health_reset_at"]
        assert isinstance(body["health_score"], int)
        # DB 落库校验
        with db_lock:
            c = db.cursor()
            c.execute("SELECT health_reset_at FROM agent_runtimes WHERE id=?", (rid,))
            row = c.fetchone()
        assert row and row[0]

    def test_repo_scope_reset_success(self, org_client):
        """仓库级（repo scope）设备引擎（仓库成员）→ 200。"""
        cid = _mk_cred("hr_repo", scope="repo", owner_name="me", status="active",
                       repo_key="gitcode:hr/repo")
        rid = _mk_runtime(cid, name="hr_repo_rt", scope="repo")
        r = org_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset",
                            params={"repo": "hr/repo"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["runtime_id"] == rid
        assert body["health_reset_at"]

    def test_reset_failure_returns_503(self, org_client):
        """health_scorer.reset_health 失败（返回 None）→ 503。"""
        cid = _mk_cred("hr_fail", scope="user", owner_name="me", status="active")
        rid = _mk_runtime(cid, name="hr_fail_rt", scope="user")
        with patch("src.runtime.health_scorer.reset_health", return_value=None):
            r = org_client.post(f"/api/settings/agent-runtimes/{rid}/health-reset")
        assert r.status_code == 503, r.text