"""org 域 id TEXT 化迁移行为锁（202611091300，D105 第三批）。

背景：organization.id 旧 DDL 为 INTEGER PRIMARY KEY AUTOINCREMENT，owner_id/
org_id/inviter_id/invited_by 及 13 张核心表 org_id 列为 INTEGER——grape_accounts.id
已 uuid7 字符串，写入 organization.owner_id SQLite 混型互查不中、MySQL 严格模式
写崩 1366 / 非严格截 0 错归属。迁移口径：
- 存量 int 保值（1 → '1' 字符串形态，不重编号不丢行）；
- 哨兵 0 → ''（''=全局/未归属；MySQL TEXT 列与 0 数值比较会把非数字串强转 0
  误匹配，'abc'=0 为 TRUE——根因级哨兵更替）；
- uuid 写入 owner_id 可查（混型互查根因消除）；
- 幂等：二次执行亲和判定命中 TEXT 分支且无混型行 → 零改动。
"""
import os, sqlite3
import importlib
import pytest

from src.api.db_setup import (
    _normalize_org_domain_text,
    _ORG_DOMAIN_TEXT_COLS,
    _ORG_ID_TABLES,
)


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """测试体切换 DEVOPS_DB_PATH 并 reload sm/db_setup——teardown 恢复 env 并
    全家族重挂（同 test_account_fk_user_id_text_migration 口径），防污染同进程后续文件。"""
    saved = {k: os.environ.get(k) for k in ("DEVOPS_DB_PATH",)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    for mod_name in ("src.domains.mgmt.user.db", "src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.persistence.repositories",
                     "src.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


_UUID = "01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67"


def _legacy_db(tmp_path):
    """模拟存量库：org 域列声明 INTEGER（旧 DDL），int 行 + uuid 行混存，
    含 UNIQUE(org_id, user_id) 与表达式默认值。"""
    p = tmp_path / "legacy_org_domain.db"
    db = sqlite3.connect(str(p))
    c = db.cursor()
    c.execute("""
        CREATE TABLE organization (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(128) NOT NULL,
            slug VARCHAR(64) NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            is_personal INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
    c.execute("""
        CREATE TABLE org_member (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            user_id VARCHAR(36) NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            invited_by INTEGER DEFAULT 0,
            UNIQUE (org_id, user_id)
        )""")
    c.execute("""
        CREATE TABLE managed_repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL,
            org_id INTEGER DEFAULT 0
        )""")
    c.execute("CREATE INDEX idx_mr_org ON managed_repos(org_id)")
    # int 存量行（含哨兵 0）+ uuid 行（混型形态）
    c.execute("INSERT INTO organization (name, slug, owner_id, is_personal) VALUES ('a', 'sa', 1, 1)")
    c.execute(f"INSERT INTO organization (name, slug, owner_id) VALUES ('b', 'sb', '{_UUID}')")
    c.execute("INSERT INTO org_member (org_id, user_id, invited_by) VALUES (1, '1', 0)")
    c.execute(f"INSERT INTO org_member (org_id, user_id, invited_by) VALUES ('{_UUID}', '{_UUID}', 1)")
    c.execute("INSERT INTO managed_repos (repo_key, org_id) VALUES ('g:o/r', 1)")
    c.execute("INSERT INTO managed_repos (repo_key, org_id) VALUES ('g:x/y', 0)")
    c.execute(f"INSERT INTO managed_repos (repo_key, org_id) VALUES ('g:u/v', '{_UUID}')")
    db.commit()
    return db, c


def _decl(c, table, col):
    return next(r[2] for r in c.execute(f"PRAGMA table_info({table})").fetchall()
                if r[1] == col)


class TestOrgDomainTextNormalize:
    def test_mixed_types_exist_before(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        for table, col in (("organization", "id"),
                           ("organization", "owner_id"),
                           ("org_member", "org_id"),
                           ("managed_repos", "org_id")):
            types = dict(c.execute(
                f"SELECT typeof({col}), COUNT(*) FROM {table} GROUP BY 1").fetchall())
            # id 主键纯 int；引用列 int 存量行 + uuid 行混存（INTEGER 亲和落 text）
            expect = {"integer": 2} if col == "id" else (
                {"integer": 2, "text": 1} if (table, col) == ("managed_repos", "org_id")
                else {"integer": 1, "text": 1})
            assert types == expect, f"{table}.{col}: {types}"

    def test_pk_text_and_values_preserved(self, tmp_path):
        """迁移保值：organization.id int 1 → '1'；uuid 写 owner_id 可查。"""
        db, c = _legacy_db(tmp_path)
        changed = _normalize_org_domain_text(db)
        db.commit()
        assert changed == -1  # 存量库 INTEGER 亲和 → 重建路径
        assert _decl(c, "organization", "id").upper() == "TEXT"
        assert _decl(c, "organization", "owner_id").upper() == "TEXT"
        # 主键保值：'1'/'2' 字符串形态，不重编号
        ids = [r[0] for r in c.execute("SELECT id FROM organization ORDER BY id")]
        assert ids == ["1", "2"], ids
        assert all(isinstance(i, str) for i in ids)
        # uuid 写 owner_id 可查（混型互查根因消除）
        assert c.execute("SELECT slug FROM organization WHERE owner_id=?",
                         (_UUID,)).fetchone()[0] == "sb"
        assert c.execute("SELECT COUNT(*) FROM organization WHERE owner_id='1'").fetchone()[0] == 1
        # org_id 引用列保值 + str 参数命中
        assert _decl(c, "org_member", "org_id").upper() == "TEXT"
        assert _decl(c, "managed_repos", "org_id").upper() == "TEXT"
        assert c.execute("SELECT repo_key FROM managed_repos WHERE org_id='1' "
                         "ORDER BY repo_key").fetchall()[0][0] == "g:o/r"
        assert c.execute("SELECT COUNT(*) FROM managed_repos WHERE org_id=?",
                         (_UUID,)).fetchone()[0] == 1
        # int 参数经 TEXT 亲和自动转换仍命中（老调用方 int 传参不受影响）
        assert c.execute("SELECT COUNT(*) FROM managed_repos WHERE org_id=?",
                         (1,)).fetchone()[0] == 1
        # 索引跨重建不丢
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='managed_repos'").fetchall()}
        assert "idx_mr_org" in idx
        org_idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='organization'").fetchall()}
        assert "sqlite_autoindex_organization_1" in org_idx  # UNIQUE(slug) 跨重建不丢

    def test_sentinel_zero_to_empty_string(self, tmp_path):
        """哨兵归零：org_id=0 / invited_by=0 / owner_id 0 → ''。"""
        db, c = _legacy_db(tmp_path)
        c.execute("INSERT INTO organization (name, slug, owner_id) VALUES ('z', 'sz', 0)")
        c.execute("INSERT INTO org_member (org_id, user_id, invited_by) VALUES (0, '9', 0)")
        db.commit()
        _normalize_org_domain_text(db)
        db.commit()
        # 哨兵行归 ''，且空串字面量可查
        assert c.execute("SELECT COUNT(*) FROM organization WHERE owner_id=''").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM managed_repos WHERE org_id=''").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM org_member WHERE org_id='' AND invited_by='' "
                         "AND user_id='9'").fetchone()[0] == 1
        # 哨兵不再以 0/'0' 形态残留
        for table, col in (("organization", "owner_id"),
                           ("org_member", "org_id"),
                           ("org_member", "invited_by"),
                           ("managed_repos", "org_id")):
            assert c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='0' OR {col}=0"
                             ).fetchone()[0] == 0, f"{table}.{col} 哨兵残留"

    def test_idempotent_second_run_zero_change(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        assert _normalize_org_domain_text(db) == -1
        db.commit()
        # 二次执行：亲和全 TEXT、无混型行、哨兵已归零 → 零改动
        assert _normalize_org_domain_text(db) == 0
        db.commit()
        total = sum(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("organization", "org_member", "managed_repos"))
        assert total == 7
        assert _decl(c, "organization", "id").upper() == "TEXT"
        assert _decl(c, "managed_repos", "org_id").upper() == "TEXT"

    def test_missing_tables_and_cols_skipped(self, tmp_path):
        """空库（表不存在）不报错零改动。"""
        db = sqlite3.connect(str(tmp_path / "empty.db"))
        assert _normalize_org_domain_text(db) == 0

    def test_enum_covers_org_tables(self):
        """清单完整性：3 张 org 表 + 13 张核心表 org_id 全量在列。"""
        required = {
            ("organization", "owner_id"),
            ("org_member", "org_id"), ("org_member", "invited_by"),
            ("org_invitation", "org_id"), ("org_invitation", "inviter_id"),
            *[(t, "org_id") for t in _ORG_ID_TABLES],
        }
        assert set(_ORG_DOMAIN_TEXT_COLS) == required
        assert len(_ORG_ID_TABLES) == 13
        # 运行时兜底补列 DDL 必须声明 TEXT 哨兵 ''（隔离环境建不齐可选表，用源码守卫）
        import inspect
        from src.api import db_setup as _ds
        assert "_ensure_column(c, table, \"org_id\", \"TEXT DEFAULT ''\")" \
            in inspect.getsource(_ds._migrate_add_org_id_columns)


class TestNoZeroSentinelLiterals:
    """MySQL 强转根因防回归：TEXT 列与 0 数值比较会把非数字串强转 0 误匹配
    （'abc'=0 TRUE）。本批文件范围内所有 org 哨兵 SQL 字面量必须是 ''，禁止
    `org_id=0` / `org_id = 0` / `org_id > 0` 形态回潮。"""

    _FILES = ("src/api/db_setup.py", "src/domains/mgmt/user/org_db.py",
              "src/api/org_context.py", "src/api/deps.py")
    _BAD = ("org_id=0", "org_id = 0", "org_id > 0", "org_id>0",
            "invited_by=0", "inviter_id=0", "owner_id=0")

    def test_no_zero_sentinel_in_sql(self):
        import re
        offenders = []
        for path in self._FILES:
            src = open(path, encoding="utf-8").read()
            for pat in self._BAD:
                for m in re.finditer(re.escape(pat), src):
                    line = src.count("\n", 0, m.start()) + 1
                    offenders.append(f"{path}:{line}: {pat}")
        assert not offenders, f"哨兵字面量 0 回潮（MySQL TEXT 列强转误匹配根因）: {offenders}"


def test_new_ddl_declares_text(tmp_path, monkeypatch):
    """行为锁：init_database 后 org 域全部清单列 PRAGMA 亲和即 TEXT/VARCHAR（新装零迁移）。"""
    import sys
    sys.path.insert(0, ".")
    monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "fresh.db"))
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    # 可选表 org_id 列由运行时幂等迁移补齐（init 期部分表未建会跳过），显式补跑一次
    db_setup._migrate_add_org_id_columns()
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        bad = []
        for table, col in _ORG_DOMAIN_TEXT_COLS:
            cols = c.execute(f"PRAGMA table_info({table})").fetchall()
            if not cols:
                continue  # 未在本库建的可选表由运行时迁移 _migrate_add_org_id_columns 兜底
            decl = next((r[2] or "" for r in cols if r[1] == col), None)
            if decl is None:
                # 可选核心表在本隔离环境未建（legacy schema 未跑），org_id 由
                # _migrate_add_org_id_columns 运行时兜底——形态由 DDL 字面量守卫覆盖
                continue
            elif decl.strip().upper() != "TEXT" and not decl.strip().upper().startswith("VARCHAR"):
                bad.append(f"{table}.{col}: {decl}")
        # organization.id 主键 TEXT
        decl_id = _decl(c, "organization", "id")
        assert decl_id.strip().upper() == "TEXT", decl_id
    assert not bad, f"新 DDL 未字符串亲和化: {bad}"
