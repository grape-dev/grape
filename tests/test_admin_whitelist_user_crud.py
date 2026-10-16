"""Issue #348 + #332: Admin OAuth Login Whitelist + User CRUD tests.

Tests the 7 new admin endpoints:
  1. GET    /api/admin/whitelist          — list login whitelist
  2. POST   /api/admin/whitelist          — add entry
  3. DELETE /api/admin/whitelist/{id}     — delete entry
  4. PUT    /api/admin/whitelist/toggle   — toggle whitelist on/off
  5. POST   /api/admin/users              — create user
  6. PUT    /api/admin/users/{id}         — edit user
  7. POST   /api/admin/users/{id}/reset-password — reset password

Uses real SQLite test DB (via conftest.py init_database) + TestClient with
dependency_overrides to bypass JWT auth.  No mock DB functions — exercises
the full DB layer (admin/db.py → session_manager.db).
"""
import pytest
from fastapi.testclient import TestClient

_MOCK_ADMIN = {"id": 1, "username": "admin", "token_type": "admin"}


@pytest.fixture
def client():
    """TestClient with mocked admin auth — real DB, no JWT."""
    from src.api.main import app
    from src.api.deps import require_admin

    app.dependency_overrides[require_admin] = lambda: _MOCK_ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helpers ──

def _cleanup_whitelist():
    """Remove all login_whitelist entries and reset toggle."""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM login_whitelist")
        db.commit()
    from src.user.db import set_meta_value
    set_meta_value("login_whitelist_enabled", "false")


def _cleanup_user(username: str):
    """Remove a test user from grape_accounts."""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE LOWER(username)=LOWER(?)", (username,))
        db.commit()


# ── OAuth Login Whitelist ──

