"""A2：凭据键模型归位（provider 纯名 + kind 列）的回归用例。

背景（2026-11-13 用户拍板）：原 provider 把类型编码进名字（`<p>_pat`、
`<p>_oauth`），审计/守卫只能靠后缀猜类型，且"同平台多类型"要靠造键名表达。
归位形态：provider 存纯平台名/凭据名，类型独立成 kind 列；迁移期双读双写，
滚动升级窗口里老实例仍按旧复合键可读。
"""
import json
import uuid

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as user_db


def _mk_user(prefix: str) -> str:
    from src.domains.mgmt.auth import grape_account_db
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    u = grape_account_db.create_grape_account(
        username=uname, email=f"{uname}@test.local", password="pw123")
    assert u, f"用户构造失败: {uname}"
    return u["id"]


def _drop(uid: str) -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.commit()


def _raw_rows(uid: str) -> list[tuple]:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider, COALESCE(kind,'') FROM user_credentials WHERE user_id=?", (uid,))
        return c.fetchall()


def test_kind_written_and_legacy_key_dual_written():
    """写归位形态 + 旧复合键（双写期），两条路都能读到。"""
    uid = _mk_user("ckind_w")
    try:
        assert user_db.set_credential(uid, "gitee", {"token": "tok-a", "login": "bob"}, kind="pat")
        rows = _raw_rows(uid)
        assert ("gitee", "pat") in rows, f"归位行缺失: {rows}"
        assert ("gitee_pat", "") in rows, f"双写期旧键行缺失（老实例读不到）: {rows}"

        assert (user_db.get_credential(uid, "gitee", kind="pat") or {}).get("token") == "tok-a"
        assert (user_db.get_credential(uid, "gitee_pat") or {}).get("token") == "tok-a"
    finally:
        _drop(uid)


def test_read_falls_back_to_legacy_composite_key():
    """迁移前形态（只有 <p>_pat 行）照样能按 (provider, kind=pat) 读到。"""
    uid = _mk_user("ckind_fb")
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO user_credentials (id,user_id,provider,kind,config_json) "
                      "VALUES (?,?,?,?,?)",
                      (uuid.uuid4().hex, uid, "gitlab_pat", "",
                       encrypt(json.dumps({"token": "legacy-tok"}))))
            db.commit()
        cfg = user_db.get_credential(uid, "gitlab", kind="pat") or {}
        assert cfg.get("token") == "legacy-tok", "旧复合键回退读失败（滚动升级期会丢凭据）"
    finally:
        _drop(uid)


def test_get_credentials_reports_kind_and_hides_legacy_rows():
    """get_credentials 暴露 kind，且双写期旧键行不重复出现在列表里。"""
    uid = _mk_user("ckind_list")
    try:
        user_db.set_credential(uid, "gitee", {"token": "tok-b"}, kind="pat")
        user_db.set_credential(uid, "huawei_cloud", {"ak": "x", "sk": "y"})
        creds = {c["provider"]: c for c in user_db.get_credentials(uid)}
        assert "gitee" in creds and creds["gitee"]["kind"] == "pat"
        assert "gitee_pat" not in creds, "旧键行泄漏到凭据列表"
        assert creds["huawei_cloud"]["kind"] == "", "裸名凭据不应被归类"
    finally:
        _drop(uid)


def test_delete_removes_both_forms():
    """删除同时清归位行与旧键行，避免残留可读副本。"""
    uid = _mk_user("ckind_del")
    try:
        user_db.set_credential(uid, "gitee", {"token": "tok-c"}, kind="pat")
        assert user_db.delete_credential(uid, "gitee", kind="pat") is True
        assert _raw_rows(uid) == [], f"删除后仍有残留行: {_raw_rows(uid)}"
        assert user_db.get_credential(uid, "gitee", kind="pat") is None
    finally:
        _drop(uid)


def test_backfill_is_idempotent_and_keeps_legacy_rows():
    """回填：拆出归位行、保留旧键行；重复跑零新增（幂等）。"""
    uid = _mk_user("ckind_bf")
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO user_credentials (id,user_id,provider,kind,config_json) "
                      "VALUES (?,?,?,?,?)",
                      (uuid.uuid4().hex, uid, "gitee_pat", "",
                       encrypt(json.dumps({"token": "tok-d", "login": "e2e"}))))
            db.commit()

        s1 = user_db.backfill_credential_kind()
        assert s1["pat"] >= 1
        rows = _raw_rows(uid)
        assert ("gitee", "pat") in rows, f"回填未生成归位行: {rows}"
        assert ("gitee_pat", "") in rows, "回填不应删除旧键行（双写期）"

        s2 = user_db.backfill_credential_kind()
        assert s2["pat"] == 0, f"回填不幂等（重复新增 {s2}）"
        assert len([r for r in _raw_rows(uid) if r[0] == "gitee"]) == 1
    finally:
        _drop(uid)


