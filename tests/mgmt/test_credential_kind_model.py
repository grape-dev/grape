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
    """结构守卫：建表 DDL 主键、upsert 冲突目标、注册迁移、ORM 主键都必须含 kind。"""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    ddl = (root / "src/domains/mgmt/user/db.py").read_text(encoding="utf-8")
    assert "PRIMARY KEY (user_id, provider, kind)" in ddl, "建表主键未扩 kind"
    assert "ON CONFLICT(user_id, provider, kind)" in ddl, "upsert 冲突目标未扩 kind"
    setup = (root / "src/api/db_setup.py").read_text(encoding="utf-8")
    assert 'version="202611170007"' in setup, "扩主键迁移未注册"
    assert 'kind="cleanup"' in setup, "重建表迁移必须按 cleanup 语义登记（QA C1）"
    assert "user_credentials__uqkind" in setup, "扩主键迁移体缺失"
    # ORM 主键：用 AST 读 Column(primary_key=True) 归属，不用字符串匹配（QA I2）
    tree = ast.parse((root / "src/kernel/persistence/models.py").read_text(encoding="utf-8"))
    pk_cols = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UserCredentialModel":
            for stmt in node.body:
                if not isinstance(stmt, (ast.AnnAssign, ast.Assign)):
                    continue
                if isinstance(stmt, ast.AnnAssign):
                    tgt = stmt.target
                    name = tgt.id if isinstance(tgt, ast.Name) else None
                else:
                    t0 = stmt.targets[0] if stmt.targets else None
                    name = t0.id if isinstance(t0, ast.Name) else None
                call = stmt.value
                if not isinstance(call, ast.Call):
                    continue
                for kw in call.keywords:
                    if kw.arg == "primary_key" and getattr(kw.value, "value", False) is True:
                        pk_cols.add(name)
    assert pk_cols == {"user_id", "provider", "kind"}, f"ORM 主键应为 user_id/provider/kind，实际 {pk_cols}"


def test_unique_kind_migration_rebuilds_legacy_table(tmp_path, monkeypatch):
    """QA M3：搬迁分支（旧表 → 新表）必须有回归用例，不能只测早退。

    造一张旧 schema 表（主键 user_id+provider）、塞 PAT/OAuth/裸名三行，
    调迁移 fn，断言：行数守恒、主键含 kind、原数据逐行不变、二次调用零写入。
    """
    import contextlib
    import sqlite3

    from src.api import db_setup as ds
    from src.domains.mgmt.user import db as user_db_mod

    p = tmp_path / "legacy_uc.db"
    con = sqlite3.connect(p)
    con.executescript("""
    CREATE TABLE user_credentials (
        user_id TEXT NOT NULL, provider TEXT NOT NULL, config_json TEXT NOT NULL,
        updated_at TEXT DEFAULT '', id TEXT DEFAULT '', created_at TEXT DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, provider));
    INSERT INTO user_credentials (user_id, provider, config_json, kind) VALUES
        ('u1','gitee','{"token":"PAT"}','pat'),
        ('u1','gitee_oauth','{"access_token":"OA"}',''),
        ('u1','huawei_cloud','{"ak":"x"}','');
    """)
    con.commit()

    monkeypatch.setattr(ds, "db", con)
    monkeypatch.setattr(ds, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(user_db_mod, "backfill_credential_kind", lambda: {"pat": 0, "oauth": 0, "skipped": 0})

    ds._fn_user_credentials_unique_kind()

    rows = con.execute(
        "SELECT user_id, provider, COALESCE(kind,''), config_json FROM user_credentials "
        "ORDER BY provider").fetchall()
    assert len(rows) == 3, f"行数不守恒: {rows}"
    assert rows[0][3] == '{"token":"PAT"}' and rows[0][2] == "pat", f"PAT 行被改: {rows[0]}"
    assert rows[1][3] == '{"access_token":"OA"}', f"OAuth 行被改: {rows[1]}"
    assert rows[2][3] == '{"ak":"x"}' and rows[2][2] == "", f"裸名行被改: {rows[2]}"

    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='user_credentials'").fetchone()[0]
    assert "PRIMARY KEY (user_id, provider, kind)" in sql, f"主键未扩: {sql}"
    assert con.execute(
        "SELECT name FROM sqlite_master WHERE name='user_credentials__old_backup'").fetchone() is None, \
        "备份表未清理"

    before = con.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0]
    ds._fn_user_credentials_unique_kind()
    assert con.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0] == before, \
        "二次调用改动了数据（幂等失效）"
    con.close()


