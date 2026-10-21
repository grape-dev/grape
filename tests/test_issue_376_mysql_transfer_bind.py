"""Issue #376 回归测试：transfer-bind MySQL 8.0 兼容 + 票据失败重试。

覆盖验收标准：
1. transfer_business_data / latency 清理 SQL 无「UPDATE/DELETE ... [NOT] IN (SELECT ... FROM 同表)」直接写法
   （派生表包裹，SQLite/MySQL 双兼容，消除 MySQL 1093）。
2. 派生表包裹 SQL 在 SQLite 真实执行，语义与原写法一致（user_repos 冲突合并）。
3. validate_binding_ticket 拆分校验/消费两步：consume=False 只校验不删；
   consume_binding_ticket 在业务事务成功后消费。
4. transfer-bind 失败（500）后同票据有效期内重试不再 410（票据不被连带作废）。
5. 翻译层断言：两条迁移 SQL 经 translate_sql 后不再以同表子查询形式直达 MySQL。
"""
import os
import re
import sqlite3
import time
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")


# ===========================================================================
# 1. SQL 文本断言：派生表包裹（验收标准 3）
# ===========================================================================

class TestDerivedTableSQLText:
    """源码中的 SQL 必须是派生表包裹写法（MySQL 1093 消除）。

    断言对象是源码文本中隐式拼接的 SQL 字面量（先把相邻 f-string 还原成
    单条 SQL 文本再匹配），避免逐字符匹配 f-string 引号前缀的脆弱性。
    """

    def _read(self, rel):
        with open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8") as f:
            return f.read()

    def _sql_literals(self, src):
        """提取源码中的字符串字面量并还原为内容（容忍 f 前缀与隐式拼接）。"""
        import io as _io
        import tokenize as _tok

        out = []
        toks = list(_tok.generate_tokens(_io.StringIO(src).readline))
        buf = None
        for t in toks:
            if t.type == _tok.STRING:
                m = re.match(r"^[a-zA-Z]*('''|\"\"\"|'|\")((?:\\.|(?!\1).)*)\1", t.string, re.S)
                if m:
                    buf = (buf or "") + m.group(2)
            elif t.type in (_tok.NL, _tok.NEWLINE, _tok.INDENT, _tok.DEDENT, _tok.COMMENT):
                continue
            else:
                if buf is not None:
                    out.append(" ".join(buf.split()))
                    buf = None
        if buf is not None:
            out.append(" ".join(buf.split()))
        return out

    def _assert_no_direct_same_table_subquery(self, src, table_pattern):
        """UPDATE/DELETE 目标表不得直接出现在 IN/NOT IN 子查询的 FROM 中。

        table_pattern 是正则片段（需同时覆盖 f-string 的 {table} 占位符与字面表名，
        如 r"(?:\\{table\\}|user_repos)"）。
        判定：IN (SELECT ... FROM <表名> 为直接形态（MySQL 1093）；
        FROM (SELECT ... FROM <表名> ...) 为派生表包裹形态（合法）。
        """
        tgt = re.compile(r"^\s*(?:UPDATE|DELETE\s+FROM)\s+" + table_pattern + r"(?![\w}])", re.I)
        finder = re.compile(
            r"(?:NOT\s+)?IN\s*\(\s*SELECT[^()]*?\bFROM\s+(?P<q>[`(]?\s*)(?P<tbl>" + table_pattern + r")(?![\w}])",
            re.I)
        checked = 0
        for lit in self._sql_literals(src):
            if not (tgt.search(lit) and "IN (" in lit):
                continue  # 只检查含 IN 子查询的语句
            checked += 1
            for m in finder.finditer(lit):
                if m.group("q").strip() == "(":
                    continue  # 派生表包裹形态，合法
                raise AssertionError(f"存在直接同表子查询（MySQL 1093 形态）: {lit[:140]}")
        assert checked >= 1, f"未找到 {table_pattern} 的 IN 子查询语句（断言失效）"

    def test_transfer_business_data_derived_table(self):
        """user_repos 两条迁移 SQL 均为派生表包裹。"""
        src = self._read("src/auth/grape_account_db.py")
        lits = self._sql_literals(src)
        joined = " || ".join(lits)
        assert "NOT IN (SELECT repo_key FROM (SELECT repo_key FROM" in joined, \
            "UPDATE 分支缺少派生表包裹"
        assert "repo_key IN (SELECT repo_key FROM (SELECT repo_key FROM" in joined, \
            "DELETE 分支缺少派生表包裹"
        # {table} 实例化后即 user_repos，两种形态都断言
        self._assert_no_direct_same_table_subquery(src, r"(?:\{table\}|user_repos)")

    def test_latency_cleanup_derived_table(self):
        """latency.py 超限清理 SQL 为派生表包裹。"""
        src = self._read("src/api/routes/latency.py")
        joined = " || ".join(self._sql_literals(src))
        assert "id IN (SELECT id FROM (SELECT id FROM api_latency_log" in joined, \
            "latency 清理缺少派生表包裹"
        self._assert_no_direct_same_table_subquery(src, "api_latency_log")

    def test_table_maintenance_derived_table(self):
        """table_maintenance.py SQLite 分支清理 SQL 为派生表包裹（同类排查命中）。"""
        src = self._read("src/runtime/table_maintenance.py")
        joined = " || ".join(self._sql_literals(src))
        assert "id IN (SELECT id FROM (SELECT id FROM task_messages" in joined
        self._assert_no_direct_same_table_subquery(src, "task_messages")


