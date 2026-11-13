"""D105 第二批补丁：unified_user_mapping.user_id rowid 别名回归行为锁。

背景：user_id INTEGER PRIMARY KEY 是 rowid 别名，SQLite 强类型——插 UUID 字符串
报 datatype mismatch（普通 INTEGER 列亲和可存 TEXT，rowid 别名例外）。
迁移 202611090003 整表重建 user_id→TEXT PRIMARY KEY（存量整型值字符串化保留）。
"""
import os, sys, sqlite3

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

import importlib
import pytest


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """_fresh/legacy 库改写 DEVOPS_DB_PATH 并 reload——进程级全局污染，跑完还原。"""
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
                     "src.domains.mgmt.user.org_db", "src.kernel.persistence.repositories",
                     "src.kernel.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


def _reload_on(dbp):
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)


def _legacy_db(tmp_path):
    """存量库：user_id 为 rowid 别名（INTEGER PRIMARY KEY），带整型存量行。"""
    dbp = str(tmp_path / "legacy.db")
    con = sqlite3.connect(dbp)
    con.execute("""
        CREATE TABLE unified_user_mapping (
            user_id INTEGER PRIMARY KEY,
            gitcode_user TEXT DEFAULT '', gitcode_nickname TEXT DEFAULT '',
            feishu_open_id TEXT DEFAULT '', feishu_name TEXT DEFAULT '',
            app_id TEXT DEFAULT '', github_login TEXT DEFAULT '',
            dingtalk_user_id TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')))""")
    con.execute("INSERT INTO unified_user_mapping(user_id, gitcode_user, feishu_open_id) "
                "VALUES (24, 'legacy_user', 'ou_legacy')")
    con.commit(); con.close()
    _reload_on(dbp)
    from src.api import db_setup
    db_setup.init_database()
    return dbp


def test_rowid_alias_fresh_ddl_is_text_pk(tmp_path):
    """新装库：unified_user_mapping.user_id 直出 TEXT PK。"""
    dbp = str(tmp_path / "fresh.db")
    _reload_on(dbp)
    from src.api import db_setup
    db_setup.init_database()
    sql = sqlite3.connect(dbp).execute(
        "SELECT sql FROM sqlite_master WHERE name='unified_user_mapping'").fetchone()[0]
    assert "user_id TEXT PRIMARY KEY" in sql.replace("  ", " "), sql


def test_rowid_alias_migration_preserves_legacy_int_ids(tmp_path):
    """存量库迁移：整型 user_id 字符串化原样保留（24 → '24'），数据零丢失。"""
    dbp = _legacy_db(tmp_path)
    con = sqlite3.connect(dbp)
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='unified_user_mapping'").fetchone()[0]
    assert "user_id TEXT PRIMARY KEY" in sql.replace("  ", " "), sql
    rows = con.execute("SELECT user_id, gitcode_user, feishu_open_id "
                       "FROM unified_user_mapping").fetchall()
    assert rows == [("24", "legacy_user", "ou_legacy")], rows
    con.close()


def test_rowid_alias_uuid_insert_roundtrip(tmp_path):
    """迁移后新 UUIDv7 user_id 插入/读回成功（datatype mismatch 回归锁）。"""
    dbp = _legacy_db(tmp_path)
    import uuid as _uuid
    uid = str(_uuid.uuid4())
    con = sqlite3.connect(dbp)
    con.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id) "
                "VALUES (?, 'uuid_user', 'ou_uuid')", (uid,))
    con.commit()
    row = con.execute("SELECT user_id, gitcode_user FROM unified_user_mapping "
                      "WHERE gitcode_user='uuid_user'").fetchone()
    con.close()
    assert row == (uid, "uuid_user"), row


def test_rowid_alias_migration_idempotent(tmp_path):
    """幂等：user_id 已 TEXT 后二次跑全量迁移零写入不报错。"""
    dbp = _legacy_db(tmp_path)
    from src.api import db_setup
    db_setup.init_database()   # 第二次全量跑不报错
    con = sqlite3.connect(dbp)
    n = con.execute("SELECT COUNT(*) FROM grape_t_schema_migrations "
                    "WHERE version='202611090003'").fetchone()[0]
    rows = con.execute("SELECT user_id FROM unified_user_mapping ORDER BY user_id").fetchall()
    con.close()
    assert n >= 1
    assert rows == [("24",)], rows