# ── #1372 回修：迁移安全网的负向 / 残留用例 ──────────────────────────────────

def _legacy_uc(p, *, key3: bool = False):
    """造一张 user_credentials：key3=False 为旧两列主键（未扩键形态）。"""
    import sqlite3
    con = sqlite3.connect(p)
    pk = ("PRIMARY KEY (user_id, provider, kind)" if key3
          else "PRIMARY KEY (user_id, provider)")
    con.executescript(f"""
    CREATE TABLE user_credentials (
        id TEXT DEFAULT '', user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        {pk});
    CREATE TABLE _bak_tpl (user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL);
    """)
    con.commit()
    return con


def _wire(monkeypatch, con, *, backfill=None):
    import contextlib
    from src.api import db_setup as ds
    from src.domains.mgmt.user import db as user_db_mod
    monkeypatch.setattr(ds, "db", con)
    monkeypatch.setattr(ds, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(user_db_mod, "backfill_credential_kind",
                        backfill or (lambda: {"pat": 0, "oauth": 0, "skipped": 0}))
    return ds


def _count(con, tbl):
    return con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]


def test_unique_kind_rejects_half_swap_leftover(tmp_path, monkeypatch):
    """C-1：存活表缺失 + 备份表有数据 → 必须 raise（不能当新装库盖章）。"""
    con = _legacy_uc(tmp_path / "half.db")
    con.executescript("""
    CREATE TABLE user_credentials__old_backup (
        user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL);
    INSERT INTO user_credentials__old_backup (user_id, provider, kind, config_json)
        VALUES ('u1','gitee','pat','{}'),('u1','gitee_oauth','','{}');
    DROP TABLE user_credentials;
    """)
    con.commit()
    ds = _wire(monkeypatch, con)

    with pytest.raises(RuntimeError, match="半交换残留"):
        ds._fn_user_credentials_unique_kind()
    assert _count(con, "user_credentials__old_backup") == 2, "残留验收时销毁了备份数据"
    con.close()


def test_unique_kind_rejects_failed_second_rename(tmp_path, monkeypatch):
    """C-1：SQLite 第二步 RENAME 失败 → raise，且下次启动按残留态再拦。"""
    con = _legacy_uc(tmp_path / "rename.db")
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, config_json) VALUES
        ('u1','gitee','{"token":"PAT"}'), ('u1','huawei_cloud','{"ak":"x"}');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)
    real = ds._destructive_ddl

    def flaky(c, ddl, **kw):
        if ddl.startswith("ALTER TABLE user_credentials__uqkind RENAME"):
            return False          # 模拟第二步失败（_destructive_ddl 真实行为是吞错返回 True）
        return real(c, ddl, **kw)

    monkeypatch.setattr(ds, "_destructive_ddl", flaky)
    with pytest.raises(RuntimeError):
        ds._fn_user_credentials_unique_kind()

    # 半交换态：存活表没了，旧数据在备份表、新数据在中转表
    assert not con.execute(
        "SELECT name FROM sqlite_master WHERE name='user_credentials' AND type='table'").fetchone()
    assert _count(con, "user_credentials__old_backup") == 2
    assert _count(con, "user_credentials__uqkind") == 2

    # 再跑一次：残留验收必须拦住（而不是走"新装库"早退盖章）
    monkeypatch.setattr(ds, "_destructive_ddl", real)
    with pytest.raises(RuntimeError, match="半交换残留"):
        ds._fn_user_credentials_unique_kind()
    con.close()


def test_unique_kind_clears_empty_leftover_tables(tmp_path, monkeypatch):
    """C-1 反例：残留表为空时不算事故，清掉空壳后正常返回。"""
    con = _legacy_uc(tmp_path / "empty.db")
    con.executescript("""
    CREATE TABLE user_credentials__uqkind (user_id TEXT NOT NULL);
    DROP TABLE user_credentials;
    """)
    con.commit()
    ds = _wire(monkeypatch, con)
    ds._fn_user_credentials_unique_kind()      # 不应抛错
    assert not con.execute(
        "SELECT name FROM sqlite_master WHERE name='user_credentials__uqkind'").fetchone()
    con.close()