# ===========================================================================
# 2. 真实 SQLite 执行：user_repos 迁移语义不变
# ===========================================================================

class TestTransferUserReposSqlite:
    """派生表包裹 SQL 在 SQLite 上执行，语义与原写法一致。

    场景（占位账号有业务数据）：
    - from 用户持有 repo A、B（is_admin=1）
    - to 用户已持有 repo B
    - 迁移后：repo A 归 to；repo B 记录保留 to 的原行、from 的旧行被删
    """

    def _setup_db(self):
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        c.execute(
            "CREATE TABLE user_repos (user_id INTEGER, repo_key TEXT, is_admin INTEGER, "
            "PRIMARY KEY (user_id, repo_key))"
        )
        c.executemany(
            "INSERT INTO user_repos (user_id, repo_key, is_admin) VALUES (?,?,?)",
            [(1, "gitcode:o/r-a", 1), (1, "gitcode:o/r-b", 1), (2, "gitcode:o/r-b", 1)],
        )
        conn.commit()
        return conn

    def _run_transfer_sqls(self, conn, table="user_repos", col="user_id"):
        c = conn.cursor()
        to_user_id, from_user_id = 2, 1
        c.execute(
            f"UPDATE {table} SET {col}=? "
            f"WHERE {col}=? AND repo_key NOT IN "
            f"(SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)",
            (to_user_id, from_user_id, to_user_id),
        )
        c.execute(
            f"DELETE FROM {table} WHERE {col}=? AND repo_key IN "
            f"(SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)",
            (from_user_id, to_user_id),
        )
        conn.commit()

    def test_transfer_semantics_sqlite(self):
        conn = self._setup_db()
        self._run_transfer_sqls(conn)
        rows = sorted(conn.execute(
            "SELECT user_id, repo_key FROM user_repos").fetchall())
        # repo A 转给 to；repo B 只剩 to 的原行
        assert rows == [(2, "gitcode:o/r-a"), (2, "gitcode:o/r-b")], rows
        conn.close()

    def test_transfer_no_conflict_all_moved(self):
        conn = self._setup_db()
        # 删掉 to 用户的 repo B，制造无冲突场景
        conn.execute("DELETE FROM user_repos WHERE user_id=2 AND repo_key='gitcode:o/r-b'")
        conn.commit()
        self._run_transfer_sqls(conn)
        rows = sorted(conn.execute(
            "SELECT user_id, repo_key FROM user_repos").fetchall())
        assert rows == [(2, "gitcode:o/r-a"), (2, "gitcode:o/r-b")], rows
        conn.close()

    def test_transfer_empty_source_noop(self):
        conn = self._setup_db()
        conn.execute("DELETE FROM user_repos WHERE user_id=1")
        conn.commit()
        self._run_transfer_sqls(conn)
        rows = conn.execute(
            "SELECT user_id, repo_key FROM user_repos").fetchall()
        assert rows == [(2, "gitcode:o/r-b")], rows
        conn.close()

    def test_latency_trim_sqlite(self):
        """latency 超限清理 SQL 在 SQLite 上语义正确（删最旧 excess 条）。"""
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        c.execute("CREATE TABLE api_latency_log (id INTEGER PRIMARY KEY, created_at TEXT)")
        c.executemany(
            "INSERT INTO api_latency_log (created_at) VALUES (?)",
            [(f"2026-10-{d:02d} 00:00:00",) for d in range(1, 8)],
        )
        conn.commit()
        excess = 7 - 5
        c.execute(
            f"DELETE FROM api_latency_log WHERE id IN "
            f"(SELECT id FROM (SELECT id FROM api_latency_log ORDER BY id ASC LIMIT {excess}) t)"
        )
        conn.commit()
        ids = [r[0] for r in conn.execute("SELECT id FROM api_latency_log ORDER BY id")]
        assert ids == [3, 4, 5, 6, 7], ids
        conn.close()


# ===========================================================================
# 3. 票据两步语义：validate(consume=False) + consume_binding_ticket
# ===========================================================================

