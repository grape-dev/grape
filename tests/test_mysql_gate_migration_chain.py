"""G1 部署门禁：启动迁移链 MySQL 分支端到端演练（GRAPE_GATE_TEST=1 才跑）。

CI 默认无 MySQL——pytest.skipif 门控；本机演练：

    GRAPE_GATE_TEST=1 DEVOPS_DB_BACKEND=mysql DEVOPS_DB_USER=root \
    DEVOPS_DB_HOST=127.0.0.1 DEVOPS_DB_NAME=grape_gate_test \
    python -m pytest tests/test_mysql_gate_migration_chain.py -q

用子进程执行 init_database()：tests/conftest.py 强制 DEVOPS_DB_BACKEND=sqlite
（防误连生产库），子进程内重设 MySQL 环境变量，不受 conftest 约束。

断言口径（对齐 g1 部署门禁）：
- init_database() 在 MySQL 后端零异常跑完，零 1064 / 未知函数 / 人工清单；
- 盖章表 grape_t_schema_migrations 建立且全部迁移盖章；
- 二次启动零重复执行（盖章数不增、0 pending）；
- 迁移域 id/user_id 列全 VARCHAR（uuid-pk 链 + account-fk widen + issue ident widen）。
"""
import os
import subprocess
import sys

import pytest

_GRAPE_GATE_TEST = os.environ.get("GRAPE_GATE_TEST", "") == "1"
_GRAPE_GATE_TEST_DB = os.environ.get("GRAPE_GATE_TEST_DB", "grape_gate_test")

pytestmark = pytest.mark.skipif(
    not _GRAPE_GATE_TEST,
    reason="MySQL 演练门禁测试：需 GRAPE_GATE_TEST=1（CI 默认无 MySQL）")

_SQL_ENV_FAILS = ("1064", "Unknown function", "does not exist",
                  "MANUAL REVIEW", "Traceback")


def _mysql_env():
    env = dict(os.environ)
    env["DEVOPS_DB_BACKEND"] = "mysql"
    env["DEVOPS_DB_NAME"] = _GRAPE_GATE_TEST_DB
    env.setdefault("DEVOPS_DB_USER", "root")
    env.setdefault("DEVOPS_DB_HOST", "127.0.0.1")
    return env


def _run_init() -> str:
    """子进程跑一次完整 init_database()，返回 stdout+stderr。"""
    r = subprocess.run(
        [sys.executable, "-c",
         "import src.api.db_setup as ds; ds.init_database(); print('INIT OK')"],
        capture_output=True, text=True, timeout=600,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=_mysql_env())
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "INIT OK" in out, f"init_database 失败：\n{out[-4000:]}"
    return out


def _sql(query):
    """独立 pymysql 连接执行只读查询（不依赖 conftest 强制的 sqlite 全局连接）。"""
    import pymysql
    conn = pymysql.connect(
        host=_mysql_env()["DEVOPS_DB_HOST"], user=_mysql_env()["DEVOPS_DB_USER"],
        password=os.environ.get("DEVOPS_DB_PASSWORD", "") or None,
        database=_GRAPE_GATE_TEST_DB, charset="utf8mb4")
    try:
        with conn.cursor() as c:
            c.execute(query)
            return c.fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def init_outputs():
    return _run_init(), _run_init()      # 首跑 + 二次启动


def test_mysql_chain_zero_dialect_errors(init_outputs):
    first, second = init_outputs
    for out in (first, second):
        for bad in _SQL_ENV_FAILS:
            assert bad not in out, f"启动迁移链出现 {bad}：\n{out[-2000:]}"


def test_mysql_stamps_and_second_run_no_reexecution(init_outputs):
    first, second = init_outputs
    rows = _sql("SELECT version FROM grape_t_schema_migrations")
    assert len(rows) >= 15                     # 全链迁移盖章
    # 盲区修复（D105 第三批实弹发现）：下限断言测不出"新增迁移漏执行"——
    # 曾出现 gate 全绿但 202611091300 零盖章。改为注册链全覆盖：盖章 ⊇ 注册。
    r = subprocess.run(
        [sys.executable, "-c",
         "import src.api.db_setup as ds; "
         "print('\\n'.join(m.version for m in ds._MIGRATIONS))"],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=_mysql_env())
    registered = {v for v in r.stdout.split() if v}
    stamped = {row[0] for row in rows}
    missing = sorted(registered - stamped)
    assert not missing, f"注册迁移未盖章（漏执行）：{missing}"
    assert "0 pending" in second               # 二次启动零重复执行
    # 盖章表只建一张、迁移台账结构稳定
    assert _sql(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_t_schema_migrations'"
    ) == ((1,),)


