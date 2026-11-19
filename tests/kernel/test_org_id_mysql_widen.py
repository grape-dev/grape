"""MySQL 升级库 org_id 1366 回归（需 GRAPE_GATE_TEST=1 且可连 MySQL 才跑）。

线上形态复现：存量库（老 DDL，org_id INT）升级到新代码后，新建 runtime 凭据
把「未归属/个人组织」写成 '' 哨兵 → (1366, "Incorrect integer value: ''''")

本用例在同一台 MySQL 上用独立 scratch 库还原这条链：
  1. 建老形态表（org_id INT）并塞存量行；
  2. 直接写 '' → 断言 1366（证明用例确实咬合缺陷）；
  3. 调 _widen_mysql_org_ids() → 列变字符串族、存量值 '1'/'2' 保留、哨兵 0 → ''；
  4. 同一条 '' 写入成功；重复调用零改动（幂等）。

本机演练（conftest 会清空 DEVOPS_DB_* 防误连生产库，故本用例用独立变量名）：
    GRAPE_GATE_TEST=1 GRAPE_MYSQL_HOST=<host> GRAPE_MYSQL_USER=root \
    GRAPE_MYSQL_PASSWORD=<pw> python -m pytest tests/kernel/test_org_id_mysql_widen.py -q
"""
import os

import pytest

_GRAPE_GATE_TEST = os.environ.get("GRAPE_GATE_TEST", "") == "1"

pytestmark = pytest.mark.skipif(
    not _GRAPE_GATE_TEST,
    reason="MySQL 演练用例：需 GRAPE_GATE_TEST=1（CI 默认无 MySQL）")


def _connect(db=None):
    import pymysql
    return pymysql.connect(
        host=os.environ.get("GRAPE_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("GRAPE_MYSQL_PORT", "3306")),
        user=os.environ.get("GRAPE_MYSQL_USER", "root"),
        password=os.environ.get("GRAPE_MYSQL_PASSWORD", "") or None,
        database=db, charset="utf8mb4", autocommit=True)


@pytest.fixture()
def scratch_db():
    """独立 scratch 库（用完即拆）。"""
    import pymysql  # noqa: F401  (保留显式依赖声明)

    name = (os.environ.get("GRAPE_GATE_TEST_DB", "grape_gate_test") + "_orgtext")
    root = _connect()
    with root.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {name}")
        c.execute(f"CREATE DATABASE {name}")
    root.close()
    conn = _connect(name)
    # 生产 RDS 默认 STRICT_TRANS_TABLES；本地演练库可能为空 sql_mode（非严格，
    # '' 写 INT 只会告警并截成 0），这里显式对齐生产语义，否则 1366 用例恒过。
    with conn.cursor() as c:
        c.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
        c.execute(
            "CREATE TABLE organization (id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "name VARCHAR(128) NOT NULL, slug VARCHAR(64) NOT NULL UNIQUE, "
            "owner_id INT NOT NULL) ENGINE=InnoDB")
        c.execute("INSERT INTO organization (name, slug, owner_id) VALUES ('a','a',1),('b','b',2)")
        c.execute(
            "CREATE TABLE org_member (id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "org_id INT NOT NULL, user_id INT NOT NULL, invited_by INT DEFAULT 0, "
            "UNIQUE KEY uq_member (org_id, user_id)) ENGINE=InnoDB")
        c.execute("INSERT INTO org_member (org_id, user_id, invited_by) VALUES (1,7,0),(2,8,1)")
        c.execute(
            "CREATE TABLE runtime_credentials (id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "token_hash VARCHAR(191) NOT NULL, name VARCHAR(191) NOT NULL, "
            "owner_name TEXT, org_id INT DEFAULT 0) ENGINE=InnoDB")
        c.execute("INSERT INTO runtime_credentials (token_hash, name, owner_name, org_id) "
                  "VALUES ('h1','c1','u1',0)")
    try:
        yield conn
    finally:
        conn.close()
        root = _connect()
        with root.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS {name}")
        root.close()


def _col_types(conn, table):
    with conn.cursor() as c:
        c.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        return dict(c.fetchall())


def test_empty_sentinel_into_int_org_id_raises_1366(scratch_db):
    """缺陷咬合证明：字符串不能写进 INT org_id 列（线上报错原文）。"""
    import pymysql
    with scratch_db.cursor() as c:
        with pytest.raises(pymysql.err.DataError) as ei:
            c.execute("INSERT INTO runtime_credentials (token_hash, name, owner_name, org_id) "
                      "VALUES ('h2','c2','u2','')")
    assert ei.value.args[0] == 1366, ei.value


def test_widen_mysql_org_ids_converts_and_is_idempotent(scratch_db):
    from src.api.db_setup import _widen_mysql_org_ids

    altered = _widen_mysql_org_ids(scratch_db)
    # 本 fixture 建了 organization(id, owner_id) + org_member(org_id, invited_by)
    # + runtime_credentials(org_id) 共 5 列
    assert altered == 5, f"应转换 5 列，实际 {altered}"

    types = _col_types(scratch_db, "runtime_credentials")
    assert types["org_id"] in ("text", "mediumtext", "longtext", "varchar"), types

    with scratch_db.cursor() as c:
        c.execute("SELECT id, owner_id FROM organization ORDER BY id")
        assert c.fetchall() == (("1", "1"), ("2", "2"))     # 存量值保值字符串化
        c.execute("SELECT org_id, invited_by FROM org_member ORDER BY id")
        # invited_by 与 SQLite 分支同口径（_ORG_SENTINEL_COLS）：0 → ''
        assert c.fetchall() == (("1", ""), ("2", "1"))
        c.execute("SELECT org_id FROM runtime_credentials")
        assert c.fetchall() == (("",),)                      # 哨兵 0 → ''

    # 同一条 '' 写入现在成功（前端「创建」走的正是这条路）
    with scratch_db.cursor() as c:
        c.execute("INSERT INTO runtime_credentials (token_hash, name, owner_name, org_id) "
                  "VALUES ('h3','c3','u3','')")

    # 幂等：第二遍零改动
    assert _widen_mysql_org_ids(scratch_db) == 0