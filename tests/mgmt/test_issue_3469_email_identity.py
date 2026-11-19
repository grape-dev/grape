"""Issue #3469 C / C1 / C2 / C3：邮箱身份三列同步与冲突判定口径。

C   —— `update_email()` 只 `SET email=?`，email_source / email_is_placeholder /
       email_verified 三列与 email 值分叉；
C1  —— 解绑第三方时 `clear_placeholder_email()` 命中「placeholder=1 且
       source==provider」判据，把用户后来绑定的真实邮箱清成 NULL；
C2  —— email_verified 永远停在 0；
C3  —— `is_email_registered()` 过滤 `email_is_placeholder=0`，持有真实邮箱的
       占位账号对冲突校验不可见，别人注册同邮箱时最终撞唯一键 → 500。

测试跑真实 SQLite 测试库（tests/conftest.py 统一指向 data/test_sessions.db，
表结构由 db_setup.init_database() 建出，含 email 唯一索引 uq_active_email）。
"""
import pytest

from src.domains.mgmt.auth.grape_account_db import (
    clear_placeholder_email,
    create_grape_account,
    create_grape_account_oauth,
    get_grape_account_by_id,
    is_email_registered,
    update_email,
)
from src.kernel.session_manager import db, db_lock

TAG = "i3469_mail"
PROVIDER = "gitcode"


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE external_id LIKE ?", (TAG + "%",))
        c.execute("DELETE FROM grape_accounts WHERE username LIKE ?", (TAG + "%",))
        c.execute("DELETE FROM grape_accounts WHERE email LIKE ?", (TAG + "%",))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _raw_row(user_id):
    """直读三列原值（get_grape_account_by_id 把 NULL 邮箱归一成 ''）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT email, email_source, email_is_placeholder, email_verified, updated_at "
            "FROM grape_accounts WHERE id=?",
            (user_id,),
        )
        row = c.fetchone()
    assert row is not None, f"账号不存在: {user_id}"
    return row


def _oauth_placeholder(username):
    """造 OAuth 占位账号：无邮箱、placeholder=1、source=provider、verified=0。"""
    created = create_grape_account_oauth(username, email="", provider=PROVIDER)
    assert isinstance(created, dict), created
    uid = created["id"]
    row = _raw_row(uid)
    assert row and row[0] is None and row[2] == 1 and row[1] == PROVIDER
    return uid


class TestUpdateEmailSyncsIdentityColumns:
    """C / C2：一条 UPDATE 同步 email + 三列。"""

    def test_real_email_sets_source_placeholder_verified(self):
        uid = _oauth_placeholder(TAG + "_oauth_a")

        assert update_email(uid, "Real.User@Example.com") is True

        row = _raw_row(uid)
        assert row[0] == "real.user@example.com"     # 小写归一
        assert row[1] == "user"                     # C1 前置：真实邮箱来源是用户
        assert row[2] == 0                          # C3 前置：不再是占位
        assert row[3] == 1                          # C2：verified 落 1
        assert row[4]                              # updated_at 有值

        acct = get_grape_account_by_id(uid)
        assert acct["email"] == "real.user@example.com"
        assert acct["email_source"] == "user"
        assert acct["email_is_placeholder"] == 0
        assert acct["email_verified"] == 1

    def test_empty_email_resets_to_placeholder_state(self):
        """清空语义：空串 → NULL + source NULL + placeholder 1 + verified 0。"""
        uid = _oauth_placeholder(TAG + "_oauth_b")
        assert update_email(uid, "tmp.user@example.com") is True

        assert update_email(uid, "") is True

        row = _raw_row(uid)
        assert row[0] is None
        assert row[1] is None
        assert row[2] == 1
        assert row[3] == 0

        acct = get_grape_account_by_id(uid)
        assert acct["email"] == ""
        assert acct["email_is_placeholder"] == 1
        assert acct["email_verified"] == 0


class TestUnbindKeepsUserRealEmail:
    """C1 / 验收 6：解绑第三方不得删掉用户自己绑定的真实邮箱。"""

    def test_clear_placeholder_email_keeps_real_email(self):
        uid = _oauth_placeholder(TAG + "_oauth_c")
        assert update_email(uid, "keep.me@example.com") is True

        assert clear_placeholder_email(uid, PROVIDER) is True

        row = _raw_row(uid)
        assert row[0] == "keep.me@example.com"   # C1：真实邮箱仍在
        assert row[1] == "user"
        assert row[2] == 0

        acct = get_grape_account_by_id(uid)
        assert acct["email"] == "keep.me@example.com"

    def test_clear_requires_both_placeholder_and_provider_source(self):
        """判据锁：只有 placeholder=1 且 source==provider 才清（两个条件缺一不可）。"""
        uid = _oauth_placeholder(TAG + "_oauth_d")
        # 真实邮箱写进 email 列，但保留占位标记、来源换成别的 provider → 不清
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET email=?, email_is_placeholder=1, "
                "email_source=? WHERE id=?",
                (TAG + ".p@example.com", "github", uid),
            )
            db.commit()

        assert clear_placeholder_email(uid, PROVIDER) is True

        row = _raw_row(uid)
        assert row[0] == TAG + ".p@example.com"  # provider 不匹配 → 不动
        assert row[1] == "github"


class TestIsEmailRegisteredAlignment:
    """C3：占位账号持有真实邮箱时必须参与冲突判定。"""

    def test_placeholder_row_holding_real_email_is_visible(self):
        uid = _oauth_placeholder(TAG + "_oauth_e")
        email = TAG + ".hidden@example.com"
        # 复现 C3 的数据形态：email 列有真实邮箱，但 email_is_placeholder 仍是 1
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_accounts SET email=?, email_is_placeholder=1 WHERE id=?",
                (email, uid),
            )
            db.commit()

        assert is_email_registered(email) is True

    def test_private_email_form_still_excluded(self):
        """私有/非邮箱形态仍不参与冲突（保留 is_placeholder_email 排除）。"""
        assert is_email_registered("someone@noreply.gitcode.com") is False
        assert is_email_registered("未公开邮箱") is False

    def test_empty_string_not_counted(self):
        """Issue #3469 O 的 '' 语义：空串行不制造假冲突。"""
        uid = _oauth_placeholder(TAG + "_oauth_f")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE grape_accounts SET email='' WHERE id=?", (uid,))
            db.commit()

        assert is_email_registered("") is False


