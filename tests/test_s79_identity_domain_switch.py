"""S79 用例（D54，先测后改）：身份域 ORM 切换——flag on/off 双路径对账。

语义保持清单（业务留 Service 层，ORM 只承载数据访问）：
- token 加密存储（encrypt/decrypt 在调用方，DTO 携带 DB 原值=密文）；
- 状态机：1=活跃 0=解绑 4=token 过期；create 的三分支（新建/活跃重绑/
  解绑恢复）；update 无条件恢复 status=1；delete 的 require_keep_one
  原子检查与清 token。
对账方式：同一组数据，GRAPE_PERSISTENCE_ORM=0/1 各跑一遍，结果逐字段
等值；DB 直查校验落库形态（密文+状态）。
"""
import pytest

from src.domains.mgmt.auth.credential_crypto import encrypt, decrypt
from src.kernel.session_manager import db, db_lock

import os


@pytest.fixture(autouse=True)
def _clean_identities():
    with db_lock:
        db.cursor().execute("DELETE FROM external_identities WHERE external_id LIKE 's79%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM external_identities WHERE external_id LIKE 's79%'")
        db.commit()
    os.environ.pop("GRAPE_PERSISTENCE_ORM", None)


def _call(fn, *a, reset=None, **kw):
    """双路径对账：flag off 与 on 各调用一次，返回 (结果off, 结果on)。

    reset：非幂等操作（如软删除）在 off 与 on 之间重建前置状态。
    """
    os.environ["GRAPE_PERSISTENCE_ORM"] = "0"
    r_off = fn(*a, **kw)
    if reset:
        reset()
    os.environ["GRAPE_PERSISTENCE_ORM"] = "1"
    try:
        r_on = fn(*a, **kw)
    finally:
        os.environ.pop("GRAPE_PERSISTENCE_ORM", None)
    return r_off, r_on


def test_s79_get_external_identity_equivalence():
    from src.domains.mgmt.auth.grape_account_db import create_external_identity, get_external_identity
    assert create_external_identity(7901, "gitee", "s79e1", "login1", "Name1",
                                    access_token="tok-plain", refresh_token="ref-plain")
    off, on = _call(get_external_identity, "gitee", "s79e1")
    for r in (off, on):
        assert r and r["grape_user_id"] == 7901 and r["external_login"] == "login1"
        assert r["access_token"] == "tok-plain" and r["refresh_token"] == "ref-plain"
    assert off == on


def test_s79_get_external_identity_any_status_equivalence():
    from src.domains.mgmt.auth.grape_account_db import (create_external_identity,
                                           delete_external_identity,
                                           get_external_identity_any_status)
    assert create_external_identity(7902, "gitee", "s79e2", "l2")
    assert delete_external_identity(7902, "gitee", require_keep_one=False)
    off, on = _call(get_external_identity_any_status, "gitee", "s79e2")
    for r in (off, on):
        assert r and r["status"] == 0
    assert off == on


def test_s79_get_user_identities_equivalence():
    from src.domains.mgmt.auth.grape_account_db import (create_external_identity,
                                           get_user_identities,
                                           get_user_identities_with_tokens)
    create_external_identity(7903, "gitee", "s79e3a", "la", access_token="ta")
    create_external_identity(7903, "github", "s79e3b", "lb", access_token="tb")
    off, on = _call(get_user_identities, 7903)
    assert off == on and {r["provider"] for r in off} == {"gitee", "github"}
    off2, on2 = _call(get_user_identities_with_tokens, 7903)
    toks = {r["provider"]: r["access_token"] for r in off2}
    assert toks == {"gitee": "ta", "github": "tb"}
    assert off2 == on2


def test_s79_create_branches_equivalence():
    """三分支：新建 / 活跃重绑 / 解绑恢复——两路径落库形态一致。"""
    from src.domains.mgmt.auth.grape_account_db import (create_external_identity,
                                           delete_external_identity,
                                           get_external_identity)
    # 新建
    off, on = _call(create_external_identity, 7904, "gitee", "s79e4", "l4",
                    "N4", access_token="t4", refresh_token="r4")
    assert off is True and on is True
    # 活跃重绑（同用户同平台换 external_id）
    off2, on2 = _call(create_external_identity, 7904, "gitee", "s79e4b", "l4b",
                      access_token="t4b")
    assert off2 is True and on2 is True
    # 解绑后恢复（status=0 → create 恢复为 1）
    assert delete_external_identity(7904, "gitee", require_keep_one=False)
    off3, on3 = _call(create_external_identity, 7905, "gitee", "s79e4", "l4r",
                      access_token="t4r")
    assert off3 is True and on3 is True
    r = get_external_identity("gitee", "s79e4")
    assert r and r["grape_user_id"] == 7905 and r["access_token"] == "t4r"


def test_s79_delete_keep_one_semantics():
    from src.domains.mgmt.auth.grape_account_db import (create_external_identity,
                                           delete_external_identity,
                                           get_user_identities)
    # require_keep_one 前置：用户有密码（否则仅一条绑定会被正确拒绝）
    with db_lock:
        db.cursor().execute(
            "INSERT OR IGNORE INTO grape_accounts (id, username, password_hash) VALUES (7906, 's79u6', 'ph')")
        db.commit()
    create_external_identity(7906, "gitee", "s79e6a", "la")
    # 仅一条活跃绑定 + 有密码 → 可删
    def _rebind():
        create_external_identity(7906, "gitee", "s79e6a", "la")
    r_off, r_on = _call(delete_external_identity, 7906, "gitee", reset=_rebind)
    assert r_off is True and r_on is True
    idents = get_user_identities(7906)
    assert all(r["provider"] != "gitee" for r in idents)


def test_s79_update_tokens_restores_expired():
    from src.domains.mgmt.auth.grape_account_db import (create_external_identity,
                                           delete_external_identity,
                                           update_external_identity_tokens,
                                           get_user_identities_with_tokens)
    from src.kernel.session_manager import db as _db
    create_external_identity(7907, "gitee", "s79e7", "l7", access_token="old")
    # 手工置 status=4（token 过期）
    with db_lock:
        _db.cursor().execute("UPDATE external_identities SET status=4 WHERE external_id='s79e7'")
        _db.commit()
    r_off, r_on = _call(update_external_identity_tokens, 7907, "gitee",
                        access_token="new-tok")
    assert r_off is True and r_on is True
    toks = {r["provider"]: r["access_token"] for r in get_user_identities_with_tokens(7907)}
    assert toks.get("gitee") == "new-tok"


def test_s79_ciphertext_at_rest():
    """落库形态：token 列是密文（两路径一致——加密语义在 Service 层）。"""
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    _call(create_external_identity, 7908, "gitee", "s79e8", "l8", access_token="plain-tok")
    with db_lock:
        row = db.cursor().execute(
            "SELECT access_token FROM external_identities WHERE external_id='s79e8'").fetchone()
    assert row and row[0] != "plain-tok"
    assert decrypt(row[0]) == "plain-tok"