class TestLoginWhitelist:

    def test_list_empty(self, client):
        """GET /whitelist returns empty list + enabled=false when no entries."""
        _cleanup_whitelist()
        r = client.get("/api/admin/whitelist")
        assert r.status_code == 200
        d = r.json()
        assert d["whitelist"] == []
        assert d["enabled"] is False

    def test_add_and_list_gitcode(self, client):
        """POST /whitelist adds gitcode entry, GET shows it."""
        _cleanup_whitelist()
        r = client.post("/api/admin/whitelist", json={
            "provider": "gitcode", "username": "testuser_gc", "remark": "test"
        })
        assert r.status_code == 200, r.text

        r2 = client.get("/api/admin/whitelist")
        assert r2.status_code == 200
        wl = r2.json()["whitelist"]
        assert len(wl) == 1
        assert wl[0]["gitcode_username"] == "testuser_gc"
        assert wl[0]["remark"] == "test"

    def test_add_and_list_github(self, client):
        """POST /whitelist adds github entry."""
        _cleanup_whitelist()
        r = client.post("/api/admin/whitelist", json={
            "provider": "github", "username": "testuser_gh", "remark": ""
        })
        assert r.status_code == 200, r.text

        r2 = client.get("/api/admin/whitelist")
        wl = r2.json()["whitelist"]
        assert len(wl) == 1
        assert wl[0]["github_username"] == "testuser_gh"

    def test_add_duplicate_returns_409(self, client):
        """POST /whitelist with duplicate entry returns 409."""
        _cleanup_whitelist()
        client.post("/api/admin/whitelist", json={
            "provider": "gitcode", "username": "dup_user", "remark": ""
        })
        r = client.post("/api/admin/whitelist", json={
            "provider": "gitcode", "username": "dup_user", "remark": ""
        })
        assert r.status_code == 409, r.text

    def test_add_invalid_provider_returns_400(self, client):
        """POST /whitelist with invalid provider returns 400."""
        _cleanup_whitelist()
        r = client.post("/api/admin/whitelist", json={
            "provider": "gitlab", "username": "xxx", "remark": ""
        })
        assert r.status_code == 400, r.text

    def test_add_empty_username_returns_400(self, client):
        """POST /whitelist with empty username returns 400."""
        _cleanup_whitelist()
        r = client.post("/api/admin/whitelist", json={
            "provider": "gitcode", "username": "  ", "remark": ""
        })
        assert r.status_code == 400, r.text

    def test_delete_entry(self, client):
        """DELETE /whitelist/{id} removes entry."""
        _cleanup_whitelist()
        client.post("/api/admin/whitelist", json={
            "provider": "gitcode", "username": "del_user", "remark": ""
        })
        r_list = client.get("/api/admin/whitelist")
        item_id = r_list.json()["whitelist"][0]["id"]

        r = client.delete(f"/api/admin/whitelist/{item_id}")
        assert r.status_code == 200, r.text

        r2 = client.get("/api/admin/whitelist")
        assert len(r2.json()["whitelist"]) == 0

    def test_delete_nonexistent_returns_404(self, client):
        """DELETE /whitelist/{99999} returns 404."""
        _cleanup_whitelist()
        r = client.delete("/api/admin/whitelist/99999")
        assert r.status_code == 404, r.text

    def test_toggle_enable(self, client):
        """PUT /whitelist/toggle sets enabled=true."""
        _cleanup_whitelist()
        r = client.put("/api/admin/whitelist/toggle", json={"enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True

        # Verify via GET
        r2 = client.get("/api/admin/whitelist")
        assert r2.json()["enabled"] is True

    def test_toggle_disable(self, client):
        """PUT /whitelist/toggle sets enabled=false."""
        _cleanup_whitelist()
        client.put("/api/admin/whitelist/toggle", json={"enabled": True})
        r = client.put("/api/admin/whitelist/toggle", json={"enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False

        r2 = client.get("/api/admin/whitelist")
        assert r2.json()["enabled"] is False


# ── User CRUD ──

class TestUserCRUD:

    def test_create_user_success(self, client):
        """POST /users creates a user in grape_accounts."""
        _cleanup_user("crud_test_user")
        r = client.post("/api/admin/users", json={
            "username": "crud_test_user",
            "email": "crud@example.com",
            "password": "testpass123",
            "nickname": "CRUD Test"
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["username"] == "crud_test_user"
        assert d["email"] == "crud@example.com"
        assert d["nickname"] == "CRUD Test"
        assert "id" in d and isinstance(d["id"], int)
        _cleanup_user("crud_test_user")

    def test_create_user_short_password_returns_400(self, client):
        """POST /users with password < 8 chars returns 400."""
        _cleanup_user("crud_short_pw")
        r = client.post("/api/admin/users", json={
            "username": "crud_short_pw",
            "email": "",
            "password": "short",
            "nickname": ""
        })
        assert r.status_code == 400, r.text
        _cleanup_user("crud_short_pw")

    def test_create_user_short_username_returns_400(self, client):
        """POST /users with username < 3 chars returns 400."""
        r = client.post("/api/admin/users", json={
            "username": "ab",
            "email": "",
            "password": "validpass123",
            "nickname": ""
        })
        assert r.status_code == 400, r.text

    def test_create_user_duplicate_returns_409(self, client):
        """POST /users with existing username returns 409."""
        _cleanup_user("crud_dup_user")
        client.post("/api/admin/users", json={
            "username": "crud_dup_user",
            "email": "dup@example.com",
            "password": "validpass123",
            "nickname": ""
        })
        r = client.post("/api/admin/users", json={
            "username": "crud_dup_user",
            "email": "other@example.com",
            "password": "validpass123",
            "nickname": ""
        })
        assert r.status_code == 409, r.text
        _cleanup_user("crud_dup_user")

    def test_edit_user_email_nickname(self, client):
        """PUT /users/{id} updates email and nickname."""
        _cleanup_user("crud_edit_user")
        r_create = client.post("/api/admin/users", json={
            "username": "crud_edit_user",
            "email": "before@example.com",
            "password": "validpass123",
            "nickname": "Before"
        })
        uid = r_create.json()["id"]

        r = client.put(f"/api/admin/users/{uid}", json={
            "email": "after@example.com",
            "nickname": "After"
        })
        assert r.status_code == 200, r.text

        # Verify via DB
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT email, nickname FROM grape_accounts WHERE id=?", (uid,))
            row = c.fetchone()
        assert row[0] == "after@example.com"
        assert row[1] == "After"
        _cleanup_user("crud_edit_user")

    def test_edit_user_not_found_returns_404(self, client):
        """PUT /users/99999 returns 404."""
        r = client.put("/api/admin/users/99999", json={
            "email": "none@example.com",
            "nickname": "None"
        })
        assert r.status_code == 404, r.text

    def test_reset_password(self, client):
        """POST /users/{id}/reset-password resets password + bumps token_version."""
        _cleanup_user("crud_reset_user")
        r_create = client.post("/api/admin/users", json={
            "username": "crud_reset_user",
            "email": "reset@example.com",
            "password": "original123",
            "nickname": ""
        })
        uid = r_create.json()["id"]

        # Get original token_version
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (uid,))
            orig_tv = c.fetchone()[0]

        r = client.post(f"/api/admin/users/{uid}/reset-password", json={
            "password": "newpass456"
        })
        assert r.status_code == 200, r.text

        # Verify token_version incremented
        with db_lock:
            c = db.cursor()
            c.execute("SELECT token_version, password_hash FROM grape_accounts WHERE id=?", (uid,))
            row = c.fetchone()
        assert row[0] == orig_tv + 1
        assert row[1] != "original123"  # hash changed
        _cleanup_user("crud_reset_user")

    def test_reset_password_short_returns_400(self, client):
        """POST /users/{id}/reset-password with short password returns 400."""
        _cleanup_user("crud_reset_short")
        r_create = client.post("/api/admin/users", json={
            "username": "crud_reset_short",
            "email": "",
            "password": "validpass123",
            "nickname": ""
        })
        uid = r_create.json()["id"]
        r = client.post(f"/api/admin/users/{uid}/reset-password", json={
            "password": "short"
        })
        assert r.status_code == 400, r.text
        _cleanup_user("crud_reset_short")

    def test_reset_password_not_found_returns_404(self, client):
        """POST /users/99999/reset-password returns 404."""
        r = client.post("/api/admin/users/99999/reset-password", json={
            "password": "validpass123"
        })
        assert r.status_code == 404, r.text


# ── Case-insensitive unique constraint ──

class TestCaseInsensitive:

    def test_duplicate_username_different_case(self, client):
        """Creating 'TestUser' then 'testuser' should be rejected (case-insensitive)."""
        _cleanup_user("ci_test_user")
        r1 = client.post("/api/admin/users", json={
            "username": "ci_test_user",
            "email": "ci1@example.com",
            "password": "validpass123",
            "nickname": ""
        })
        assert r1.status_code == 200, r1.text

        # Different case should conflict
        r2 = client.post("/api/admin/users", json={
            "username": "CI_TEST_USER",
            "email": "ci2@example.com",
            "password": "validpass123",
            "nickname": ""
        })
        assert r2.status_code == 409, r2.text
        _cleanup_user("ci_test_user")
