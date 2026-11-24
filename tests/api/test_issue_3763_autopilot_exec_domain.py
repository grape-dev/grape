"""Issue #3763：三库拓扑下 autopilot_executions 只落 owner 域（runtime）。

覆盖：
- table_ownership 中该表归属 runtime，迁移只登记 runtime 域；
- 迁移在建表侧幂等补建（CREATE IF NOT EXISTS + 补列/索引），存量库重跑零写入；
- mgmt 域源码无直接 INSERT/UPDATE/DELETE 该表，跨域写只经 runtime.public 原语
  且原语在自有 DSN 拓扑下取 runtime 域连接。
"""
import contextlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 复审 R1：版本号按目标分支顺延（202611220001 已被 chat-conversations-status-index 占用）；
# 复审 R2：202611230003 又与基线 #3800（autopilot-task-failure-state-columns）撞号，
# 顺延至 202611230006（202611230004 已用、202611230005 被 #1981/#3794 预定）。
# 用例一律按 name 定位迁移，版本号变更只改这一处常量。
MIGRATION_NAME = "create-autopilot-executions-table"
MIGRATION_VERSION = "202611230006"


def _migration():
    """按 name 定位迁移（不按 version，避免换号后用例定位失败）。"""
    from src.api import db_setup

    found = [m for m in db_setup._MIGRATIONS if m.name == MIGRATION_NAME]
    assert len(found) == 1, f"迁移 {MIGRATION_NAME} 应恰好登记一次，实际 {len(found)} 次"
    return found[0]


def _ownership() -> dict:
    return json.loads(
        (ROOT / "configs" / "table_ownership.json").read_text(encoding="utf-8")
    )["tables"]


def test_autopilot_executions_owned_by_runtime():
    assert _ownership().get("autopilot_executions") == "runtime"


def test_migration_registered_for_runtime_domain_only():
    from src.api import db_setup

    mig = _migration()
    assert mig.name == MIGRATION_NAME
    assert mig.version == MIGRATION_VERSION, (
        f"迁移版本号应为 {MIGRATION_VERSION}（202611230003 已被基线 #3800 占用），实际 {mig.version}")
    assert mig.domains == ("runtime",)
    assert "CREATE TABLE IF NOT EXISTS autopilot_executions" in db_setup._AUTOPILOT_EXECUTIONS_DDL
    body = __import__("inspect").getsource(mig.fn)
    assert '_migration_conn("runtime")' in body
    assert 'CREATE INDEX IF NOT EXISTS idx_autopilot_exec_task' in body


def test_migration_is_idempotent_on_sqlite(monkeypatch):
    from src.api import db_setup

    conn = sqlite3.connect(":memory:")
    try:
        monkeypatch.setattr(db_setup, "_migration_conn",
                            lambda domain, default=None: conn)
        monkeypatch.setattr(db_setup, "db", conn)

        mig = _migration()
        mig.fn()
        mig.fn()  # 重跑零写入、不抛错

        c = conn.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(autopilot_executions)").fetchall()}
        assert {"agent_task_id", "params_json", "notified", "findings_json"} <= cols
        idx = {r[1] for r in c.execute("PRAGMA index_list(autopilot_executions)").fetchall()}
        assert {"idx_autopilot_exec_task", "idx_autopilot_exec_status",
                "idx_ap_exec_agent_task"} <= idx
    finally:
        conn.close()


def test_mgmt_domain_has_no_direct_autopilot_executions_writes():
    write_re = re.compile(
        r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`?autopilot_executions`?", re.I)
    hits = []
    for p in (ROOT / "src" / "domains" / "mgmt").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        if write_re.search(p.read_text(encoding="utf-8", errors="ignore")):
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, f"mgmt 域仍直接写 runtime 域表 autopilot_executions: {hits}"


def test_mgmt_cross_domain_write_routes_to_runtime_connection(monkeypatch):
    from src.domains.runtime import public as rt
    from src.kernel import db_registry

    seen = []

    class _Cur:
        rowcount = 1

        def execute(self, sql, args=None):
            seen.append((" ".join(sql.split()), args))
            return self

        def close(self):
            pass

    @contextlib.contextmanager
    def _cursor(domain):
        seen.append(("DOMAIN", domain))
        yield _Cur()

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda _d: True)
    monkeypatch.setattr(db_registry, "cursor", _cursor)

    assert rt.delete_autopilot_executions_by_task_any(42) == 1
    assert ("DOMAIN", "runtime") in seen, f"跨域写未走 runtime 域连接: {seen}"
    assert any(
        sql.startswith("DELETE FROM autopilot_executions") and args == (42,)
        for sql, args in (s for s in seen if s[0] != "DOMAIN"))