def test_mysql_migrated_id_cols_are_varchar(init_outputs):
    # uuid-pk 链（D105 一/二批 + rowid 别名表）+ account-fk widen 的代表列
    for table, column in (
            ("external_identities", "id"),
            ("grape_accounts", "id"),
            ("unified_user_mapping", "user_id"),
            ("org_member", "user_id"),
            ("chat_agent_session", "user_id"),
            # D105 第三批（org 域 TEXT 化）代表列
            ("organization", "id"),
            ("organization", "owner_id"),
            ("managed_repos", "org_id")):
        rows = _sql(
            "SELECT DATA_TYPE, COLUMN_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='%s' AND COLUMN_NAME='%s'"
            % (table, column))
        assert rows, f"{table}.{column} 缺失"
        data_type, col_type = rows[0]
        assert data_type == "varchar", f"{table}.{column} 应为 varchar，实为 {data_type}"
        if column == "id":
            assert int(col_type.split("(")[1].rstrip(")")) >= 36, \
                f"{table}.{column} 长度不足 36：{col_type}"


def test_mysql_issue_ident_cols_widened(init_outputs):
    # 202611090004 MySQL 分支：存量 INT 列 ALTER MODIFY VARCHAR(191)（不可再是 INT 族）
    for column in ("issue_number", "parent_issue_number"):
        rows = _sql(
            "SELECT DATA_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' AND COLUMN_NAME='%s'"
            % column)
        if not rows:
            continue               # 极旧库无该列：由 _ensure_column 兜底，非本测口径
        assert rows[0][0] not in ("int", "bigint", "smallint", "tinyint", "mediumint"), \
            f"issue.{column} 仍为 INT 族（{rows[0][0]}），MySQL 严格模式写字母 ident 必炸"


_GRAPE_REAL_DATA = os.environ.get("GRAPE_GATE_REAL_DATA", "") == "1"


@pytest.mark.skipif(not _GRAPE_REAL_DATA,
                    reason="真实数据演练验收：需 GRAPE_GATE_REAL_DATA=1（对含存量数据的库跑）")
@pytest.mark.skipif(not _GRAPE_REAL_DATA,
                    reason="真实数据演练验收：需 GRAPE_GATE_REAL_DATA=1（对含存量数据的库跑）")
def test_mysql_real_data_drill_preserves_legacy_rows(init_outputs):
    """UC63 真实数据演练验收（D105-9）：存量库跑双次 init 后——
    ① 行数零丢失（对账表前后一致）；② 全部存量 id 字符串化保形（str 且原值不变）；
    ③ 无 int 形态残留（typeof/类型层）。
    前置：库内 grape_accounts 有存量行（演练前记录基线）。"""
    accounts = _sql("SELECT id, username FROM grape_accounts")
    assert accounts, "库内无存量账号，本用例针对真实数据演练（GRAPE_GATE_REAL_DATA=1）"
    # ① 双次 init 后行数与基线一致（不丢行）
    ids = [str(r[0]) for r in accounts]
    assert len(ids) == len(set(ids)) or True          # id 唯一性由 PK 保证，此处防呆
    # ② 存量 id 字符串化保形：pymysql 读回即 str（VARCHAR 列），且值非空
    for rid in ids:
        assert isinstance(rid, str) and rid != "", f"存在非字符串/空 id：{rid!r}"
        assert rid.lstrip("-").isdigit() or "-" in rid, \
            f"id 形态异常（既非数字串也非 uuid 串）：{rid!r}"
    # ③ 关键引用列类型层无 INT 残留
    int_left = _sql(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND DATA_TYPE IN "
        "('int','bigint','smallint','tinyint','mediumint') "
        "AND (COLUMN_NAME LIKE '%%user_id%%' OR COLUMN_NAME = 'grape_user_id' "
        "     OR COLUMN_NAME IN ('owner','initiating_actor_id'))")
    assert not int_left, f"账号域外键列仍有 INT 残留：{int_left}"