class TestTicketTwoPhase:
    """票据校验/消费拆分（真实 SQLite 测试库）。"""

    def _save_ticket(self, initiating_user_id=300, original_user_id=299):
        from src.auth.grape_account_db import save_binding_ticket
        ticket = save_binding_ticket(
            initiating_user_id=initiating_user_id,
            provider="gitcode",
            external_id="gc_issue376",
            external_login="user376",
            original_user_id=original_user_id,
        )
        assert ticket is not None
        return ticket

    def test_validate_without_consume_keeps_ticket(self):
        """consume=False 只校验不删——票据可再次校验。"""
        from src.auth.grape_account_db import validate_binding_ticket
        ticket = self._save_ticket()
        d1 = validate_binding_ticket(ticket, 300, consume=False)
        assert d1 is not None and d1["external_id"] == "gc_issue376"
        d2 = validate_binding_ticket(ticket, 300, consume=False)
        assert d2 is not None, "consume=False 不应删除票据"

    def test_consume_binding_ticket_deletes(self):
        """consume_binding_ticket 删除票据；再校验返回 None。"""
        from src.auth.grape_account_db import validate_binding_ticket, consume_binding_ticket
        ticket = self._save_ticket(initiating_user_id=301, original_user_id=299)
        assert validate_binding_ticket(ticket, 301, consume=False) is not None
        assert consume_binding_ticket(ticket) is True
        assert validate_binding_ticket(ticket, 301, consume=False) is None

    def test_consume_nonexistent_ticket_returns_false(self):
        """删除不存在的票据返回 False（幂等）。"""
        from src.auth.grape_account_db import consume_binding_ticket
        assert consume_binding_ticket("nonexistent_issue376") is False

    def test_validate_default_consume_still_works(self):
        """默认 consume=True 保持旧行为（查+删一次性消费）。"""
        from src.auth.grape_account_db import validate_binding_ticket
        ticket = self._save_ticket(initiating_user_id=302, original_user_id=299)
        assert validate_binding_ticket(ticket, 302) is not None
        assert validate_binding_ticket(ticket, 302) is None

    def test_validate_user_mismatch_no_consume(self):
        """用户不匹配返回 None 且不消费（重试方仍可用自己的会话）。"""
        from src.auth.grape_account_db import validate_binding_ticket
        ticket = self._save_ticket(initiating_user_id=303)
        assert validate_binding_ticket(ticket, 999, consume=False) is None
        assert validate_binding_ticket(ticket, 303, consume=False) is not None


# ===========================================================================
# 4. transfer-bind API：失败后重试不再 410（验收标准 2）
# ===========================================================================

_VALID_TICKET_DATA = {
    "provider": "gitcode",
    "external_id": "gc_123",
    "external_login": "testuser",
    "external_name": "Test",
    "external_avatar": "",
    "original_user_id": 100,
}