def test_unique_kind_backfills_on_early_return(tmp_path, monkeypatch):
    """M-1：键已扩但未盖章的早退路径必须执行回填。"""
    con = _legacy_uc(tmp_path / "early.db", key3=True)
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, kind, config_json) VALUES
        ('u1','gitee_pat','','{"token":"PAT"}'),
        ('u1','gitee_oauth','','{"access_token":"OA"}');
    """)
    con.commit()
    calls = []
    ds = _wire(monkeypatch, con, backfill=lambda: calls.append(1) or
               {"pat": 0, "oauth": 0, "skipped": 0})

    ds._fn_user_credentials_unique_kind()
    assert calls, "早退路径未执行回填（A2 收口会丢 PAT）"
    con.close()


def test_unique_kind_keeps_backup_when_window_wrote_to_old_table(tmp_path, monkeypatch):
    """M-3：备份表存在存活表没有的行 → raise 并保留备份（可人工回收）。"""
    con = _legacy_uc(tmp_path / "window.db", key3=True)
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, kind, config_json) VALUES
        ('u1','gitee','pat','{"token":"PAT"}');
    CREATE TABLE user_credentials__old_backup (
        user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL, updated_at TEXT);
    INSERT INTO user_credentials__old_backup (user_id, provider, kind, config_json, updated_at) VALUES
        ('u1','gitee','pat','{"token":"PAT"}','2026-11-13 09:00:00'),
        ('u2','gitee','pat','{"token":"LATE"}','2026-11-13 09:00:00');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)

    with pytest.raises(RuntimeError, match="备份表存在存活表没有的行"):
        ds._fn_user_credentials_unique_kind()
    assert _count(con, "user_credentials__old_backup") == 2, "复核失败后备份表被删（数据不可回收）"
    assert _count(con, "user_credentials") == 1, "存活表数据被改动"
    con.close()


def test_unique_kind_reraises_when_swap_silently_fails(tmp_path, monkeypatch):
    """C-1：交换被 _destructive_ddl 吞成「成功」（返回 True 但没执行）→ 结构复核必须 raise。"""
    con = _legacy_uc(tmp_path / "silent.db")
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, config_json) VALUES
        ('u1','gitee','{"token":"PAT"}'), ('u1','huawei_cloud','{"ak":"x"}');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)
    real = ds._destructive_ddl

    def swallowed(c, ddl, **kw):
        if "RENAME" in ddl.upper():
            return True                     # 假装成功、实际什么都没做
        return real(c, ddl, **kw)

    monkeypatch.setattr(ds, "_destructive_ddl", swallowed)
    with pytest.raises(RuntimeError, match="交换后主键仍不含 kind|交换未完成|交换窗口内"):
        ds._fn_user_credentials_unique_kind()

    # 数据没被动：旧表还在、行数不变
    assert _count(con, "user_credentials") == 2
    con.close()


def test_uc_cleanup_gate_strict_requires_min_code_version(monkeypatch):
    """C-2：MySQL 侧 meta.min_code_version 未设置即拒绝重建（不再默认放行）。"""
    from src.api import db_setup as ds
    monkeypatch.setattr(ds, "_mysql_backend", lambda: True)
    monkeypatch.delenv("GRAPE_ALLOW_UNGUARDED_CLEANUP", raising=False)
    monkeypatch.setattr(ds, "get_min_code_version", lambda: "")

    with pytest.raises(RuntimeError, match="min_code_version 未设置"):
        ds._uc_cleanup_gate_strict()

    # 落后节点同样拒绝
    monkeypatch.setattr(ds, "get_min_code_version", lambda: "2026.12.01")
    monkeypatch.setattr(ds, "current_code_version", lambda: "g-2026.11.13-abc123")
    with pytest.raises(RuntimeError, match="落后节点"):
        ds._uc_cleanup_gate_strict()

    # 门槛达标 → 放行
    monkeypatch.setattr(ds, "current_code_version", lambda: "g-2026.12.02-abc123")
    ds._uc_cleanup_gate_strict()

    # 显式逃生口 → 放行（门禁沙盒库口径）
    monkeypatch.setattr(ds, "get_min_code_version", lambda: "")
    monkeypatch.setenv("GRAPE_ALLOW_UNGUARDED_CLEANUP", "1")
    ds._uc_cleanup_gate_strict()


def test_uc_cleanup_gate_strict_wired_before_rebuild():
    """C-2 结构守卫：严格门槛必须在建新表之前调用（放错位置等于没修）。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "src/api/db_setup.py").read_text(encoding="utf-8")
    gate = text.index("        _uc_cleanup_gate_strict()")
    rebuild = text.index("        c.execute(_UC_REBUILD_DDL)")
    assert gate < rebuild, "严格门槛没有排在重建之前"


