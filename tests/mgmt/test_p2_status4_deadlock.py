"""Issue #3054 P2：登录决策与 status=4 死锁（四不通 + 票据 + 换绑墓碑）。

覆盖验收：
1. status=4 → 重登恢复 status=1，且不产生 status=3 孤儿账号；
2. status=4 可解绑并释放 UNIQUE(provider, external_id)，他人可重新绑定；
3. 他人绑定 status=4 → 走票据（need_transfer），不是 bind_failed；
4. 换绑后旧平台账号登录回到原账号（不新建）；
5. status=0（正常解绑）行为不变。

约束背景（实锤）：external_identities 同时有 UNIQUE(provider, external_id) 与
UNIQUE(grape_user_id, provider)，一人一平台只有一行 → 墓碑只能随行存 metadata，
不能另起一行。状态语义：1=活跃 0=解绑 4=token 过期。
"""
import re

import pytest

from src.kernel.session_manager import db, db_lock

PROVIDER = "p2test"
_TAG = "p2t3054"


class _FakeRequest:
    """最小 Request 替身（登录/绑定 handler 只用 headers/cookies/client）。"""

    def __init__(self, cookies=None, headers=None, client_host="127.0.0.1"):
        self.cookies = cookies or {}
        self.headers = headers or {"host": "127.0.0.1:8000"}
        self.client = type("C", (), {"host": client_host})()
        self.query_params = {}


def _cfg_get(*a, **kw):  # pragma: no cover - 防御性：某些分支会读配置
    return None


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


def _wipe():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE provider=?", (PROVIDER,))
        c.execute("DELETE FROM binding_tickets WHERE external_id LIKE ?", (_TAG + "%",))
        c.execute("DELETE FROM grape_accounts WHERE username LIKE ?", (_TAG + "%",))
        c.execute("DELETE FROM grape_accounts WHERE email LIKE ?", (_TAG + "%",))
        db.commit()


def _mk_account(suffix: str):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    acc = create_grape_account(f"{_TAG}_{suffix}", f"{_TAG}_{suffix}@example.com",
                               "Str0ng!Passw0rd")
    assert isinstance(acc, dict), acc
    return acc


def _login(suffix: str, **kw):
    from src.domains.mgmt.routes.grape_auth import _handle_oauth_login
    return _handle_oauth_login(_FakeRequest(**kw), PROVIDER, suffix,
                               {"external_login": "lg" + suffix, "external_name": "N"},
                               "access-new", "refresh-new")


def _count_accounts(status=None):
    """按 email 前缀计数（登录链会把 username 同步成平台登录名，不能用 username 断言）。"""
    with db_lock:
        c = db.cursor()
        if status is None:
            c.execute("SELECT COUNT(*) FROM grape_accounts WHERE email LIKE ?",
                      (_TAG + "%",))
        else:
            c.execute("SELECT COUNT(*) FROM grape_accounts WHERE email LIKE ? AND status=?",
                      (_TAG + "%", status))
        return c.fetchone()[0]


def _row_status(provider, external_id):
    from src.domains.mgmt.auth.grape_account_db import get_external_identity_any_status
    d = get_external_identity_any_status(provider, external_id)
    return None if d is None else d["status"]


# ---------------------------------------------------------------- 1. status=4 恢复
def test_status4_relogin_recovers_and_creates_no_orphan():
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, get_external_identity, mark_identity_expired)

    acc = _mk_account("r1")
    ext = _TAG + "-r1"
    assert create_external_identity(acc["id"], PROVIDER, ext, "lg", "N", access_token="old")
    assert mark_identity_expired(acc["id"], PROVIDER)
    assert _row_status(PROVIDER, ext) == 4
    before = _count_accounts()

    resp = _login(ext)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "oauth_error" not in loc and "error=" not in loc

    ident = get_external_identity(PROVIDER, ext)          # 默认 statuses=(1,) → 必须已恢复
    assert ident is not None and str(ident["grape_user_id"]) == str(acc["id"])
    assert ident["status"] == 1
    assert ident["access_token"] == "access-new"
    assert _row_status(PROVIDER, ext) == 1
    # 没有新建账号、没有 status=3 孤儿
    assert _count_accounts() == before
    assert _count_accounts(status=3) == 0


