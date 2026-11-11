"""D105 第一批：external_identities 主键 UUID 化——迁移/写入/读取行为锁。

口径（用户裁定）：主键直接字符串化；存量行主键值原样保留（字符串化形态）；
新行应用层 uuid4；grape_user_id 等跨表引用本批不动。
"""
import os, sys, sqlite3, uuid as _uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import importlib
import pytest


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """_fresh_db 改写 DEVOPS_DB_PATH 并 reload session_manager/db_setup——
    这是进程级全局污染：不恢复则后续测试（如同文件外的 admin 平台卡测试）
    的写入落进本文件的临时库、而 src.domains.mgmt.user.db 等旧绑定仍读原库，读写分裂。
    每个用例跑完恢复 env 并重载受影响模块，把全局态还原。"""
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
    # 所有 `from src.kernel.session_manager import db` 持旧绑定的模块——全家族重挂
    for mod_name in ("src.domains.mgmt.user.db", "src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.persistence.repositories",
                     "src.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


def _fresh_db(tmp_path):
    """建独立临时库并跑 provision（触发含 D105 迁移的完整建表链）。"""
    dbp = str(tmp_path / "t.db")
    os.environ["DEVOPS_DB_PATH"] = dbp
    import importlib
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    return dbp


def test_d105_new_install_ddl_is_text_pk(tmp_path):
    """新装库：external_identities.id 直出 TEXT PK（无 AUTOINCREMENT）。"""
    dbp = _fresh_db(tmp_path)
    sql = sqlite3.connect(dbp).execute(
        "SELECT sql FROM sqlite_master WHERE name='external_identities'").fetchone()[0]
    assert "id" in sql and "TEXT PRIMARY KEY" in sql.upper().replace("  ", " ")
    assert "AUTOINCREMENT" not in sql


def test_d105_migration_stringifies_legacy_ids(tmp_path):
    """存量库迁移：老整型 id 字符串化原样保留（"4" 不重编号），数据零丢失。"""
    dbp = str(tmp_path / "legacy.db")
    con = sqlite3.connect(dbp)
    con.execute("""
        CREATE TABLE external_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, grape_user_id INTEGER NOT NULL,
            provider VARCHAR(32) NOT NULL, external_id VARCHAR(128) NOT NULL,
            external_login VARCHAR(128) DEFAULT '', external_name VARCHAR(128) DEFAULT '',
            external_avatar VARCHAR(512) DEFAULT '', access_token TEXT DEFAULT '',
            refresh_token TEXT DEFAULT '', token_expires_at TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}', status INTEGER DEFAULT 1,
            unbound_at TEXT DEFAULT NULL, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
            UNIQUE (provider, external_id), UNIQUE (grape_user_id, provider))""")
    con.execute("INSERT INTO external_identities(id,grape_user_id,provider,external_id,status) "
                "VALUES (4,24,'gitee','u1',1), (7,25,'huawei','u2',1)")
    con.commit(); con.close()
    os.environ["DEVOPS_DB_PATH"] = dbp
    import importlib
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    con = sqlite3.connect(dbp)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='external_identities'").fetchone()[0]
    assert "AUTOINCREMENT" not in sql
    rows = con.execute("SELECT id, provider, grape_user_id, status FROM external_identities ORDER BY id").fetchall()
    # grape_user_id 自 202611091000（根修 A）起归一为 TEXT 亲和：存量 int 值
    # 字符串化保值（24 → '24'），与 uuid 写入同型——不再断言 int 形态。
    assert rows == [("4", "gitee", "24", 1), ("7", "huawei", "25", 1)], "老值字符串化保留、行数据零丢失"


def test_d105_migration_idempotent(tmp_path):
    """迁移幂等：已 TEXT PK 的表二次跑直接返回。"""
    dbp = _fresh_db(tmp_path)
    from src.api import db_setup
    db_setup.init_database()   # 第二次全量跑不报错不重复迁移
    con = sqlite3.connect(dbp)
    n = con.execute("SELECT COUNT(*) FROM grape_t_schema_migrations "
                    "WHERE version='202611120002'").fetchone()[0]
    assert n >= 1


def test_d105_create_generates_uuid_pk(tmp_path):
    """新行写入：应用层 uuid4 主键（36 字符标准形式），业务键读写不受影响。"""
    _fresh_db(tmp_path)
    from src.domains.mgmt.auth import grape_account_db as gdb
    ok = gdb.create_external_identity(24, "gitee", "uid-x1", external_login="u1")
    assert ok
    con = sqlite3.connect(os.environ["DEVOPS_DB_PATH"])
    row = con.execute("SELECT id FROM external_identities WHERE provider='gitee'").fetchone()
    assert row and len(row[0]) == 36 and row[0].count("-") == 4, f"主键应为 UUID36: {row}"
    # 读路径（业务键）照常
    ident = gdb.get_user_identities(24)
    assert any(i["provider"] == "gitee" for i in ident)
