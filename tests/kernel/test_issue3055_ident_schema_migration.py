"""Issue #3055 B 项：注册迁移 202611172020 ident 列宽度 + 键空间收敛。

锁的契约（docs/weekly-11/issue-3055-issue-ident-hardening.md §2.3 / §3 / §4-B）：
1. `issue.issue_number = 0`（PR 卡片/无单号占位）→ `pr:<主键 id>`，幂等；
2. `chat_conversations.issue_number = 0` 是通用会话合法哨兵，**不动**；
3. `(repo_key, issue_number)` 唯一索引必须真的存在（历史代码 `CREATE UNIQUE
   INDEX IF NOT EXISTS` 在同名普通索引 + 重复 0 行时静默缺失）；
4. 索引建不起来（重复非 0 键）必须 raise → 执行器不盖章、下次启动重试；
5. SQLite 列类型不动：INTEGER 亲和是有意契约（#995/#2072）；
6. 三库边界：issue/chat_conversations 归 runtime，非 owner 域（不在本库/是视图）
   抛 `_NonOwnerObject` 跳过且不盖章，不得 log 后正常返回。

实现位于 src/domains/runtime/migrations/issue_ident_columns.py（db_setup.py 只留
`register_migration` 接线），本文用注入的临时 sqlite 连接直接驱动迁移类。
"""
import sqlite3

import pytest

from src.api.db_setup import _fn_issue3055_ident_schema
from src.domains.runtime.migrations.issue_ident_columns import (
    CHAT_TABLE,
    ISSUE_TABLE,
    MIGRATION_NAME,
    MIGRATION_VERSION,
    UNIQUE_INDEX,
    IssueIdentColumnsMigration,
)


def _run(conn):
    return IssueIdentColumnsMigration(conn=conn).run()


def _non_owner_cls():
    """取**当前** db_setup 模块里的 _NonOwnerObject。

    其它用例会 importlib.reload(db_setup)（如 test_issue_number_text_migration.py），
    reload 会重建类对象；模块级 import 的类与运行期 `ds._NonOwnerObject` 就不是
    同一个类，pytest.raises 会漏接。故按调用时取。
    """
    from src.api import db_setup

    return db_setup._NonOwnerObject


def _make_db(tmp_path, name="issue3055.db", with_chat=True):
    """存量库形态：issue/chat_conversations 均 INTEGER 亲和，无唯一索引。"""
    db = sqlite3.connect(str(tmp_path / name))
    c = db.cursor()
    c.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "user_id TEXT NOT NULL DEFAULT '', repo_key TEXT NOT NULL, "
              "issue_number INTEGER NOT NULL, title TEXT DEFAULT '')")
    if with_chat:
        c.execute("CREATE TABLE chat_conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "user_id INTEGER NOT NULL DEFAULT 0, repo_key TEXT DEFAULT '', "
                  "issue_number INTEGER DEFAULT 0, conv_type TEXT DEFAULT 'general')")
        # 通用会话哨兵 0 + 已绑 ident 的任务会话
        c.execute("INSERT INTO chat_conversations (user_id, repo_key, issue_number, conv_type) "
                  "VALUES (1, '', 0, 'general')")
        c.execute("INSERT INTO chat_conversations (user_id, repo_key, issue_number, conv_type) "
                  "VALUES (1, 'gitee:o/r', 0, 'task')")
    # 同仓多行 0（PR 卡片）——历史 1062 静默缺失的直接成因
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 0)")
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 0)")
    # 跨仓另一行 0（PR 卡片）
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitcode:o/r', 0)")
    # 真实 ident 单号（不得被改写）
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 'IKW28R')")
    # 数字单号（读侧恒 int 契约，不得被改写）
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 28)")
    db.commit()
    return db


def _issue_rows(c):
    return dict(c.execute("SELECT id, issue_number FROM issue").fetchall())


def _index_sql(c):
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                    (UNIQUE_INDEX,)).fetchone()
    return (row[0] or "") if row else ""


