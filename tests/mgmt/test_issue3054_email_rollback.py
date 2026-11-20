"""Issue #3054 遗留闭环：provider 私有邮箱归一 + 建号失败回滚不占坑。

覆盖两条 user-facing 缺口（v1 只修了 status=4 死锁，邮箱这层没动）：

1. **私有邮箱不落库**：gitee/gitcode/github 的 noreply 邮箱不可收信，若按真实邮箱
   入库会占 email 唯一键、且让登录推进「注册新账号 / 关联已有账号」二选一页；
2. **建号失败回滚释放凭据**：rolled-back 账号原先走注销冷静期（status=3）保留
   email/username 30 天，同一邮箱/用户名对所有人不可用，重登还会撞唯一键。
"""
import pytest

from src.domains.mgmt.auth.grape_account_db import (
    create_grape_account_oauth,
    get_grape_account_by_id,
    release_failed_signup_account,
)
from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy
from src.kernel.session_manager import db, db_lock

TAG = "i3054_mail"
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


class TestProviderEmailPolicy:
    """归一判据：私有邮箱 → 不落库、不参与冲突。"""

    @pytest.mark.parametrize("email", [
        "12345+user@users.noreply.github.com",
        "abc@noreply.gitcode.com",
        "a@noreply.gitee.com",
        "x@noreply.gitlab.com",
        "relay@privaterelay.appleid.com",
    ])
    def test_private_domains_detected(self, email):
        assert ProviderEmailPolicy.is_private(email) is True
        assert ProviderEmailPolicy.for_storage(email) == (None, 1)
        assert ProviderEmailPolicy.is_conflict_eligible(email) is False

    @pytest.mark.parametrize("email", ["alice@example.com", "bob@corp.cn", "USER@Example.COM"])
    def test_real_email_kept_lowercase(self, email):
        stored, placeholder = ProviderEmailPolicy.for_storage(email)
        assert placeholder == 0
        assert stored == email.lower()

    @pytest.mark.parametrize("raw", ["未公开邮箱", "not-an-email", "a@b", " x y@z.com ", "@x.com",
                                     "未 公开@邮箱.com"])
    def test_non_email_shape_is_placeholder(self, raw):
        """gitee 私密邮箱下发的是字面串（无 @ 或不成形态）→ 不得入库、不得参与冲突。

        实锤：ProviderEmailPolicy.for_storage('未公开邮箱') 历史上返回
        ('未公开邮箱', 0)，于是占 email 唯一键并在下次登录撞 email_conflict。
        """
        assert ProviderEmailPolicy.is_private(raw) is True
        assert ProviderEmailPolicy.for_storage(raw) == (None, 1)
        assert ProviderEmailPolicy.is_conflict_eligible(raw) is False

    def test_empty_email_is_placeholder(self):
        assert ProviderEmailPolicy.for_storage("") == (None, 1)
        assert ProviderEmailPolicy.for_storage(None) == (None, 1)


class TestPrivateEmailNotStored:
    """建号路径必须走归一：私有邮箱不入 email 列，也不占唯一键。"""

    def test_noreply_email_becomes_placeholder_account(self):
        acc = create_grape_account_oauth(
            username=f"{TAG}_p1", email="123+u@noreply.gitee.com", provider="gitee")
        assert isinstance(acc, dict), acc
        row = get_grape_account_by_id(acc["id"])
        assert row is not None
        assert row["email"] is None or row["email"] == ""
        assert int(row["email_is_placeholder"] or 0) == 1

    def test_two_accounts_can_share_one_private_email(self):
        a = create_grape_account_oauth(
            username=f"{TAG}_p2", email="same@noreply.gitee.com", provider="gitee")
        b = create_grape_account_oauth(
            username=f"{TAG}_p3", email="same@noreply.gitee.com", provider="gitee")
        assert isinstance(a, dict), a
        assert isinstance(b, dict), b   # 私有邮箱不落库 → 不会 email_conflict


class TestFailedSignupRollback:
    """回滚释放 email/username（与用户主动注销的冷静期语义分开）。"""

    def test_rollback_releases_email_and_username(self):
        email = f"{TAG}_real@example.com"
        acc = create_grape_account_oauth(
            username=f"{TAG}_rb", email=email, provider=PROVIDER)
        assert isinstance(acc, dict), acc
        uid, uname = acc["id"], acc["username"]

        assert release_failed_signup_account(uid) is True

        row = get_grape_account_by_id(uid)
        assert row is not None
        assert int(row["status"]) == 3
        assert row["email"] is None or row["email"] == ""
        assert str(row["username"]).startswith(uname[:32])
        assert "_void_" in str(row["username"])

        # 邮箱已释放 → 同一邮箱可被新账号使用（不再 30 天占坑）
        again = create_grape_account_oauth(
            username=f"{TAG}_rb2", email=email, provider=PROVIDER)
        assert isinstance(again, dict), again
        assert again["email"] == email

    def test_rollback_clears_identity_tokens(self):
        from src.domains.mgmt.auth.grape_account_db import create_external_identity

        acc = create_grape_account_oauth(
            username=f"{TAG}_rb3", email=f"{TAG}_rb3@example.com", provider=PROVIDER)
        assert isinstance(acc, dict), acc
        assert create_external_identity(
            acc["id"], PROVIDER, f"{TAG}_ext", "rollback_user", "", "", "token-x")
        assert release_failed_signup_account(acc["id"]) is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, access_token FROM external_identities "
                      "WHERE grape_user_id=?", (acc["id"],))
            status, token = c.fetchone()
        assert int(status) == 0 and not token

class TestDeactivationReleasesIdentityKeys:
    """Issue #3054 遗留：用户主动注销也必须释放 UNIQUE(provider, external_id)。

    原实现只置 status=0，external_id 原样保留 → 该第三方账号在 30 天冷静期内对所有
    人不可绑（撞唯一键）。定稿口径与解绑一致：旧值进墓碑，external_id 改 void:<行 id>。
    """

    def test_soft_delete_releases_key_and_writes_tombstone(self):
        from src.domains.mgmt.auth.auth_guards import AccountDeactivationCascade
        from src.domains.mgmt.auth.grape_account_db import create_external_identity

        acc = create_grape_account_oauth(
            username=f"{TAG}_deact", email=f"{TAG}_deact@example.com", provider=PROVIDER)
        assert isinstance(acc, dict), acc
        ext = f"{TAG}_deact_ext"
        assert create_external_identity(acc["id"], PROVIDER, ext, "deact_user", "", "", "tok-d")

        assert AccountDeactivationCascade.soft_delete(acc["id"]) is True

        with db_lock:
            row = db.cursor().execute(
                "SELECT status, external_id, COALESCE(metadata,'') "
                "FROM external_identities WHERE grape_user_id=?", (acc["id"],)).fetchone()
        assert row is not None
        assert int(row[0]) == 0
        assert str(row[1]).startswith("unbound:")   # 与解绑同口径的行级占位前缀
        assert ext in str(row[2])            # 墓碑留下"谁用过这个平台账号"

        # 唯一键已释放 → 另一个人可以立刻绑定同一个第三方账号
        other = create_grape_account_oauth(
            username=f"{TAG}_deact2", email=f"{TAG}_deact2@example.com", provider=PROVIDER)
        assert isinstance(other, dict), other
        assert create_external_identity(other["id"], PROVIDER, ext, "other_user", "", "", "tok-o")
