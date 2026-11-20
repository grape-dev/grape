"""Issue #827: 「我的需求」过滤口径扩展回归测试。

覆盖：user_id=0 但 created_by 或 assignee 命中当前用户 GitCode 用户名时，卡片在 /mine 可见。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.domains.mgmt.auth import grape_account_db
from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:mine827/demo"
MINE_USER = "mine_test_user_827"
MINE_GC_LOGIN = "mine_gc_login_827"


def _jwt_access_token(user_id, username):
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


client = TestClient(app)
_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass
    _uid = grape_account_db.create_grape_account(MINE_USER, email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username(MINE_USER)
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM external_identities WHERE provider='gitcode' AND external_login=?",
                  (MINE_GC_LOGIN,))
        db.commit()
    # D105/身份域口径：user_id 反查登录名走 external_identities（unified_user_mapping 仅存量兜底）
    grape_account_db.create_external_identity(
        _uid["id"], "gitcode", external_id=f"mine827-{MINE_GC_LOGIN}", external_login=MINE_GC_LOGIN)


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM external_identities WHERE provider='gitcode' AND external_login=?",
                  (MINE_GC_LOGIN,))
        db.commit()


def _insert_issue(num, user_id, assignee, created_by, title="test card"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (user_id, assignee, REPO_KEY, num, title, created_by))
        db.commit()


class TestMineFilterCreatedByFallback:
    """user_id=0 但 created_by 命中当前用户 → /mine 可见。"""

    def test_card_with_zero_user_id_but_matching_created_by_visible(self):
        _insert_issue(200, 0, "someone_else", MINE_GC_LOGIN, "created_by me")
        resp = client.get("/api/issues/mine", headers=_headers)
        assert resp.status_code == 200
        nums = [i["issue_number"] for i in resp.json().get("issues", [])]
        assert 200 in nums, f"card #200 should be visible via created_by fallback, got {nums}"


class TestMineFilterAssigneeFallback:
    """user_id=0 但 assignee 命中当前用户 → /mine 可见。"""

    def test_card_with_zero_user_id_but_matching_assignee_visible(self):
        _insert_issue(201, 0, MINE_GC_LOGIN, "someone_else", "assigned to me")
        resp = client.get("/api/issues/mine", headers=_headers)
        assert resp.status_code == 200
        nums = [i["issue_number"] for i in resp.json().get("issues", [])]
        assert 201 in nums, f"card #201 should be visible via assignee fallback, got {nums}"


class TestMineFilterUserIdStillPrimary:
    """user_id 直接命中仍然可见（不退化）。"""

    def test_card_with_matching_user_id_visible(self):
        _insert_issue(202, _uid["id"], "someone_else", "someone_else", "my user_id")
        resp = client.get("/api/issues/mine", headers=_headers)
        assert resp.status_code == 200
        nums = [i["issue_number"] for i in resp.json().get("issues", [])]
        assert 202 in nums, f"card #202 should be visible via user_id, got {nums}"