class TestZeroRowsRewrite:
    def test_zero_issue_rows_become_pr_ident(self, tmp_path):
        db = _make_db(tmp_path)
        c = db.cursor()
        report = _run(db)
        assert report["backend"] == "sqlite"
        assert report["rewritten"] == 3
        rows = _issue_rows(c)
        # 每行 0 → pr_<主键 id>（前缀与全仓 startswith("pr_") 判定一致），互不冲突
        assert rows[1] == "pr_1" and rows[2] == "pr_2" and rows[3] == "pr_3"
        # 真实 ident / 数字单号不动（ident 不得被 "= 0" 类谓词误伤）
        assert rows[4] == "IKW28R"
        assert rows[5] == 28
        assert c.execute("SELECT COUNT(*) FROM issue WHERE issue_number = 0").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM issue WHERE issue_number = '0'").fetchone()[0] == 0

    def test_chat_conversations_sentinel_untouched(self, tmp_path):
        db = _make_db(tmp_path)
        c = db.cursor()
        _run(db)
        got = c.execute("SELECT issue_number, typeof(issue_number), conv_type "
                        "FROM chat_conversations ORDER BY id").fetchall()
        # 通用会话/任务会话的 0 是合法哨兵：值、类型、行数全部不动
        assert got == [(0, "integer", "general"), (0, "integer", "task")]

    def test_sqlite_affinity_contract_untouched(self, tmp_path):
        """SQLite 列声明必须还是 INTEGER（#995/#2072 契约：数字读侧恒 int）。"""
        db = _make_db(tmp_path)
        c = db.cursor()
        _run(db)
        decl = next(r[2] for r in c.execute("PRAGMA table_info(issue)").fetchall()
                    if r[1] == "issue_number")
        assert decl.strip().upper() == "INTEGER"
        assert c.execute("SELECT typeof(issue_number) FROM issue WHERE id=5").fetchone()[0] == "integer"


class TestUniqueIndex:
    def test_unique_index_created_when_missing(self, tmp_path):
        db = _make_db(tmp_path)
        c = db.cursor()
        report = _run(db)
        assert report["index"] == "created"
        assert "UNIQUE" in _index_sql(c).upper()
        # 语义生效：同仓同号第二行被拒
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 'IKW28R')")
        db.rollback()

    def test_plain_index_upgraded_to_unique(self, tmp_path):
        db = _make_db(tmp_path)
        c = db.cursor()
        # 历史遗留：同名**普通**索引（MySQL 兼容层见同名即跳过 → 唯一性静默缺失）
        c.execute(f"CREATE INDEX {UNIQUE_INDEX} ON issue(repo_key, issue_number)")
        db.commit()
        report = _run(db)
        assert report["index"] == "created"
        assert "UNIQUE" in _index_sql(c).upper()

    def test_existing_unique_index_is_noop(self, tmp_path):
        db = _make_db(tmp_path)
        _run(db)                       # 先清 0 行并建唯一索引
        report = _run(db)
        assert report["index"] == "exists"
        assert report["rewritten"] == 0


class TestIdempotency:
    def test_second_run_zero_writes(self, tmp_path):
        db = _make_db(tmp_path)
        _run(db)
        before = db.total_changes
        snapshot = _issue_rows(db.cursor())
        report2 = _run(db)
        assert report2["rewritten"] == 0
        assert report2["index"] == "exists"
        assert db.total_changes == before, "二次执行必须零写入"
        assert _issue_rows(db.cursor()) == snapshot

    def test_already_pr_rows_skipped(self, tmp_path):
        """已是 pr_ 前缀的行不重复改写（幂等口径显式锁）。"""
        db = _make_db(tmp_path)
        c = db.cursor()
        c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 'pr:99')")
        db.commit()
        row_id = c.execute("SELECT id FROM issue WHERE repo_key='gitee:o/r'").fetchone()[0]
        _run(db)
        # 历史 pr: 前缀一次性归一为 pr_<该行 id>，库里不残留冒号形态
        val = c.execute("SELECT issue_number FROM issue WHERE id=?", (row_id,)).fetchone()[0]
        assert str(val) == f"pr_{row_id}" and not str(val).startswith("pr:")

    def test_legacy_pr_colon_prefix_is_normalised(self, tmp_path):
        """早期迁移产物的 ``pr:<id>`` 一次性归一为 ``pr_<id>``（幂等）。"""
        db = _make_db(tmp_path)
        c = db.cursor()
        c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 'pr:1')")
        db.commit()
        _run(db)
        assert c.execute("SELECT COUNT(*) FROM issue WHERE issue_number = 'pr:1'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM issue WHERE issue_number = 'pr_1'").fetchone()[0] == 1


class TestFailureRaises:
    def test_duplicate_nonzero_keys_raise(self, tmp_path):
        """重复非 0 键（真脏数据）→ 唯一索引建不起来必须 raise，不得静默放过。"""
        db = _make_db(tmp_path)
        c = db.cursor()
        c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitee:o/r', 28)")
        db.commit()
        with pytest.raises(RuntimeError) as ei:
            _run(db)
        assert UNIQUE_INDEX in str(ei.value)
        # raise 不等于「假装成功」：索引确实没建成（不盖章 → 下次启动重试）
        assert "UNIQUE" not in _index_sql(c).upper()