class TestDuplicateBindingConflict:
    """验收 7 的单线程面：同邮箱绑到第二个账号返回 "conflict"（并发面由路由 409 兜底）。"""

    def test_second_account_gets_conflict(self):
        uid1 = _oauth_placeholder(TAG + "_oauth_g1")
        uid2 = _oauth_placeholder(TAG + "_oauth_g2")
        email = TAG + ".dup@example.com"

        assert update_email(uid1, email) is True
        assert update_email(uid2, email) == "conflict"

        # uid2 的邮箱未被改动，uid1 保持持有
        assert _raw_row(uid2)[0] is None
        assert _raw_row(uid1)[0] == email

    def test_register_path_precheck_and_create_agree(self):
        """C3 闭环：冲突前置校验与落库唯一键同口径，不会再 500。"""
        email = TAG + ".precheck@example.com"
        assert create_grape_account(TAG + "_reg_a", email, "Pw#12345678") is not None
        assert is_email_registered(email) is True
        assert create_grape_account(TAG + "_reg_b", email, "Pw#12345678") is None


def test_clear_placeholder_email_zeroes_verified():
    """复审 P3：清空邮箱时 email_verified 一并归零（否则留 NULL + verified=1 的畸形）。"""
    uid = _oauth_placeholder(TAG + "_oauth_clear")
    with db_lock:
        c = db.cursor()
        # 造「占位账号持有 provider 私有邮箱 + verified=1」的解绑前形态
        c.execute(
            "UPDATE grape_accounts SET email=?, email_source=?, email_is_placeholder=1, "
            "email_verified=1 WHERE id=?",
            ("1+u@noreply." + PROVIDER + ".com", PROVIDER, uid))
        db.commit()

    assert clear_placeholder_email(uid, PROVIDER) is True

    row = _raw_row(uid)
    assert row[0] is None           # email 清空
    assert row[2] == 1              # 占位语义
    assert int(row[3] or 0) == 0    # verified 归零
