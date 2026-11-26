"""refresh_token 读取口径回归：status=4（token 过期、绑定仍在）也须可读。

过期后唯一的自动自愈路径是用行内 refresh_token 续期；只读 status=1 会让过期行
永远读不到 refresh_token（日志表现为 "no refresh_token"），把可续期误报成
「用户没给刷新权限」。本文件独立成篇，不追加到基线上已红的既有用例文件里。
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock


def _cleanup(account_id, external_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE external_id=?", (external_id,))
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (str(account_id),))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (str(account_id),))
        db.commit()


def test_refresh_token_readable_after_identity_expired():
    """mark_identity_expired 之后（status=4）仍能读到 refresh_token 明文。"""
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt.auth.grape_account_db import (
        create_grape_account, create_external_identity, mark_identity_expired,
        get_external_identity_any_status,
    )

    tag = uuid.uuid4().hex[:12]
    account = create_grape_account(username=f"test_rt4_{tag}", email="", password="")
    assert account is not None
    ext_id = f"test_ext_rt4_{tag}"
    try:
        assert create_external_identity(
            grape_user_id=account["id"], provider="gitcode",
            external_id=ext_id, external_login=f"login_{tag}",
            access_token="access-4", refresh_token="refresh-4")
        assert mark_identity_expired(account["id"], "gitcode")
        row = get_external_identity_any_status("gitcode", ext_id)
        assert row and int(row["status"]) == 4, "前提：身份行已被标记为过期(4)"

        assert user_db._read_refresh_token_from_identity(account["id"], "gitcode") == "refresh-4"
    finally:
        _cleanup(account["id"], ext_id)


def test_refresh_token_readable_for_active_identity():
    """status=1 的既有语义不回退。"""
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt.auth.grape_account_db import (
        create_grape_account, create_external_identity,
    )

    tag = uuid.uuid4().hex[:12]
    account = create_grape_account(username=f"test_rt1_{tag}", email="", password="")
    assert account is not None
    ext_id = f"test_ext_rt1_{tag}"
    try:
        assert create_external_identity(
            grape_user_id=account["id"], provider="gitcode",
            external_id=ext_id, external_login=f"login_{tag}",
            access_token="access-1", refresh_token="refresh-1")
        assert user_db._read_refresh_token_from_identity(account["id"], "gitcode") == "refresh-1"
    finally:
        _cleanup(account["id"], ext_id)