class TestOwnerDomainGuard:
    """三库边界：issue/chat_conversations 归 runtime，非 owner 域必须跳过不盖章。"""

    def test_missing_issue_table_is_non_owner(self, tmp_path):
        db = sqlite3.connect(str(tmp_path / "empty.db"))
        with pytest.raises(_non_owner_cls()):
            _run(db)

    def test_missing_chat_conversations_is_non_owner(self, tmp_path):
        """chat_conversations 不在本库 → 同样 _NonOwnerObject（不得 log 后正常返回）。"""
        db = _make_db(tmp_path, name="no_chat.db", with_chat=False)
        with pytest.raises(_non_owner_cls()) as ei:
            _run(db)
        assert "chat_conversations" in str(ei.value)


# ── MySQL 分支：本地无 MySQL 时用脚本化假游标验证 DDL 口径与步骤顺序 ────────
class _FakeCursor:
    """按 SQL 片段分派的假游标（记录全部语句，便于断言顺序与形态）。"""

    def __init__(self, resolver):
        self._resolver = resolver
        self.statements = []
        self._rows = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._rows = self._resolver(sql, params, self) or []
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def executed(self, prefix):
        return [s for s, _ in self.statements if s.startswith(prefix)]


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _mysql_resolver(post_int_col=None, base_table_type="BASE TABLE"):
    """Default MySQL shape: 三列 INT 族 + issue 无唯一索引 + 两行 0 号行。"""

    def resolve(sql, params, cursor):
        if sql.startswith("SELECT 1 FROM information_schema.tables"):
            return [(1,)]
        if sql.startswith("SELECT table_type FROM information_schema.tables"):
            return [(base_table_type,)]
        if sql.startswith("SELECT TABLE_TYPE FROM information_schema.TABLES"):
            return [(base_table_type,)]
        if sql.startswith("SELECT DATABASE()"):
            return [("grape_runtime_db",)]
        if sql.startswith("SELECT IS_NULLABLE, COLUMN_DEFAULT"):
            # issue_number/parent_issue_number: NOT NULL 无默认；chat: NOT NULL DEFAULT 0
            if params[1] == CHAT_TABLE:
                return [("NO", "0")]
            return [("NO", None if params[2] != "parent_issue_number" else "0")]
        if sql.startswith(f"SELECT id, issue_number FROM {ISSUE_TABLE}"):
            return [(11, "0"), (12, "0")]
        if sql.startswith("SELECT NON_UNIQUE FROM information_schema.statistics"):
            created = bool(cursor.executed(f"CREATE UNIQUE INDEX {UNIQUE_INDEX}"))
            return [(0,)] if created else []
        if sql.startswith("SELECT DATA_TYPE FROM information_schema.columns"):
            col = params[2]
            if col == post_int_col:
                return [("int",)]
            return [("text",)] if col == "repo_key" else [("varchar",)]
        return []

    return resolve