# ---------------------------------------------------------------- 2. status=4 解绑
def test_status4_unbind_releases_unique_key_and_others_can_bind():
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, delete_external_identity, find_identity_by_tombstone,
        get_external_identity, get_external_identity_any_status, mark_identity_expired)

    acc_a = _mk_account("u2a")
    acc_b = _mk_account("u2b")
    ext = _TAG + "-u2"
    assert create_external_identity(acc_a["id"], PROVIDER, ext, "lga", "A", access_token="tok-a")
    assert mark_identity_expired(acc_a["id"], PROVIDER)

    # status=4 行可解绑（此前 404 死锁）
    assert delete_external_identity(acc_a["id"], PROVIDER) is True
    row = get_external_identity_any_status(PROVIDER, ext)
    assert row is None or str(row["grape_user_id"]) != str(acc_a["id"]) or row["status"] != 0
    with db_lock:
        c = db.cursor()
        c.execute("SELECT external_id, status, metadata FROM external_identities "
                  "WHERE grape_user_id=? AND provider=?", (str(acc_a["id"]), PROVIDER))
        _ext, _st, _meta = c.fetchone()
    assert _st == 0 and _ext.startswith("unbound:")
    assert ext in _meta  # 墓碑留痕

    # UNIQUE(provider, external_id) 已释放 → 他人可直接绑定
    assert create_external_identity(acc_b["id"], PROVIDER, ext, "lgb", "B", access_token="tok-b")
    ident = get_external_identity(PROVIDER, ext)
    assert ident and str(ident["grape_user_id"]) == str(acc_b["id"])
    # 墓碑仍指向原账号（用于旧账号回登）
    assert str(find_identity_by_tombstone(PROVIDER, ext)["grape_user_id"]) == str(acc_a["id"])


# ---------------------------------------------------------------- 3. 他人绑定 status=4
def test_other_user_binding_status4_gets_ticket_not_bind_failed():
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, mark_identity_expired)
    from src.domains.mgmt.routes.grape_auth import _handle_oauth_bind

    acc_a = _mk_account("b3a")
    acc_c = _mk_account("b3c")
    ext = _TAG + "-b3"
    assert create_external_identity(acc_a["id"], PROVIDER, ext, "lga", "A")
    assert mark_identity_expired(acc_a["id"], PROVIDER)

    resp = _handle_oauth_bind(
        _FakeRequest(), PROVIDER, ext,
        {"external_login": "lgc", "external_name": "C"}, "tok-c", "ref-c",
        binding_user_id=acc_c["id"])
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "need_transfer" in loc, loc
    assert "bind_failed" not in loc
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM binding_tickets WHERE external_id=? "
                  "AND initiating_user_id=? AND provider=?", (ext, str(acc_c["id"]), PROVIDER))
        assert c.fetchone()[0] == 1


# ---------------------------------------------------------------- 4. 换绑墓碑
def test_rebind_keeps_tombstone_and_old_account_returns_to_owner():
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, find_identity_by_tombstone, get_external_identity)

    acc = _mk_account("s4")
    old_ext, new_ext = _TAG + "-s4a", _TAG + "-s4b"
    assert create_external_identity(acc["id"], PROVIDER, old_ext, "lg-old", "N",
                                    access_token="t-old")
    # 换绑（同用户同平台换平台账号）
    assert create_external_identity(acc["id"], PROVIDER, new_ext, "lg-new", "N",
                                    access_token="t-new")
    ident = get_external_identity(PROVIDER, new_ext)
    assert ident and str(ident["grape_user_id"]) == str(acc["id"])

    tomb = find_identity_by_tombstone(PROVIDER, old_ext)
    assert tomb and str(tomb["grape_user_id"]) == str(acc["id"])

    before = _count_accounts()
    resp = _login(old_ext)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "oauth_error" not in loc
    ident = get_external_identity(PROVIDER, old_ext)
    assert ident and str(ident["grape_user_id"]) == str(acc["id"])
    assert _count_accounts() == before          # 没有新建第二个 Grape 号
    # 反向墓碑：刚切过去的 new_ext 也在账本里
    assert find_identity_by_tombstone(PROVIDER, new_ext)