def test_migration_registered():
    """迁移登记在场（版本号 + 函数名），保证存量库启动期自动补列/回填。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src/api/db_setup.py"
    text = src.read_text(encoding="utf-8")
    assert 'version="202611170006"' in text
    assert 'name="user-credentials-kind-model"' in text
    assert "backfill_credential_kind" in text


def test_read_picks_newer_row_after_rollback_write():
    """回退窗口分叉（2026-11-13 收口前补丁）：旧实例只更新 `<p>_pat` 行时，
    读侧必须按 updated_at 取较新者，而不是固定优先归位行——否则用户会看到
    "凭据莫名回退到旧值"。同 updated_at 仍归位形态优先（确定性）。"""
    uid = _mk_user("ckind_new")
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt

        assert user_db.set_credential(uid, "gitee", {"token": "new-tok"}, kind="pat")

        # 模拟回退期的旧实例：只写旧复合键，时间戳更晚
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_credentials SET config_json=?, updated_at=? "
                      "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=''",
                      (encrypt(json.dumps({"token": "rollback-tok"})),
                       "2099-01-01 00:00:00", uid, "gitee_pat"))
            db.commit()

        got = user_db.get_credential(uid, "gitee", kind="pat") or {}
        assert got.get("token") == "rollback-tok", (
            f"回退期读到 stale 归位行（应收较新的旧键行）: {got}")

        # 反向：归位行更新更晚 → 取归位行
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_credentials SET updated_at=? "
                      "WHERE user_id=? AND provider=? AND COALESCE(kind,'')='pat'",
                      ("2099-06-01 00:00:00", uid, "gitee"))
            db.commit()
        got2 = user_db.get_credential(uid, "gitee", kind="pat") or {}
        assert got2.get("token") == "new-tok", f"未按 updated_at 取较新行: {got2}"
    finally:
        _drop(uid)


def test_same_provider_pat_and_oauth_coexist():
    """A2b（2026-11-13）：主键含 kind —— 同平台 PAT 与 OAuth 各占一行，互不覆盖。

    扩键前实弹：set_credential(PAT, kind=pat) 后 set_credential(OAuth, kind=oauth)
    会把归位行 upsert 成 kind='oauth'，PAT 只剩双写旧键副本；A2 收口删旧键行后即丢。
    """
    uid = _mk_user("ckind_uq")
    try:
        assert user_db.set_credential(uid, "gitee", {"token": "PAT-KEEP"}, kind="pat")
        assert user_db.set_credential(uid, "gitee", {"access_token": "OAUTH-KEEP"}, kind="oauth")

        rows = _raw_rows(uid)
        assert ("gitee", "pat") in rows, f"PAT 归位行被 OAuth 覆盖了: {rows}"
        assert ("gitee", "oauth") in rows, f"OAuth 归位行缺失: {rows}"

        assert (user_db.get_credential(uid, "gitee", kind="pat") or {}).get("token") == "PAT-KEEP"
        assert (user_db.get_credential(uid, "gitee", kind="oauth") or {}).get("access_token") == "OAUTH-KEEP"

        # 再更新 PAT，不许动 OAuth 行
        assert user_db.set_credential(uid, "gitee", {"token": "PAT-V2"}, kind="pat")
        assert (user_db.get_credential(uid, "gitee", kind="oauth") or {}).get("access_token") == "OAUTH-KEEP", \
            "更新 PAT 时把 OAuth 行改了（主键未含 kind）"
    finally:
        _drop(uid)


def test_unique_key_includes_kind():
    """结构守卫：建表 DDL 主键与注册迁移都必须含 kind。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    ddl = (root / "src/domains/mgmt/user/db.py").read_text(encoding="utf-8")
    assert "PRIMARY KEY (user_id, provider, kind)" in ddl, "建表主键未扩 kind"
    assert "ON CONFLICT(user_id, provider, kind)" in ddl, "upsert 冲突目标未扩 kind"
    setup = (root / "src/api/db_setup.py").read_text(encoding="utf-8")
    assert 'version="202611170007"' in setup, "扩主键迁移未注册"
    assert "user_credentials__uqkind" in setup, "扩主键迁移体缺失"
    models = (root / "src/kernel/persistence/models.py").read_text(encoding="utf-8")
    assert 'primary_key=True, default=""' in models, "ORM 的 kind 未进主键"


def test_unique_kind_migration_is_idempotent():
    """迁移幂等：库上唯一键已含 kind 时再跑零写入。"""
    from src.api import db_setup
    import src.domains.mgmt.user.db as user_db_mod  # noqa: F401

    with db_lock:
        c = db.cursor()
        before = c.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0]
    colsets = []
    real = db_setup._uc_unique_key_columns
    try:
        db_setup._uc_unique_key_columns = lambda cur: {"user_id", "provider", "kind"}
        db_setup._fn_user_credentials_unique_kind()
    finally:
        db_setup._uc_unique_key_columns = real
    with db_lock:
        c = db.cursor()
        after = c.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0]
    assert before == after, "幂等分支改动了数据"