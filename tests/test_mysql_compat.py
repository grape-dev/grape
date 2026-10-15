"""MySQL 兼容层单元测试（src/mysql_compat.py）。

覆盖：配置读取（load/get_db_backend/mysql_config）、SQLite→MySQL 方言翻译
（translate_sql / _translate_create_table / _split_create_columns / _replace_qmarks / _backtick_key）、
MySQLCompatCursor / MySQLCompatDB 的最小接口（mock pymysql，不连真库）。
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import src.mysql_compat as mc


# ====== 配置读取 ======


def _write_cfg(tmp_path, data):
    p = tmp_path / "db-backend.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestConfig:
    def test_load_default_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(tmp_path / "nope.json"))
        assert mc.load_db_backend_config() == {"backend": "sqlite", "mysql": {}}

    def test_load_invalid_json_returns_default(self, tmp_path, monkeypatch):
        p = tmp_path / "db-backend.json"
        p.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
        assert mc.load_db_backend_config() == {"backend": "sqlite", "mysql": {}}

    def test_load_normalizes_backend_and_mysql(self, tmp_path, monkeypatch):
        p = _write_cfg(tmp_path, {"backend": "MySQL", "mysql": {"host": "h"}})
        monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
        cfg = mc.load_db_backend_config()
        assert cfg["backend"] == "mysql" and cfg["mysql"] == {"host": "h"}

    def test_load_mysql_none_returns_empty_dict(self, tmp_path, monkeypatch):
        p = _write_cfg(tmp_path, {"backend": "mysql", "mysql": None})
        monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
        assert mc.load_db_backend_config()["mysql"] == {}

    @pytest.mark.parametrize("env,expect", [
        ("mysql", "mysql"), ("rds", "mysql"), ("1", "mysql"), ("true", "mysql"),
        ("sqlite", "sqlite"), ("0", "sqlite"), ("false", "sqlite"), ("", "sqlite"),
    ])
    def test_get_db_backend_env(self, monkeypatch, env, expect):
        monkeypatch.setenv("DEVOPS_DB_BACKEND", env)
        assert mc.get_db_backend() == expect

    def test_get_db_backend_falls_back_to_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEVOPS_DB_BACKEND", raising=False)
        p = _write_cfg(tmp_path, {"backend": "mysql"})
        monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
        assert mc.get_db_backend() == "mysql"

    def test_mysql_config_env_overrides_all(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_DB_HOST", "10.1.1.1")
        monkeypatch.setenv("DEVOPS_DB_PORT", "3307")
        monkeypatch.setenv("DEVOPS_DB_USER", "u")
        monkeypatch.setenv("DEVOPS_DB_PASSWORD", "p")
        monkeypatch.setenv("DEVOPS_DB_NAME", "db")
        c = mc.mysql_config()
        assert c == {"host": "10.1.1.1", "port": 3307, "user": "u",
                     "password": "p", "database": "db", "charset": "utf8mb4"}

    def test_mysql_config_url_parse(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_DB_URL", "mysql://u2:p2@10.9.9.9:3308/db2")
        c = mc.mysql_config()
        assert c["host"] == "10.9.9.9" and c["port"] == 3308
        assert c["user"] == "u2" and c["password"] == "p2" and c["database"] == "db2"


# ====== translate_sql 纯逻辑 ======


class TestTranslateSql:
    def test_empty_returns_as_is(self):
        assert mc.translate_sql("") == ""
        assert mc.translate_sql(None) is None
        assert mc.translate_sql("   ") == "   "

    def test_pragma_table_info_marked(self):
        assert mc.translate_sql("PRAGMA table_info(users)") == "PRAGMA table_info(users)"

    def test_pragma_index_marked(self):
        assert mc.translate_sql("PRAGMA index_list(users)") == "PRAGMA index_list(users)"
        assert mc.translate_sql("PRAGMA index_info(idx_x)") == "PRAGMA index_info(idx_x)"

    def test_pragma_ignored_becomes_noop(self):
        assert mc.translate_sql("PRAGMA journal_mode=WAL") == "__NOOP__"
        assert mc.translate_sql("PRAGMA foreign_keys=ON") == "__NOOP__"

    def test_sqlite_master_name_to_show_tables(self):
        assert mc.translate_sql("SELECT name FROM sqlite_master WHERE type='table'") == "SHOW TABLES"

    def test_sqlite_master_sql_marked(self):
        sql = "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        assert mc.translate_sql(sql) == sql

    def test_create_index_marked(self):
        sql = "CREATE INDEX IF NOT EXISTS idx_x ON users (id)"
        assert mc.translate_sql(sql) == sql
        sql2 = "CREATE UNIQUE INDEX IF NOT EXISTS idx_y ON users (name)"
        assert mc.translate_sql(sql2) == sql2

    def test_insert_or_replace_to_replace(self):
        assert "REPLACE INTO t" in mc.translate_sql("INSERT OR REPLACE INTO t (a) VALUES (1)")
        assert "INSERT OR REPLACE" not in mc.translate_sql("INSERT OR REPLACE INTO t (a) VALUES (1)")

    def test_insert_or_ignore_to_insert_ignore(self):
        assert "INSERT IGNORE INTO t" in mc.translate_sql("INSERT OR IGNORE INTO t (a) VALUES (1)")

    def test_datetime_now_translated(self):
        assert "CAST(NOW() AS CHAR)" in mc.translate_sql("UPDATE t SET x=datetime('now')")

    def test_concat_translated(self):
        assert mc.translate_sql("SELECT 'gitcode:' || repo_key FROM t") == \
            "SELECT CONCAT('gitcode:', repo_key) FROM t"

    def test_cast_as_text_to_char(self):
        """CAST(seq AS TEXT) → CAST(seq AS CHAR)（MySQL 不支持 TEXT 作 CAST 目标）"""
        out = mc.translate_sql("SELECT * FROM events WHERE CAST(seq AS TEXT) LIKE %s")
        assert "CAST(seq AS CHAR)" in out
        assert "AS TEXT" not in out

    def test_cast_as_text_case_insensitive(self):
        """大小写不敏感：CAST(seq AS text) 同样翻译"""
        out = mc.translate_sql("SELECT CAST(seq AS text) FROM t")
        assert "CAST(seq AS CHAR)" in out

    def test_cast_as_text_preserves_expression(self):
        """CAST(复杂表达式 AS TEXT) → CAST(复杂表达式 AS CHAR)"""
        out = mc.translate_sql("SELECT CAST(seq || ':' || title AS TEXT) FROM t")
        assert "AS CHAR)" in out
        assert "AS TEXT" not in out

    def test_cast_as_char_not_double_translated(self):
        """已经是 CAST(x AS CHAR) 的不被修改"""
        sql = "SELECT CAST(seq AS CHAR) FROM t"
        assert mc.translate_sql(sql) == sql

    def test_agent_keyword_search_sql_translated(self):
        """回归测试 #328: agents.py keyword 搜索 SQL 中的 CAST(seq AS TEXT) 被翻译为 CHAR。
        模拟实际 WHERE 子句，确保 MySQL 后端不再 1064 语法错误。"""
        sql = ("SELECT seq, event_type, title FROM events WHERE "
               "repo_key IN (%s) AND agent_type=%s AND "
               "(CAST(seq AS TEXT) LIKE %s OR issue_id LIKE %s OR event_type LIKE %s "
               "OR title LIKE %s OR summary LIKE %s OR actor LIKE %s) "
               "ORDER BY seq DESC LIMIT %s OFFSET %s")
        out = mc.translate_sql(sql)
        assert "CAST(seq AS CHAR)" in out
        assert "CAST(seq AS TEXT)" not in out

    def test_qmark_to_placeholder(self):
        assert mc.translate_sql("SELECT * FROM t WHERE a=? AND b=?") == \
            "SELECT * FROM t WHERE a=%s AND b=%s"

    def test_qmark_skipped_inside_string(self):
        assert mc.translate_sql("SELECT 'a?b' , c=?") == "SELECT 'a?b' , c=%s"

    def test_meta_key_backticked(self):
        sql = "SELECT value FROM meta WHERE key='x'"
        out = mc.translate_sql(sql)
        assert "`key`" in out and "WHERE `key`='x'" in out

    def test_meta_key_protects_primary_key(self):
        sql = "ALTER TABLE meta ADD PRIMARY KEY (key)"
        out = mc.translate_sql(sql)
        assert "PRIMARY KEY" in out

    def test_binary_column_backticked_in_create(self):
        sql = "CREATE TABLE t (binary TEXT, id INT)"
        out = mc.translate_sql(sql)
        assert "`binary`" in out

    def test_alter_add_text_default_wrapped(self):
        sql = "ALTER TABLE t ADD COLUMN col TEXT NOT NULL DEFAULT 'x'"
        out = mc.translate_sql(sql)
        assert "DEFAULT ('x')" in out

    def test_delete_not_in_same_table_wrapped(self):
        sql = "DELETE FROM issue WHERE id NOT IN (SELECT id FROM issue WHERE status='active')"
        out = mc.translate_sql(sql)
        assert "DELETE FROM `tasks`" in out
        assert "AS __derived" in out

    # ---- ON CONFLICT DO UPDATE（UPSERT）→ ON DUPLICATE KEY UPDATE（cluster-seq-11130 heartbeat 根因）----

    _HEARTBEAT_SQL = (
        "INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled) VALUES (?,?,?,1) "
        "ON CONFLICT(instance_id) DO UPDATE SET "
        "last_heartbeat=datetime('now'), "
        "hostname=COALESCE(excluded.hostname, hostname), "
        "ip=COALESCE(excluded.ip, ip), im_enabled=1"
    )

    def test_conflict_upsert_to_duplicate_key(self):
        out = mc.translate_sql(self._HEARTBEAT_SQL)
        assert "ON DUPLICATE KEY UPDATE" in out
        assert "ON CONFLICT" not in out
        assert "excluded." not in out
        assert "VALUES(hostname)" in out and "VALUES(ip)" in out
        assert "CAST(NOW() AS CHAR)" in out
        assert "%s" in out and "?" not in out.replace("VALUES", "")

    def test_conflict_upsert_backtick_excluded_cols(self):
        sql = ("INSERT INTO t (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET "
               "v=COALESCE(excluded.`v`, v)")
        out = mc.translate_sql(sql)
        assert "ON DUPLICATE KEY UPDATE" in out
        assert "VALUES(`v`)" in out or "VALUES(v)" in out
        assert "excluded." not in out

    def test_conflict_upsert_upper_case_insensitive(self):
        sql = ("insert into t (a) values (1) "
               "on conflict(a) do update set a=excluded.a")
        out = mc.translate_sql(sql)
        assert "ON DUPLICATE KEY UPDATE" in out
        assert "VALUES(a)" in out and "excluded." not in out

    def test_delete_not_in_other_table_not_wrapped(self):
        sql = "DELETE FROM a WHERE id NOT IN (SELECT id FROM b WHERE x=1)"
        out = mc.translate_sql(sql)
        assert "__derived" not in out


class TestTranslateCreateTable:
    def test_text_primary_key_to_varchar(self):
        out = mc.translate_sql("CREATE TABLE t (id TEXT PRIMARY KEY, name TEXT)")
        assert "id VARCHAR(191) PRIMARY KEY" in out

    def test_integer_pk_autoincrement_to_mysql(self):
        out = mc.translate_sql("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, x INT)")
        assert "id INT AUTO_INCREMENT PRIMARY KEY" in out

    def test_text_default_to_expression(self):
        out = mc.translate_sql("CREATE TABLE t (name TEXT NOT NULL DEFAULT 'default')")
        assert "name TEXT NOT NULL DEFAULT ('default')" in out

    def test_composite_primary_key_text_to_varchar(self):
        out = mc.translate_sql("CREATE TABLE t (repo_key TEXT, agent_type TEXT, PRIMARY KEY (repo_key, agent_type))")
        assert "repo_key VARCHAR(191)" in out
        assert "agent_type VARCHAR(191)" in out

    def test_inline_unique_text_to_varchar(self):
        out = mc.translate_sql("CREATE TABLE t (username TEXT UNIQUE NOT NULL)")
        assert "username VARCHAR(191)" in out

    def test_unparseable_returned_as_is(self):
        sql = "CREATE TABLE t "  # 缺列体 → 无法解析，原样
        assert mc.translate_sql(sql) == sql

    def test_nested_paren_default(self):
        out = mc.translate_sql("CREATE TABLE t (payload TEXT DEFAULT '{}', id INT)")
        assert "payload TEXT DEFAULT ('{}')" in out


# ====== 辅助函数 ======


class TestSplitColumns:
    def test_simple(self):
        assert mc._split_create_columns("a INT, b TEXT, c INT") == ["a INT", "b TEXT", "c INT"]

    def test_nested_parens(self):
        cols = mc._split_create_columns("a TEXT DEFAULT ('{}'), b INT, PRIMARY KEY (a, b)")
        assert len(cols) == 3
        assert "DEFAULT ('{}')" in cols[0]

    def test_string_with_comma(self):
        cols = mc._split_create_columns("a TEXT DEFAULT 'x,y', b INT")
        assert cols == ["a TEXT DEFAULT 'x,y'", "b INT"]

    def test_line_comment(self):
        cols = mc._split_create_columns("a INT -- 注释,带逗号\n, b INT")
        assert len(cols) == 2


class TestReplaceQmarks:
    def test_basic(self):
        assert mc._replace_qmarks("a=? AND b=?") == "a=%s AND b=%s"

    def test_skip_string(self):
        assert mc._replace_qmarks("a='?' AND b=?") == "a='?' AND b=%s"


class TestBacktickKey:
    def test_key_column(self):
        out = mc._backtick_key("WHERE key='x'")
        assert "`key`" in out

    def test_protects_keyword_key(self):
        out = mc._backtick_key("ADD PRIMARY KEY (a)")
        assert "PRIMARY KEY" in out and "`key`" not in out


# ====== Cursor / DB 类（mock pymysql） ======


class TestCompatCursor:
    def _cursor(self, raw_rows=None):
        raw = MagicMock()
        if raw_rows is not None:
            raw.fetchall.return_value = raw_rows
            raw.fetchone.return_value = raw_rows[0] if raw_rows else None
        db = mc.MySQLCompatDB(host="h")
        db._raw_cursor = MagicMock(return_value=raw)
        return db, raw

    def test_pragma_table_info_delegates(self):
        db, raw = self._cursor([(0, "id", "int", 1, None, 1)])
        cur = db.cursor()
        cur.execute("PRAGMA table_info(users)")
        assert raw.execute.call_count == 1
        assert "information_schema.columns" in raw.execute.call_args[0][0]
        assert cur.rowcount == 1
        assert cur.fetchone() == (0, "id", "int", 1, None, 1)

    def test_noop_pragma(self):
        db, _ = self._cursor()
        cur = db.cursor()
        cur.execute("__NOOP__")
        assert cur.fetchall() == []

    def test_normal_execute_translates_and_fetches(self):
        db, raw = self._cursor()
        raw.fetchall.return_value = [(1,)]
        raw.fetchone.return_value = (1,)
        raw.rowcount = 1
        cur = db.cursor()
        cur.execute("SELECT * FROM t WHERE id=?")
        # 翻译后的 SQL 走 %s
        assert "%s" in raw.execute.call_args[0][0]
        assert "?" not in raw.execute.call_args[0][0]
        assert cur.fetchall() == [(1,)]

    def test_executemany(self):
        db, raw = self._cursor()
        raw.rowcount = 0
        cur = db.cursor()
        cur.executemany("INSERT INTO t (a) VALUES (?)", [(1,), (2,)])
        assert raw.execute.call_count == 2

    def test_scalar_param_wrapped(self):
        db, raw = self._cursor()
        raw.rowcount = 0
        cur = db.cursor()
        cur.execute("SELECT * FROM t WHERE id=?", 5)
        assert raw.execute.call_args[0][1] == (5,)

    def test_pragma_index_list(self):
        db, raw = self._cursor([(0, "idx_x", 1)])
        cur = db.cursor()
        cur.execute("PRAGMA index_list(users)")
        assert "information_schema.statistics" in raw.execute.call_args[0][0]
        assert cur.fetchall() == [(0, "idx_x", 1)]

    def test_pragma_index_info(self):
        db, raw = self._cursor([(0, 0, "name")])
        cur = db.cursor()
        cur.execute("PRAGMA index_info(idx_x)")
        assert "SEQ_IN_INDEX" in raw.execute.call_args[0][0]
        assert cur.fetchall() == [(0, 0, "name")]

    def test_sqlite_master_sql_normalizes_create(self):
        db, raw = self._cursor([("tbl", "CREATE TABLE `users` (`id` int,`name` text)")])
        cur = db.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        assert cur.fetchall() == [("CREATE TABLE users (id int, name text)",)]

    def test_sqlite_master_sql_no_row(self):
        db, raw = self._cursor([])
        raw.fetchone.return_value = None
        cur = db.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        assert cur.fetchall() == []

    def test_create_index_exists_skips(self):
        db, raw = self._cursor()
        raw.fetchone.return_value = (1,)  # 已存在
        cur = db.cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_x ON users (name)")
        # 只查 statistics，不创建
        assert raw.execute.call_count == 1

    def test_create_index_missing_creates_with_text_prefix(self):
        db, raw = self._cursor()
        # fetchone 依次：statistics 计数(0) → 列类型查询(text)
        raw.fetchone.side_effect = [(0,), ("text",)]
        cur = db.cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_x ON users (name)")
        last_sql = raw.execute.call_args_list[-1][0][0]
        assert "CREATE  INDEX" in last_sql or "CREATE INDEX" in last_sql
        assert "`name`(191)" in last_sql


class TestCompatDB:
    def test_raw_cursor_connects_on_first_use(self):
        db = mc.MySQLCompatDB(host="10.0.0.1", port=3306, user="u", password="p", database="d")
        with patch("src.mysql_compat.pymysql.connect") as m_conn:
            m_conn.return_value.cursor.return_value = MagicMock()
            cur = db._raw_cursor()
        assert cur is not None
        assert m_conn.call_count == 1

    def test_raw_cursor_reconnects_on_ping_failure(self):
        db = mc.MySQLCompatDB(host="h")
        old_conn = MagicMock()
        db._local.conn = old_conn
        # 第一次 ping 抛异常 → 重新 connect
        old_conn.ping.side_effect = RuntimeError("gone")
        with patch("src.mysql_compat.pymysql.connect") as m_conn:
            m_conn.return_value.cursor.return_value = MagicMock()
            db._raw_cursor()
        assert m_conn.call_count == 1

    def test_commit_rollback_close_noop_without_conn(self):
        db = mc.MySQLCompatDB()
        db.commit()
        db.rollback()
        db.close()  # 不应抛错

    def test_close_with_conn(self):
        db = mc.MySQLCompatDB()
        fake_conn = MagicMock()
        db._local.conn = fake_conn
        db.close()
        fake_conn.close.assert_called_once()
        assert getattr(db._local, "conn", None) is None

    def test_execute_delegates_to_cursor(self):
        db = mc.MySQLCompatDB()
        db._raw_cursor = MagicMock()
        db._raw_cursor.return_value.rowcount = 0
        cur = db.execute("SELECT 1")
        assert isinstance(cur, mc.MySQLCompatCursor)

    def test_executescript_splits_statements(self):
        db = mc.MySQLCompatDB()
        db._raw_cursor = MagicMock()
        db._raw_cursor.return_value.rowcount = 0
        db.executescript("CREATE TABLE a (id INT);\nCREATE TABLE b (id INT);")
        # 两条语句各执行一次（每次经 translate/execute）
        assert db._raw_cursor.call_count == 2