def test_backfill_migration_registered_after_widen():
    """M-1：扩键后回填必须有自己的版本号（改 007 函数体不会重跑）。"""
    from src.api import db_setup as ds
    kinds = {m.version: (m.name, m.kind) for m in ds._MIGRATIONS}
    assert "202611170008" in kinds, "扩键后回填迁移未注册"
    assert kinds["202611170008"][1] == "backfill", "回填迁移应为 backfill（无破坏性 DDL）"


def test_unique_kind_detects_content_only_loss(tmp_path, monkeypatch):
    """M-3 内容型丢写：行数相同、备份里是同键旧值 → 必须 raise（只比行数会漏）。"""
    con = _legacy_uc(tmp_path / "content.db", key3=True)
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, kind, config_json) VALUES
        ('u1','gitee','oauth','{"access_token":"NEW"}');
    CREATE TABLE user_credentials__old_backup (
        user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL,
        updated_at TEXT);
    -- 备份行的 updated_at 更新 ⇒ 存活表拿的是旧值 ⇒ 真丢写
    INSERT INTO user_credentials__old_backup (user_id, provider, kind, config_json, updated_at)
        VALUES ('u1','gitee','oauth','{"access_token":"OLD"}','2026-11-13 10:00:00');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)

    with pytest.raises(RuntimeError, match="备份表存在存活表没有的行"):
        ds._fn_user_credentials_unique_kind()
    assert _count(con, "user_credentials__old_backup") == 1, "内容丢写时备份表被删"
    con.close()


def test_unique_kind_rebuilds_over_stale_scratch(tmp_path, monkeypatch):
    """M-3/P2-2：存活表仍是旧键时，非空中转表是派生数据 → 丢弃重建，不 raise。"""
    con = _legacy_uc(tmp_path / "stale.db")
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, config_json) VALUES
        ('u1','gitee','{"token":"PAT"}'), ('u1','huawei_cloud','{"ak":"x"}');
    CREATE TABLE user_credentials__uqkind (
        id TEXT, user_id TEXT NOT NULL, provider TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL,
        created_at TEXT, updated_at TEXT, PRIMARY KEY (user_id, provider, kind));
    INSERT INTO user_credentials__uqkind (user_id, provider, kind, config_json)
        VALUES ('u9','stale','','{}');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)

    ds._fn_user_credentials_unique_kind()          # 不应 raise
    assert _count(con, "user_credentials") == 2, "重建后行数不守恒"
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='user_credentials'").fetchone()[0]
    assert "PRIMARY KEY (user_id, provider, kind)" in sql
    assert not con.execute(
        "SELECT name FROM sqlite_master WHERE name='user_credentials__uqkind'").fetchone()
    con.close()