# ---------------------------------------------------------------- 6. 服务层 DI
class _FakeRepo:
    """IdentityAdminRepository 的内存替身：证明服务可脱离数据库单测（依赖注入）。"""

    def __init__(self):
        self.rows = {}
        self._seq = 0

    def _dto(self, row):
        return type("D", (), dict(row, id=row["id"]))

    def get_by_platform(self, session, provider, external_id, statuses=(1,)):
        for r in self.rows.values():
            if (r["provider"] == provider and r["external_id"] == str(external_id)
                    and r["status"] in statuses):
                return self._dto(r)
        return None

    def get_by_platform_any_status(self, session, provider, external_id):
        return self.get_by_platform(session, provider, external_id, statuses=(0, 1, 4))

    def get_by_user_provider(self, session, uid, provider, statuses=(0, 1, 4)):
        for rid in sorted(self.rows):
            r = self.rows[rid]
            if str(r["grape_user_id"]) == str(uid) and r["provider"] == provider \
                    and r["status"] in statuses:
                return self._dto(r)
        return None

    def list_by_user_statuses(self, session, uid, statuses=(1, 4)):
        return [self._dto(r) for r in self.rows.values()
                if str(r["grape_user_id"]) == str(uid) and r["status"] in statuses]

    def list_by_provider_statuses(self, session, provider, statuses=(0, 1, 4)):
        return [self._dto(r) for r in self.rows.values()
                if r["provider"] == provider and r["status"] in statuses]

    def update_by_id(self, session, rid, external_id, external_login, external_name,
                     external_avatar, access_token, refresh_token, token_expires_at,
                     updated_at, status):
        r = self.rows[str(rid)]
        r.update(external_id=str(external_id), external_login=external_login,
                 external_name=external_name, external_avatar=external_avatar,
                 access_token=access_token, refresh_token=refresh_token,
                 token_expires_at=token_expires_at, updated_at=updated_at, status=status)

    def update_metadata_by_id(self, session, rid, metadata_json):
        self.rows[str(rid)]["metadata_json"] = metadata_json

    def delete_by_id(self, session, rid):
        self.rows.pop(str(rid), None)

    def create(self, session, uid, provider, external_id, external_login,
               external_name, external_avatar, access_token, refresh_token,
               token_expires_at, status, created_at):
        self._seq += 1
        rid = f"fake-{self._seq}"
        self.rows[rid] = {"id": rid, "grape_user_id": str(uid), "provider": provider,
                          "external_id": str(external_id),
                          "external_login": external_login,
                          "external_name": external_name,
                          "external_avatar": external_avatar,
                          "access_token": access_token,
                          "refresh_token": refresh_token,
                          "token_expires_at": token_expires_at,
                          "metadata_json": "{}", "status": status,
                          "created_at": created_at}
        return rid


class _FakeCipher:
    @staticmethod
    def encrypt(v):
        return "enc:" + (v or "")

    @staticmethod
    def decrypt(v):
        return (v or "")[4:] if (v or "").startswith("enc:") else (v or "")


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def test_service_is_dependency_injectable_and_owns_the_state_machine():
    """服务类可注入仓储/事务/加密：bind → 换绑 → 墓碑 → 解绑释放，全部内存可验。"""
    from src.domains.mgmt.auth.identity_lifecycle import IdentityLifecycleService

    repo = _FakeRepo()
    svc = IdentityLifecycleService(repository=repo, unit_of_work=_NullCtx,
                                   cipher=_FakeCipher, db_access=lambda: (None, None))
    assert svc.bind("u1", "p2test", "old", "lg1", access_token="t1") is True
    assert svc.bind("u1", "p2test", "new", "lg2", access_token="t2") is True
    assert len(repo.rows) == 1                                  # 一人一平台一行
    row = list(repo.rows.values())[0]
    assert row["external_id"] == "new" and row["status"] == 1
    assert [t["external_id"] for t in svc.tombstones(row["metadata_json"])] == ["old"]
    _tomb_old = svc.find_tombstone("p2test", "old")
    assert _tomb_old and _tomb_old["grape_user_id"] == "u1"

    # status=4 解绑 → 释放唯一键（external_id 变占位值），墓碑保留
    row["status"] = 4
    assert svc.unbind("u1", "p2test", require_keep_one=False) is True
    assert row["status"] == 0 and row["external_id"].startswith("unbound:")
    _tomb_new = svc.find_tombstone("p2test", "new")
    assert _tomb_new and _tomb_new["grape_user_id"] == "u1"

    # 他人可绑定同名 external_id（不再撞 UNIQUE(provider, external_id)）
    assert svc.bind("u2", "p2test", "new", "lg2") is True
    _after = svc.get("p2test", "new")
    assert _after and _after["grape_user_id"] == "u2"