class _MySQLFakeCursor:
    """模拟 MySQL 游标：表已存在但缺列；TEXT 族带 DEFAULT 的补列按 1845 拒绝。"""

    def __init__(self, existing_cols):
        self.existing_cols = list(existing_cols)
        self.statements = []
        self.succeeded = []
        self._rows = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT 1 FROM INFORMATION_SCHEMA.TABLES"):
            self._rows = [(1,)]
        elif upper.startswith("PRAGMA TABLE_INFO"):
            self._rows = [
                (i, name, "TEXT", 0, None, 0)
                for i, name in enumerate(self.existing_cols)
            ]
        elif upper.startswith("SELECT TABLE_TYPE"):
            self._rows = [("BASE TABLE",)]
        elif upper.startswith("ALTER TABLE") and re.search(
                r"(?:MEDIUM|LONG)?TEXT\s+DEFAULT\s+''", sql, re.I):
            # MySQL 8.0：TEXT 族带 DEFAULT 的列 INSTANT/INPLACE 均不支持（1845）
            raise RuntimeError(
                "(1845) ALGORITHM=INSTANT is not supported. Reason: DEFAULT is not allowed")
        else:
            self._rows = []
        self.succeeded.append(sql)
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass

    def alt(self, only_succeeded=False):
        src = self.succeeded if only_succeeded else [s for s, _ in self.statements]
        return [s for s in src if " ".join(s.split()).upper().startswith("ALTER TABLE")]


def test_migration_adds_missing_columns_on_mysql_without_recreating_table(monkeypatch):
    """存量 MySQL「表在但缺列」：只补列、不重建表，且按 MySQL 方言/翻译补列。"""
    from src.api import db_setup

    cur = _MySQLFakeCursor(existing_cols=("id", "task_id", "status"))

    class _FakeConn:
        def cursor(self):
            return cur

        def commit(self):
            pass

    conn = _FakeConn()
    monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
    monkeypatch.setattr(db_setup, "_migration_conn", lambda domain, default=None: conn)
    monkeypatch.setattr(db_setup, "db", conn)

    mig = _migration()
    mig.fn()

    # 表已存在 → 不得再执行建表，只走补列路径
    assert not any("CREATE TABLE" in s.upper() for s, _ in cur.statements), cur.statements
    attempts = cur.alt()
    for col in ("agent_type", "findings_json", "agent_task_id", "params_json", "notified"):
        assert any(f"ADD COLUMN `{col}`" in s for s in attempts), (col, attempts)

    # MySQL 方言：补列必须带 ALGORITHM 子句，而非 SQLite 裸 ADD COLUMN
    assert all("ALGORITHM=" in s for s in attempts), attempts

    # 翻译兼容：TEXT 族 DEFAULT '' 经剥离后重试成功（不落人工清单/不抛错）
    ok = cur.alt(only_succeeded=True)
    for col in ("agent_type", "findings_json", "params_json"):
        assert any(f"`{col}`" in s and "DEFAULT" not in s for s in ok), (col, ok)
    # DEFAULT NULL 的可空 TEXT 列可走 INSTANT（隐式默认等价）直接补列
    assert any("`agent_task_id`" in s for s in ok), ok

    # 幂等索引语句也要执行（CREATE INDEX IF NOT EXISTS 由 mysql_compat 翻译）
    assert any("CREATE INDEX IF NOT EXISTS idx_autopilot_exec_task" in s
               for s in cur.succeeded), cur.succeeded
