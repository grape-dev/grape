"""Issue #372 §19: get_user_identities_with_tokens 查询条件一致性测试

get_user_identities 已改为 status IN (1, 4)（R10 过期 token 也返回）。
get_user_identities_with_tokens 应同步对齐，否则 status=4 的绑定查不到。

测试覆盖：
- T9-01: status=1 的绑定正常返回
- T9-02: status=4（过期）的绑定也返回
- T9-03: status=0（已解绑）的绑定不返回
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.auth.grape_account_db import (
    create_external_identity,
    get_user_identities_with_tokens,
    mark_identity_expired,
    delete_external_identity,
    create_grape_account,
)


def _cleanup(user_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (user_id,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (user_id,))
        db.commit()


def setup_function():
    pass


def teardown_function():
    pass


def test_status_1_identity_returned():
    """T9-01: status=1 的绑定正常返回。"""
    import uuid as _uuid
    _ext = "ext_" + _uuid.uuid4().hex[:8]
    username = "test_giwt_s1_" + _uuid.uuid4().hex[:6]
    account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
    try:
        create_external_identity(account["id"], "gitcode", _ext, "gcuser",
                                 access_token="tok_active", refresh_token="ref_active")
        identities = get_user_identities_with_tokens(account["id"])
        assert len(identities) == 1
        assert identities[0]["provider"] == "gitcode"
        assert identities[0]["access_token"] == "tok_active"
        assert identities[0]["status"] == 1
    finally:
        _cleanup(account["id"])


def test_status_4_identity_returned():
    """T9-02: status=4（过期）的绑定也返回。

    修复前：get_user_identities_with_tokens 只查 status=1 → 过期绑定查不到。
    修复后：status IN (1, 4) → 过期绑定也返回，调用方用 token 时 API 返回 401。
    """
    import uuid as _uuid
    _ext = "ext_" + _uuid.uuid4().hex[:8]
    username = "test_giwt_s4_" + _uuid.uuid4().hex[:6]
    account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
    try:
        create_external_identity(account["id"], "gitcode", _ext, "gcuser2",
                                 access_token="tok_expired", refresh_token="ref_expired")
        # 标记过期
        assert mark_identity_expired(account["id"], "gitcode")
        identities = get_user_identities_with_tokens(account["id"])
        assert len(identities) == 1, "status=4 的绑定应返回（修复后 status IN (1,4)）"
        assert identities[0]["provider"] == "gitcode"
        assert identities[0]["access_token"] == "tok_expired"
        assert identities[0]["status"] == 4
    finally:
        _cleanup(account["id"])


def test_status_0_identity_not_returned():
    """T9-03: status=0（已解绑）的绑定不返回。"""
    import uuid as _uuid
    _ext = "ext_" + _uuid.uuid4().hex[:8]
    username = "test_giwt_s0_" + _uuid.uuid4().hex[:6]
    account = create_grape_account(username, f"{username}@example.com", "TestPass123!")
    try:
        create_external_identity(account["id"], "gitcode", _ext, "gcuser3",
                                 access_token="tok_unbound", refresh_token="ref_unbound")
        # 软删除（status → 0）
        assert delete_external_identity(account["id"], "gitcode")
        identities = get_user_identities_with_tokens(account["id"])
        assert len(identities) == 0, "status=0 的绑定不应返回"
    finally:
        _cleanup(account["id"])
