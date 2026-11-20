"""Issue #3469 的 MySQL 真库门禁用例（需 GRAPE_GATE_TEST=1 且可连 MySQL 才跑）。

复审指出：O（uq_active_email 生成列 + 换索引）与 I（code_hash 列宽）最容易出事的
正是 MySQL 分支，而仓库里原本一条自动化用例都没有。本文件把这两条搬进可复算的门禁：

1. `code_hash` 列宽在 MySQL 上确实 >= 191，且带 kid 的 67 字符哈希能真正落库
   （严格模式；短列会 1406，非严格模式会截断 —— 两种都断言不到就说明列宽没修）。
2. `uq_active_email` 收敛后：status=1/3 仍唯一、status=4 的墓碑行不占键、
   `''` 归 NULL；`active_email` 是 STORED 生成列。
3. 迁移幂等：同样的 fn 跑两次零改动。

本机演练（conftest 会清空 DEVOPS_DB_* 防误连生产库，故用独立变量名）：
    GRAPE_GATE_TEST=1 GRAPE_MYSQL_HOST=<host> GRAPE_MYSQL_USER=root \\
    GRAPE_MYSQL_PASSWORD=<pw> python -m pytest tests/api/test_issue_3469_mysql_gate.py -q
"""
from __future__ import annotations

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


def _compat(conn):
    """生产同款兼容层（把迁移里的 `?` 翻成 `%s`），连同一个 scratch 库。"""
    from src.kernel.mysql_compat import MySQLCompatDB

    with conn.cursor() as c:
        c.execute("SELECT DATABASE()")
        name = c.fetchone()[0]
    return MySQLCompatDB(
        host=os.environ.get("GRAPE_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("GRAPE_MYSQL_PORT", "3306")),
        user=os.environ.get("GRAPE_MYSQL_USER", "root"),
        password=os.environ.get("GRAPE_MYSQL_PASSWORD", ""),
        database=name)


@pytest.fixture()
def scratch_db():
    """独立 scratch 库：按新装口径建 verification_codes + grape_accounts 的旧索引形态。"""
    name = os.environ.get("GRAPE_GATE_TEST_DB", "grape_gate_test") + "_i3469"
    root = _connect()
    with root.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {name}")
        c.execute(f"CREATE DATABASE {name}")
    root.close()

    from src.domains.mgmt.schema_ddl import _GRAPE_ACCOUNTS_DDL, _VERIFICATION_CODES_DDL
    from src.kernel.mysql_compat import translate_sql

    conn = _connect(name)
    with conn.cursor() as c:
        c.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
        for ddl in (_VERIFICATION_CODES_DDL, _GRAPE_ACCOUNTS_DDL):
            c.execute(translate_sql(ddl))
        # 旧形态：全列唯一索引（Issue #3469 O 要收敛掉的那份）
        c.execute("CREATE UNIQUE INDEX uq_active_email ON grape_accounts(email)")
        # 存量：墓碑行（status=4）占着邮箱 + 一行空串
        c.execute("INSERT INTO grape_accounts (id, username, email, password_hash, status) "
                  "VALUES ('u4','gone','gone@example.com','',4)")
        c.execute("INSERT INTO grape_accounts (id, username, email, password_hash, status) "
                  "VALUES ('u5','blank','','',1)")
    yield conn
    conn.close()
    root = _connect()
    with root.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {name}")
    root.close()


def test_code_hash_column_fits_kid_prefixed_hash(scratch_db):
    """I 复审：code_hash 必须装得下 'kid:<64hex>'（67 字符）并能真正落库。"""
    with scratch_db.cursor() as c:
        c.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='verification_codes' "
            "AND COLUMN_NAME='code_hash'")
        width = int(c.fetchone()[0])
        assert width >= 191, f"code_hash 列宽 {width}，装不下 kid 前缀哈希"

        kid_hash = "k1:" + "a" * 64
        c.execute(
            "INSERT INTO verification_codes (email, code_hash, scene, ip, attempts, used,"
            " expires_at, created_at) VALUES (%s,%s,'register','127.0.0.1',0,0,"
            " '2099-01-01 00:00:00','2026-01-01 00:00:00')", ("a@b.com", kid_hash))
        c.execute("SELECT code_hash FROM verification_codes WHERE email='a@b.com'")
        assert c.fetchone()[0] == kid_hash, "带 kid 的哈希被截断/改写"


def test_active_email_index_converged_on_mysql(scratch_db, monkeypatch):
    """O 复审：生成列 + 唯一索引语义（墓碑让键、活跃唯一、''归NULL）。"""
    import src.api.db_setup as ds
    from src.domains.mgmt.migrations.active_email_uniqueness import (
        migrate_active_email_uniqueness,
    )

    # 本进程是测试环境（conftest 清掉 DEVOPS_DB_*），迁移的 owner 守卫/DLL 分支
    # 由 db_setup 的后端探针决定——这里显式声明"这台库是 MySQL"。
    monkeypatch.setattr("src.api.db_setup._mysql_backend", lambda: True, raising=False)

    stats = migrate_active_email_uniqueness(conn=_compat(scratch_db))
    assert stats["blank_to_null"] >= 1

    with scratch_db.cursor() as c:
        c.execute("SELECT EXTRA FROM information_schema.columns "
                  "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
                  "AND COLUMN_NAME='active_email'")
        extra = (c.fetchone() or [""])[0].upper()
        assert "STORED GENERATED" in extra

        c.execute("SELECT COLUMN_NAME FROM information_schema.statistics "
                  "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_accounts' "
                  "AND INDEX_NAME='uq_active_email' ORDER BY SEQ_IN_INDEX")
        assert [r[0] for r in c.fetchall()] == ["active_email"]

        # 墓碑行（status=4）不再占键：同邮箱可被活跃账号使用
        c.execute("INSERT INTO grape_accounts (id, username, email, password_hash, status) "
                  "VALUES ('u6','new1','gone@example.com','',1)")

        # 两条活跃（status=1）同邮箱必须撞唯一键
        import pymysql
        with pytest.raises(pymysql.err.IntegrityError):
            c.execute("INSERT INTO grape_accounts (id, username, email, password_hash, status) "
                      "VALUES ('u7','new2','gone@example.com','',1)")

    # 幂等：第二次跑零改动
    stats2 = migrate_active_email_uniqueness(conn=_compat(scratch_db))
    assert stats2["created"] is False and stats2["dropped_old"] is False


def test_verification_hash_width_migration_on_mysql(scratch_db, monkeypatch):
    """I 迁移：把旧的 VARCHAR(64) 加宽到 191，且二次执行零改动。"""
    from src.domains.mgmt.migrations.verification_hash_width import (
        migrate_verification_hash_width,
    )

    monkeypatch.setattr("src.api.db_setup._mysql_backend", lambda: True, raising=False)

    with scratch_db.cursor() as c:
        # 退回旧形态，模拟存量库
        c.execute("ALTER TABLE verification_codes MODIFY code_hash VARCHAR(64) NOT NULL")

    stats = migrate_verification_hash_width(conn=_compat(scratch_db))
    assert stats["altered"] is True and stats["after"] >= 191

    stats2 = migrate_verification_hash_width(conn=_compat(scratch_db))
    assert stats2["altered"] is False and stats2["after"] >= 191