class TestMysqlBranch:
    """MySQL 侧口径：先放宽列（否则 'pr:<id>' 写 INT 列仍截断）→ 改写 0 号行 →
    建唯一索引（显式 DROP 同名普通索引）→ information_schema 后验。"""

    def test_widen_rewrite_index_order_and_shape(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
        cursor = _FakeCursor(_mysql_resolver())
        report = _run(_FakeConn(cursor))
        assert report["backend"] == "mysql"
        assert sorted(report["widened"]) == [
            "chat_conversations.issue_number",
            "issue.issue_number",
            "issue.parent_issue_number",
        ]
        assert report["rewritten"] == 2
        assert report["index"] == "created"

        stmts = [s for s, _ in cursor.statements]
        first_alter = next(i for i, s in enumerate(stmts) if s.startswith("ALTER TABLE"))
        first_update = next(i for i, s in enumerate(stmts) if s.startswith(f"UPDATE {ISSUE_TABLE}"))
        create_idx = next(i for i, s in enumerate(stmts) if s.startswith("CREATE UNIQUE INDEX"))
        # 顺序必须 放宽列 → 改写 0 号行 → 建索引
        assert first_alter < first_update < create_idx, stmts

        alters = cursor.executed("ALTER TABLE")
        # 列宽语句显式标 COPY + LOCK=SHARED：INT→VARCHAR 是类型转换，MySQL 只支持 COPY，
        # 写 INPLACE 会报 1846 让迁移失败（评审建议过一次，这里用断言锁住不许改回去）
        # 逗号不能少：MySQL 里 `MODIFY ... VARCHAR(191) NOT NULL, ALGORITHM=COPY`
        # 才是合法语法（漏逗号是 1064，这条迁移此前从未在 MySQL 落过库）
        assert any(s.endswith("MODIFY `issue_number` VARCHAR(191) NOT NULL, "
                              "ALGORITHM=COPY, LOCK=SHARED") for s in alters), alters
        assert not any("ALGORITHM=INPLACE" in s for s in alters), alters
        # chat_conversations.issue_number 的 DEFAULT 0 语义保留
        assert any("`chat_conversations` MODIFY `issue_number` VARCHAR(191) NOT NULL "
                   "DEFAULT '0', ALGORITHM=COPY" in s for s in alters), alters
        # repo_key 是 TEXT → 唯一索引带前缀长度；issue_number 已 VARCHAR 无需前缀
        create = cursor.executed(f"CREATE UNIQUE INDEX {UNIQUE_INDEX}")[0]
        assert "`repo_key`(191)" in create and "`issue_number`" in create, create
        # 后验：三列各查一次 DATA_TYPE
        assert len(cursor.executed("SELECT DATA_TYPE FROM information_schema.columns")) >= 3

    def test_postcheck_raises_when_column_still_int(self, monkeypatch):
        """MODIFY 未生效（无 ALTER 权限等）→ 后验必须 raise，不盖章。"""
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
        cursor = _FakeCursor(_mysql_resolver(post_int_col="issue_number"))
        with pytest.raises(RuntimeError) as ei:
            _run(_FakeConn(cursor))
        assert "仍为 int" in str(ei.value)

    def test_view_is_non_owner(self, monkeypatch):
        """非 owner 域里 issue 是跨库视图 → _NonOwnerObject（不盖章），不做任何 DDL。"""
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
        cursor = _FakeCursor(_mysql_resolver(base_table_type="VIEW"))
        with pytest.raises(_non_owner_cls()):
            _run(_FakeConn(cursor))
        assert not cursor.executed("ALTER TABLE")
        assert not cursor.executed("CREATE UNIQUE INDEX")

    def test_existing_unique_index_and_pr_rows_are_noop(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")

        def resolver(sql, params, cursor):
            if sql.startswith("SELECT IS_NULLABLE, COLUMN_DEFAULT"):
                return []            # 三列已是 VARCHAR → 零 ALTER
            if sql.startswith(f"SELECT id, issue_number FROM {ISSUE_TABLE}"):
                return [(21, "pr_21")]   # 已是 pr_ 前缀 → 零改写零归一
            if sql.startswith("SELECT NON_UNIQUE FROM information_schema.statistics"):
                return [(0,)]        # 已是唯一索引
            return _mysql_resolver()(sql, params, cursor)

        cursor = _FakeCursor(resolver)
        report = _run(_FakeConn(cursor))
        assert report["widened"] == []
        assert report["rewritten"] == 0
        assert report["index"] == "exists"
        assert not cursor.executed("ALTER TABLE")
        assert not cursor.executed("UPDATE issue")
        assert not cursor.executed("CREATE UNIQUE INDEX")


class TestRegistration:
    def test_registered_once_with_backfill_kind(self):
        # 取当前 sys.modules 中的模块对象（其它用例可能 reload 过 db_setup）
        from src.api import db_setup as ds

        current_fn = ds._fn_issue3055_ident_schema
        assert _fn_issue3055_ident_schema.__name__ == current_fn.__name__
        regs = [m for m in ds._MIGRATIONS if m.version == MIGRATION_VERSION]
        assert len(regs) == 1, f"版本号必须恰好注册一次: {[m.name for m in regs]}"
        mig = regs[0]
        assert mig.name == MIGRATION_NAME == "issue3055-issue-ident-schema"
        # kind=backfill：本迁移 UPDATE 存量 issue 行，不是纯 DDL（Issue #3054 QA）
        assert mig.kind == "backfill"
        assert mig.fn is current_fn
        # 号不撞：本链唯一且为最大号（新增迁移顺延，不复用已盖章号）
        versions = [m.version for m in ds._MIGRATIONS]
        assert versions.count(MIGRATION_VERSION) == 1
        assert max(versions) >= MIGRATION_VERSION   # 允许后续迁移顺延到更大号

    def test_version_literal_matches_module_constant(self):
        """db_setup 的 register_migration 版本号是字面量（AST 守卫要求），
        必须与实现模块的常量一致，防两处漂移。"""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "db_setup.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "register_migration"):
                kw = {k.arg: k.value for k in node.keywords}
                if "fn" in kw and getattr(kw["fn"], "id", "") == "_fn_issue3055_ident_schema":
                    found.append((ast.literal_eval(kw["version"]), ast.literal_eval(kw["name"])))
        assert found == [(MIGRATION_VERSION, MIGRATION_NAME)], f"注册字面量与实现常量漂移: {found}"