def test_backfill_migration_guards_non_owner_view(monkeypatch):
    """P3-2：008 与 007 同口径 —— 视图库抛 _NonOwnerObject（不盖章），不硬跑回填。"""
    import contextlib
    from src.api import db_setup as ds

    class _Cur:
        def execute(self, *a, **k):
            return self

    class _Db:
        def cursor(self):
            return _Cur()

    called = []
    monkeypatch.setattr(ds, "db", _Db())
    monkeypatch.setattr(ds, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(ds, "_uc_backfill_kind_rows", lambda: called.append(1))

    monkeypatch.setattr(ds, "_table_present", lambda c, t: True)
    monkeypatch.setattr(ds, "_is_base_table", lambda c, t: False)
    with pytest.raises(ds._NonOwnerObject):
        ds._fn_user_credentials_kind_backfill()
    assert not called, "视图库上不应执行回填"

    monkeypatch.setattr(ds, "_table_present", lambda c, t: False)   # 新装库
    ds._fn_user_credentials_kind_backfill()
    assert not called


def test_unique_kind_swap_recheck_uses_key_level():
    """结构守卫：复核必须走键级判定（缺键/备份更新才丢），不能退回行数或整行集合差。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "src/api/db_setup.py").read_text(encoding="utf-8")
    assert "_lost_after = _uc_lost_keys(" in text, "交换后复核未走键级判定"
    assert "_lost = _uc_lost_keys(" in text, "早退分支未走键级判定"
    assert "_uc_row_set(" not in text, "整行集合差会把「存活表被刷新覆盖」误判成丢写"


def test_uc_lost_keys_ignores_newer_live_value():
    """M-1：键在且存活行更新 ⇒ 不丢（多实例刷新覆盖旧值，不是丢写）。"""
    from src.api import db_setup as ds
    bak = {("u1", "gitee", "oauth"): ('{"t":"OLD"}', "2026-11-13 09:00:00")}
    live = {("u1", "gitee", "oauth"): ('{"t":"NEW"}', "2026-11-13 10:00:00")}
    assert ds._uc_lost_keys(bak, live) == []
    assert ds._uc_lost_keys(live, bak) == [("u1", "gitee", "oauth")]   # 反向：备份更新=丢


def test_uc_lost_keys_flags_missing_key():
    """M-1：存活表缺键 ⇒ 真丢（哪怕备份只有一行）。"""
    from src.api import db_setup as ds
    bak = {("u1", "gitee", "pat"): ('{"t":"X"}', "2026-11-13 09:00:00")}
    assert ds._uc_lost_keys(bak, {}) == [("u1", "gitee", "pat")]


def test_unique_kind_does_not_raise_when_live_value_is_newer(tmp_path, monkeypatch):
    """M-1 端到端：交换后存活表被刷新覆盖（更新值）→ 不 raise，备份正常清理。"""
    con = _legacy_uc(tmp_path / "newer.db", key3=True)
    con.executescript("""
    INSERT INTO user_credentials (user_id, provider, kind, config_json, updated_at) VALUES
        ('u1','gitee','oauth','{"access_token":"NEW"}','2026-11-13 10:00:00');
    CREATE TABLE user_credentials__old_backup (
        user_id TEXT NOT NULL, provider TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
        config_json TEXT NOT NULL, updated_at TEXT);
    INSERT INTO user_credentials__old_backup (user_id, provider, kind, config_json, updated_at)
        VALUES ('u1','gitee','oauth','{"access_token":"OLD"}','2026-11-13 09:00:00');
    """)
    con.commit()
    ds = _wire(monkeypatch, con)

    ds._fn_user_credentials_unique_kind()          # 不应 raise
    assert _count(con, "user_credentials") == 1
    assert not con.execute(
        "SELECT name FROM sqlite_master WHERE name='user_credentials__old_backup'").fetchone(), \
        "判定为「未丢写」后备份表应被清理"
    con.close()


def test_uc_cleanup_gate_strict_hints_fp_deployment(monkeypatch):
    """C-1：无 .git 部署（fp- 指纹）不可比 → 拒绝，且报错必须给出可执行指引。"""
    from src.api import db_setup as ds
    monkeypatch.setattr(ds, "_mysql_backend", lambda: True)
    monkeypatch.delenv("GRAPE_ALLOW_UNGUARDED_CLEANUP", raising=False)
    monkeypatch.setattr(ds, "get_min_code_version", lambda: "2026.11.13")
    monkeypatch.setattr(ds, "current_code_version", lambda: "fp-9a1b2c3d4e5f")
    with pytest.raises(RuntimeError) as ei:
        ds._uc_cleanup_gate_strict()
    msg = str(ei.value)
    assert "GRAPE_DEPLOY_VERSION" in msg and "fp-" in msg, f"缺可执行指引: {msg}"


def test_code_version_honours_deploy_env(monkeypatch):
    """C-1：镜像部署可用 GRAPE_DEPLOY_VERSION 注入可比版本（与 min_code_version 同源）。"""
    from src.api import code_version as cv
    monkeypatch.setenv("GRAPE_DEPLOY_VERSION", "2026.11.13")
    assert cv._compute_version() == "2026.11.13"
    monkeypatch.delenv("GRAPE_DEPLOY_VERSION")
    monkeypatch.setattr(cv, "_git_version", lambda: "")
    assert cv._compute_version().startswith("fp-")


def test_set_min_code_version_accepts_fp_fingerprint():
    """C-1：部署脚本必须接受 fp- 指纹原值（否则镜像部署永远写不进门槛）。"""
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "set_min_code_version", root / "scripts/set_min_code_version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._validate_version("2026.11.13") == ""
    assert mod._validate_version("fp-9a1b2c3d4e5f") == ""
    assert mod._validate_version("deadbeefcafe") != ""      # 纯哈希仍拒绝
    assert mod._validate_version("") != ""