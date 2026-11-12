"""D105 第二批：grape_accounts 主键 UUID 化 + user_id 引用列放宽——迁移/写入/鉴权行为锁。

口径（已拍板 2026-11-09）：主键 UUID、存量整型值字符串化保留（"24" 不重编号）；
user_id 引用列 VARCHAR(36)；调用方一次性清完（sub/lastrowid/强转全量 str 化）。
"""
import os, sys, sqlite3, uuid as _uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import importlib
import pytest


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """_fresh_db 改写 DEVOPS_DB_PATH 并 reload session_manager/db_setup——
    进程级全局污染，每用例后恢复 env 并重载受影响模块（同 D105 第一批铁律）。
    裁决口径（仓库既有）：需要新绑定的用例在测试体内 reload（teardown 之后的
    家族重挂会让下游文件 collection 期捕获的旧函数对象失配——比不重载更糟，
    实锤：grape_auth 13 failed）——多文件组合的顺序污染按失败集 diff 裁决。"""
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
    import src.domains.mgmt.user.db as user_db
    importlib.reload(user_db)
    # 所有 `from src.kernel.session_manager import db` 持旧绑定的模块——全家族重挂
    for mod_name in ("src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.kernel.persistence.repositories",
                     "src.kernel.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


def _rebind(mod_name):
    """按模块名重挂并返回当前对象（D105 家族铁律的落地形态）。

    conftest 的逐用例 sys.modules 快照/恢复会让「父包属性 → 旧模块对象」与
    sys.modules 条目脱钩；`from pkg import sub` 命中旧属性后再 reload 会抛
    ImportError: module ... not in sys.modules。import_module 先取 canonical 条目，
    reload 后即为可用绑定（与 test_d105_* 兄弟文件同一写法）。
    """
    return importlib.reload(importlib.import_module(mod_name))


def _fresh_db(tmp_path, name="t.db"):
    dbp = str(tmp_path / name)
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    return dbp


def _legacy_db(tmp_path, name="legacy.db"):
    """建存量形态库：grape_accounts INTEGER AUTOINCREMENT + 老整型账号。"""
    dbp = str(tmp_path / name)
    con = sqlite3.connect(dbp)
    con.execute("""
        CREATE TABLE grape_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(64) NOT NULL UNIQUE,
            email VARCHAR(255) DEFAULT NULL,
            email_source VARCHAR(16) DEFAULT NULL,
            password_hash TEXT NOT NULL DEFAULT '',
            nickname VARCHAR(128) DEFAULT '',
            avatar_url VARCHAR(512) DEFAULT '',
            status INTEGER DEFAULT 1,
            token_version INTEGER DEFAULT 0,
            email_verified INTEGER DEFAULT 0,
            email_is_placeholder INTEGER DEFAULT 0,
            created_by_oauth INTEGER DEFAULT 0,
            last_active_at TEXT DEFAULT NULL,
            last_login_at TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
    con.execute("INSERT INTO grape_accounts(id, username, status, token_version) "
                "VALUES (24, 'legacy_user', 1, 0)")
    con.commit()
    con.close()
    return dbp


def test_batch2_fresh_ddl_is_text_pk(tmp_path):
    """新装库：grape_accounts.id 直出 TEXT PK（无 AUTOINCREMENT）。"""
    dbp = _fresh_db(tmp_path)
    sql = sqlite3.connect(dbp).execute(
        "SELECT sql FROM sqlite_master WHERE name='grape_accounts'").fetchone()[0]
    assert "TEXT PRIMARY KEY" in sql.upper().replace("  ", " ")
    assert "AUTOINCREMENT" not in sql


def test_batch2_migration_stringifies_legacy_ids(tmp_path):
    """存量库迁移：老整型 id 字符串化原样保留（'24' 不重编号），行数据零丢失。"""
    dbp = _legacy_db(tmp_path)
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    con = sqlite3.connect(dbp)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='grape_accounts'").fetchone()[0]
    assert "AUTOINCREMENT" not in sql
    row = con.execute("SELECT id, username, status FROM grape_accounts").fetchone()
    assert row == ("24", "legacy_user", 1), f"老值字符串化保留: {row}"


def test_batch2_migration_idempotent(tmp_path):
    """迁移幂等：已 TEXT PK 的库二次跑 init 零报错零重写。"""
    dbp = _legacy_db(tmp_path)
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    n0 = sqlite3.connect(dbp).execute("SELECT COUNT(*) FROM grape_accounts").fetchone()[0]
    db_setup.init_database()
    con = sqlite3.connect(dbp)
    assert con.execute("SELECT COUNT(*) FROM grape_accounts").fetchone()[0] == n0
    assert con.execute("SELECT COUNT(*) FROM grape_t_schema_migrations "
                       "WHERE version='202611120003'").fetchone()[0] >= 1


def test_batch2_create_account_returns_uuid36(tmp_path):
    """新行写入：应用层 uuid4 主键（36 字符标准形式），密码/OAuth 两创建路径一致。"""
    _fresh_db(tmp_path)
    gdb = _rebind("src.domains.mgmt.auth.grape_account_db")   # 多文件组合时旧绑定指向他库——用前按名重挂
    a = gdb.create_grape_account("u1", "u1@test.local", "pw123456")
    assert a and len(a["id"]) == 36 and a["id"].count("-") == 4, f"密码注册应返回 UUID36: {a}"
    import uuid as _u
    if hasattr(_u, "uuid7"):
        assert a["id"][14] == "7", f"新装库主键应为 UUIDv7（version 位=7）: {a['id']}"
    b = gdb.create_grape_account_oauth("u2", provider="gitee")
    assert b and len(b["id"]) == 36 and b["id"].count("-") == 4, f"OAuth 注册应返回 UUID36: {b}"
    # 按新 id 读取（get_grape_account_by_id 现为 str 语义）
    got = gdb.get_grape_account_by_id(a["id"])
    assert got and got["username"] == "u1"


def test_batch2_legacy_int_sub_jwt_authenticates(tmp_path):
    """向前兼容锁：迁移后老账号（id '24'）+ 老 JWT（sub='24'）照常过鉴权。"""
    dbp = _legacy_db(tmp_path)
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    # 其 `from src.kernel.session_manager import db` 旧绑定需重挂（按名，避开父包旧属性）
    gdb_mod = _rebind("src.domains.mgmt.auth.grape_account_db")
    jwt_utils = _rebind("src.api.jwt_utils")
    deps = _rebind("src.api.deps")
    # Issue #343 一次性 +1 是既有设计（老 JWT 本就应失效一次）——用迁移后实际 ver 签发，
    # 本用例锁的是「int 形态 sub 能命中字符串化老 id」这一向前兼容点
    ver = gdb_mod.get_status_and_token_version("24")[1]
    token = jwt_utils.create_access_token(24, "legacy_user", ver)   # 老调用方传 int
    user = deps.get_current_user(f"Bearer {token}")
    assert user["id"] == "24", f"老 int sub 应命中字符串化老账号: {user}"
    assert user["username"] == "legacy_user"


def test_batch2_ownership_str_int_compat(tmp_path):
    """跨类型归属锁：存量库 INTEGER user_id 行（int 24）× 鉴权链 str id——
    chat 归属校验必须命中（D105 实弹回归：conv['user_id'] != user['id']
    在 int/str 混型下恒不等 → 会话 404）。"""
    dbp = _legacy_db(tmp_path)
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    gdb_mod = _rebind("src.domains.mgmt.auth.grape_account_db")
    jwt_utils = _rebind("src.api.jwt_utils")
    deps = _rebind("src.api.deps")
    # 存量形态会话 + 消息（user_id 整型 24，老代码写入形态）
    from src.kernel.session_manager import db as smdb, db_lock as smdl
    with smdl:
        smdb.cursor().execute(
            "INSERT INTO chat_conversations (id, user_id, agent_session_id, runtime_session_id, "
            "repo_key, im_channel_id, title, status, conv_type) VALUES (?,?,?,?,?,?,?,?,?)",
            ("conv-legacy-1", 24, "", "", "gitee:o/r", "", "存量会话", "active", "general"))
        smdb.cursor().execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES (?,?,?,datetime('now'))", ("conv-legacy-1", "user", "hello"))
        smdb.commit()
    ver = gdb_mod.get_status_and_token_version("24")[1]
    token = jwt_utils.create_access_token(24, "legacy_user", ver)
    user = deps.get_current_user(f"Bearer {token}")
    chat_routes = _rebind("src.domains.runtime.routes.chat")
    conv = chat_routes.get_conversation("conv-legacy-1")
    assert conv is not None
    # 修复后的归属判定：str(conv.user_id) == str(user.id) 必须命中
    assert str(conv["user_id"] or "") == str(user["id"] or "")


def test_batch2_user_id_column_accepts_uuid_roundtrip(tmp_path):
    """引用列放宽：user_id 传 UUID 字符串写读照常（user_repos 往返）。"""
    _fresh_db(tmp_path)
    user_db = _rebind("src.domains.mgmt.user.db")
    uid = str(_uuid.uuid4())
    user_db.save_user_repos(uid, [{"repo_key": "gitcode:o/r", "full_name": "o/r",
                                   "repo_owner": "o", "repo_name": "r"}])
    rows = user_db.get_user_repos(uid)
    assert any(r["repo_key"] == "gitcode:o/r" for r in rows)
