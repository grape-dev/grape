"""PR #1507 arch-guard F7/C3 regression: sandbox admin write routes must write
audit logs (operator + ip + identifiers) to the auth audit chain.

Regression: before the fix, all 7 write routes in sandbox_admin.py only did
log.info and never called auth_audit.log_audit — mutations of egress
whitelist / filesystem paths / sandbox config were untraceable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_MOCK_ADMIN = {"id": 1, "username": "admin", "is_admin": 1}


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.deps import require_no_session_token

    app.dependency_overrides[require_no_session_token] = lambda: _MOCK_ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _latest_audit_row(event_type: str):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        row = db.execute(
            "SELECT event_type, operator_id, ip, detail FROM audit_logs "
            "WHERE event_type=? ORDER BY seq DESC LIMIT 1",
            (event_type,)
        ).fetchone()
    return row


def _cleanup(backend_name: str):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_engine_sandbox_whitelist WHERE backend_name=?", (backend_name,))
        c.execute("DELETE FROM grape_t_engine_sandbox_paths WHERE backend_name=?", (backend_name,))
        c.execute("DELETE FROM audit_logs WHERE event_type LIKE 'sandbox_%'")
        db.commit()


class TestSandboxAdminWriteAudit:
    """7 个写路由：白名单 create/update/delete、路径 create/update/delete、配置 update。"""

    def test_whitelist_create_writes_audit(self, client):
        _cleanup("qa-audit-test")
        resp = client.post("/admin/sandbox/whitelist", json={
            "backend_name": "qa-audit-test", "domain": "example.com",
            "method": "GET", "path_prefix": "/", "scheme": "https", "port": 443})
        assert resp.status_code == 200, resp.text
        row = _latest_audit_row("sandbox_whitelist_created")
        assert row is not None, "whitelist create must write audit log"
        assert row[0] == "sandbox_whitelist_created"
        assert str(row[1]) == "1"  # operator_id = mock admin id（uuid 主键列落库为 TEXT）
        assert "qa-audit-test" in (row[3] or "")
        _cleanup("qa-audit-test")

    def test_whitelist_delete_writes_audit(self, client):
        _cleanup("qa-audit-test")
        # 直接落一行再调 delete 路由——审计断言只关心路由层写审计
        from src.kernel.session_manager import db, db_lock
        from src.kernel.sandbox_uid import new_row_uid
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_engine_sandbox_whitelist "
                "(id, backend_name, domain, method, path_prefix, scheme, port) "
                "VALUES (?, 'qa-audit-test', 'example.com', 'GET', '/', 'https', 443)",
                (new_row_uid(),))
            db.commit()
            row = db.execute(
                "SELECT id FROM grape_t_engine_sandbox_whitelist WHERE backend_name='qa-audit-test'"
            ).fetchone()
        rule_id = row[0]
        resp = client.delete(f"/admin/sandbox/whitelist/{rule_id}")
        assert resp.status_code == 200, resp.text
        audit = _latest_audit_row("sandbox_whitelist_deleted")
        assert audit is not None, "whitelist delete must write audit log"
        assert rule_id in (audit[3] or "")
        _cleanup("qa-audit-test")

    def test_paths_create_writes_audit(self, client):
        _cleanup("qa-audit-test")
        resp = client.post("/admin/sandbox/paths", json={
            "backend_name": "qa-audit-test", "path": "/tmp/qa", "access": "ro"})
        assert resp.status_code == 200, resp.text
        row = _latest_audit_row("sandbox_path_created")
        assert row is not None, "path create must write audit log"
        assert "/tmp/qa" in (row[3] or "")
        _cleanup("qa-audit-test")

    def test_config_update_writes_audit(self, client):
        _cleanup("qa-audit-test")
        from src.kernel.session_manager import db, db_lock
        from src.kernel.sandbox_uid import new_row_uid
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_engine_sandbox_config "
                "(id, runtime_id, engine_name, sandbox_mode, degrade_policy) "
                "VALUES (?, 1, 'qa-engine', 'required', 'compatible')",
                (new_row_uid(),))
            db.commit()
            row = db.execute(
                "SELECT id FROM grape_t_engine_sandbox_config WHERE engine_name='qa-engine'"
            ).fetchone()
        config_id = row[0]
        resp = client.put(f"/admin/sandbox/config/{config_id}", json={
            "sandbox_mode": "preferred", "degrade_policy": "strict"})
        assert resp.status_code == 200, resp.text
        audit = _latest_audit_row("sandbox_config_updated")
        assert audit is not None, "config update must write audit log"
        assert "preferred" in (audit[3] or "")
        with db_lock:
            db.execute("DELETE FROM grape_t_engine_sandbox_config WHERE id=?", (config_id,))
            db.commit()
        _cleanup("qa-audit-test")