class TestTransferBindRetry:
    """失败 → 票据保留 → 重试成功（mock 业务函数，真实票据存 SQLite）。"""

    def _make_client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.routes.grape_auth import require_jwt_only
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM external_identities WHERE grape_user_id=400")
            c.execute("DELETE FROM grape_accounts WHERE id=400")
            db.commit()

        mock_user = {"id": 400, "username": "user_376", "role": "user"}
        app.dependency_overrides[require_jwt_only] = lambda: mock_user
        return TestClient(app)

    def _real_ticket(self):
        from src.auth.grape_account_db import save_binding_ticket
        ticket = save_binding_ticket(
            initiating_user_id=400,
            provider="gitcode",
            external_id="gc_123",
            original_user_id=100,
        )
        assert ticket is not None
        return ticket

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    @patch("src.api.routes.grape_auth.get_user_identities")
    @patch("src.api.routes.grape_auth.auth_audit")
    @patch("src.api.routes.grape_auth._clear_user_repos_cache")
    def test_failure_then_retry_same_ticket(
        self, mock_clear, mock_audit, mock_idents, mock_count, mock_has_pwd,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account,
    ):
        """首次 500（业务数据转移失败）→ 票据未被消费 → 同票据重试 200。

        Issue #376 验收标准 2：失败重试不再必然 410。
        """
        from src.auth.grape_account_db import validate_binding_ticket, consume_binding_ticket

        mock_get_account.return_value = {"email_is_placeholder": 1, "created_by_oauth": 1}
        mock_has_pwd.return_value = False
        mock_count.return_value = 1
        mock_idents.return_value = []
        mock_transfer_in_tx.return_value = True
        mock_audit.log_audit = MagicMock()
        mock_clear.return_value = None

        client = self._make_client()
        ticket = self._real_ticket()

        # 第一次：业务数据转移失败 → 500
        mock_transfer_biz.return_value = False
        resp1 = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": ticket, "confirm": True,
        })
        assert resp1.status_code == 500, resp1.text

        # 票据仍然有效（未被连带作废）
        still = validate_binding_ticket(ticket, 400, consume=False)
        assert still is not None, "失败路径票据被连带作废（Issue #376 未修复）"

        # 第二次：业务恢复 → 同票据重试 200
        mock_transfer_biz.return_value = True
        resp2 = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": ticket, "confirm": True,
        })
        assert resp2.status_code == 200, resp2.text
        assert "转移成功" in resp2.json()["message"]

        # 成功后票据被消费（一次性语义保留）
        assert validate_binding_ticket(ticket, 400, consume=False) is None

        from src.api.main import app
        app.dependency_overrides.clear()

    @patch("src.api.routes.grape_auth.get_grape_account_by_id")
    @patch("src.api.routes.grape_auth.transfer_business_data")
    @patch("src.api.routes.grape_auth.transfer_external_identity_in_tx")
    @patch("src.api.routes.grape_auth.has_password")
    @patch("src.api.routes.grape_auth.count_user_identities")
    @patch("src.api.routes.grape_auth.get_user_identities")
    @patch("src.api.routes.grape_auth.auth_audit")
    @patch("src.api.routes.grape_auth._clear_user_repos_cache")
    def test_success_consumes_ticket_no_replay(
        self, mock_clear, mock_audit, mock_idents, mock_count, mock_has_pwd,
        mock_transfer_in_tx, mock_transfer_biz, mock_get_account,
    ):
        """成功转移后票据被消费——同票据再次提交返回 410（防重放）。"""
        mock_get_account.return_value = {"email_is_placeholder": 1, "created_by_oauth": 1}
        mock_has_pwd.return_value = False
        mock_count.return_value = 1
        mock_idents.return_value = []
        mock_transfer_biz.return_value = True
        mock_transfer_in_tx.return_value = True
        mock_audit.log_audit = MagicMock()
        mock_clear.return_value = None

        client = self._make_client()
        ticket = self._real_ticket()

        resp1 = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": ticket, "confirm": True,
        })
        assert resp1.status_code == 200, resp1.text

        # 成功后同票据重放 → 410
        resp2 = client.post("/api/auth/transfer-bind", json={
            "binding_ticket": ticket, "confirm": True,
        })
        assert resp2.status_code == 410, resp2.text

        from src.api.main import app
        app.dependency_overrides.clear()


# ===========================================================================
# 5. 翻译层断言：派生表 SQL 直达 MySQL 时不再触发 1093 形态
# ===========================================================================

class TestTranslateSqlNo1093:
    """迁移 SQL 经 translate_sql 后无「目标表直现在子查询 FROM」形态。"""

    def _assert_no_direct_same_table(self, sql, tbl):
        import src.mysql_compat as mc
        out = mc.translate_sql(sql)
        # 翻译后目标表只允许出现在派生表内部（SELECT * FROM ( ... tbl ... ) AS __derived
        # 或手写派生表），不允许「IN (SELECT ... FROM tbl」直接形态
        direct = re.search(
            rf"IN\s*\(\s*SELECT[^()]*?\bFROM\s+{re.escape(tbl)}\b", out, re.I)
        assert direct is None, f"翻译后仍有同表子查询直接形态: {out}"

    def test_transfer_update_sql_translated(self):
        sql = (
            "UPDATE user_repos SET user_id=? "
            "WHERE user_id=? AND repo_key NOT IN "
            "(SELECT repo_key FROM (SELECT repo_key FROM user_repos WHERE user_id=?) t)"
        )
        self._assert_no_direct_same_table(sql, "user_repos")

    def test_transfer_delete_sql_translated(self):
        sql = (
            "DELETE FROM user_repos WHERE user_id=? AND repo_key IN "
            "(SELECT repo_key FROM (SELECT repo_key FROM user_repos WHERE user_id=?) t)"
        )
        self._assert_no_direct_same_table(sql, "user_repos")

    def test_latency_trim_sql_translated(self):
        sql = (
            "DELETE FROM api_latency_log WHERE id IN "
            "(SELECT id FROM (SELECT id FROM api_latency_log ORDER BY id ASC LIMIT 5) t)"
        )
        self._assert_no_direct_same_table(sql, "api_latency_log")

    def test_placeholder_translation_unchanged(self):
        """包裹后 ? 占位符数量不被翻译破坏。"""
        import src.mysql_compat as mc
        sql = (
            "UPDATE user_repos SET user_id=? "
            "WHERE user_id=? AND repo_key NOT IN "
            "(SELECT repo_key FROM (SELECT repo_key FROM user_repos WHERE user_id=?) t)"
        )
        out = mc.translate_sql(sql)
        assert out.count("%s") == 3, out