# ---------------------------------------------------------------- 5. status=0 不变
def test_status0_unbind_behaviour_unchanged():
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, delete_external_identity, get_external_identity_any_status,
        get_unbound_identity)

    acc_a = _mk_account("v5a")
    acc_b = _mk_account("v5b")
    ext = _TAG + "-v5"
    assert create_external_identity(acc_a["id"], PROVIDER, ext, "lga", "A")
    assert delete_external_identity(acc_a["id"], PROVIDER) is True

    # status=1 解绑：status=0、external_id 保留（历史口径不变）
    row = get_external_identity_any_status(PROVIDER, ext)
    assert row and row["status"] == 0 and str(row["grape_user_id"]) == str(acc_a["id"])
    unbound = get_unbound_identity(PROVIDER, ext)
    assert unbound and str(unbound["grape_user_id"]) == str(acc_a["id"])

    # 他人直接 create 仍被拒（合法路径是票据转移），与改动前一致
    assert create_external_identity(acc_b["id"], PROVIDER, ext, "lgb", "B") is False

def test_set_order_keeps_status_last_for_mysql_semantics():
    """QA 回归（P0）：MySQL 单表 UPDATE 的 SET 从左到右求值。

    原语句把 `status=0` 放在最前，两个 `CASE WHEN status=4` 读到的已是 0 →
    release_key / 墓碑两处都走 ELSE：唯一键不释放、墓碑不写，status=4 解绑在
    MySQL（集群 profile）上整体失效，而 SQLite 用旧行求值，6 条用例全绿看不见。
    这里把「status=0 必须在两个 CASE 之后」锁死。
    """
    from src.domains.mgmt.auth.identity_unbind import (
        MySqlUnbindStatement,
        SqliteUnbindStatement,
        UnbindTarget,
    )

    target = UnbindTarget(identity_id="1", status=4, external_id="ext-1", metadata="{}")
    for cls in (SqliteUnbindStatement, MySqlUnbindStatement):
        stmt = cls(target, "u1", now="2026-01-01 00:00:00",
                   new_external_id="void:1", new_metadata='{"tombstones": []}')
        sql, _params = stmt.build()
        set_part = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        assert "status=0" in set_part
        assert set_part.index("status=0") > set_part.index("external_id=CASE WHEN status=4")
        assert set_part.index("status=0") > set_part.index("metadata=CASE WHEN status=4")
        # WHERE 仍用旧行判定（更新前求值），status=0 挪位不影响命中范围
        assert "(status=1 OR status=4)" in sql


def test_keep_one_gate_counts_status4_as_login_method():
    """QA 回归（P0）：解绑前置闸必须把 status=4 计为可用登录方式。

    实锤场景：无密码用户持有 gitcode(status=1) + gitee(status=4)，
    旧 count_user_identities 只数 status=1 → 前置闸 400，服务层原子解绑判据
    根本走不到（验收头号场景在 UI 上就失败）。
    """
    from src.domains.mgmt.auth import grape_account_db as gad

    provider2 = PROVIDER + "2"
    acc = gad.create_grape_account_oauth(
        username=f"{_TAG}_gate", email="", nickname="", avatar_url="",
        email_verified=False, provider=PROVIDER)
    assert isinstance(acc, dict), acc
    uid = str(acc["id"])
    try:
        assert gad.create_external_identity(
            uid, PROVIDER, f"{_TAG}_gateA", "gate_a", "", "", "")
        assert gad.create_external_identity(
            uid, provider2, f"{_TAG}_gateB", "gate_b", "", "", "")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE external_identities SET status=4 "
                      "WHERE grape_user_id=? AND provider=?", (uid, provider2))
            db.commit()
        assert gad.has_password(uid) is False
        assert gad.count_user_identities(uid) == 1                  # 默认口径不变
        # 前置闸口径：status=4 仍是可用登录方式，放行解绑
        assert gad.count_user_identities(uid, statuses=(1, 4)) == 2
        assert not gad.count_user_identities(uid, statuses=(1, 4)) <= 1
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
            db.commit()
