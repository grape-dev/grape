"""Issue #268 — latency cleanup SQLite datetime() 语法在 MySQL 后端失效修复测试。

覆盖：
1. SQLite 路径（默认测试后端）：超期记录删除 + 超量裁剪正常执行
2. MySQL SQL 方言：_maybe_cleanup() 与 _cleanup_old_latency_logs() 生成 NOW() - INTERVAL N DAY
"""
from unittest.mock import MagicMock

import pytest

from src.api.routes import latency
from src.api import db_setup


# ====== 辅助 ======


def _reset_cleanup_state():
    """重置 _maybe_cleanup 节流状态，使下一次调用真正执行清理。"""
    latency._last_cleanup = 0


def _insert_latency_row(created_at="2020-01-01 00:00:00", method="GET", path="/x"):
    """直接插入一条 api_latency_log 记录（绕过 report_latency 的 datetime('now') 默认值）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO api_latency_log (method, path, status, duration_ms, source, created_at) "
            "VALUES (?, ?, ?, ?, 'frontend', ?)",
            (method, path, 200, 10, created_at),
        )
        db.commit()
    return c.lastrowid


def _count_latency_rows():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        return c.execute("SELECT COUNT(*) FROM api_latency_log").fetchone()[0]


def _make_mock_db(captured):
    """构造 mock db：cursor() 返回的 mock cursor 记录所有 execute SQL。
    fetchone 默认返回 (0,) 模拟空表，rowcount=0。"""
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = lambda sql, *a, **kw: captured.append(sql) or mock_cursor
    mock_cursor.fetchone.return_value = (0,)
    mock_cursor.rowcount = 0
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor
    return mock_db


# ====== SQLite 路径（默认测试后端）======


class TestSqlitePath:
    """conftest 强制 DEVOPS_DB_BACKEND=sqlite，验证原 datetime('now','-N days') 语法正常工作。"""

    def test_old_records_deleted(self):
        _reset_cleanup_state()
        _insert_latency_row(created_at="2020-01-01 00:00:00")
        assert _count_latency_rows() >= 1
        latency._maybe_cleanup()
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM api_latency_log WHERE created_at='2020-01-01 00:00:00'"
            ).fetchone()[0]
        assert row == 0, "超期记录未被删除"

    def test_recent_records_kept(self):
        _reset_cleanup_state()
        from src.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute(
                "INSERT INTO api_latency_log (method, path, status, duration_ms, source) "
                "VALUES ('GET', '/keep', 200, 5, 'frontend')"
            )
            db.commit()
        latency._maybe_cleanup()
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM api_latency_log WHERE path='/keep'"
            ).fetchone()[0]
        assert row == 1, "新记录被误删"

    def test_trim_when_exceeding_max_rows(self, monkeypatch):
        """超量裁剪逻辑不再因前序 DELETE 异常被跳过。"""
        _reset_cleanup_state()
        monkeypatch.setattr(latency, "_MAX_ROWS", 3)
        from src.session_manager import db, db_lock
        before = _count_latency_rows()
        # 插入近期记录（created_at=now），确保不被超期 DELETE 删除，只触发超量裁剪
        for i in range(5):
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO api_latency_log (method, path, status, duration_ms, source) "
                    "VALUES ('GET', '/trim%d', 200, 1, 'frontend')" % i
                )
                db.commit()
        assert _count_latency_rows() == before + 5
        latency._maybe_cleanup()
        remaining = _count_latency_rows()
        assert remaining <= 3, f"超量裁剪未生效，剩余 {remaining} 行（上限 3）"


# ====== MySQL SQL 方言断言 ======


class TestMysqlDialect:
    """验证 MySQL 后端下生成 NOW() - INTERVAL N DAY 而非 datetime('now', '-N days')。"""

    def test_maybe_cleanup_uses_mysql_interval(self, monkeypatch):
        _reset_cleanup_state()
        import src.mysql_compat as mc
        monkeypatch.setattr(mc, "get_db_backend", lambda: "mysql")

        captured = []
        monkeypatch.setattr(latency, "db", _make_mock_db(captured))
        monkeypatch.setattr(latency, "db_lock", MagicMock())

        latency._maybe_cleanup()

        delete_sqls = [s for s in captured if s.upper().startswith("DELETE")]
        assert any("NOW() - INTERVAL" in s and "DAY" in s for s in delete_sqls), \
            f"MySQL 分支应使用 NOW() - INTERVAL N DAY，实际: {delete_sqls}"
        assert not any("datetime('now'" in s for s in delete_sqls), \
            f"MySQL 分支不应出现 SQLite datetime() 语法，实际: {delete_sqls}"

    def test_maybe_cleanup_sqlite_keeps_datetime(self, monkeypatch):
        _reset_cleanup_state()
        import src.mysql_compat as mc
        monkeypatch.setattr(mc, "get_db_backend", lambda: "sqlite")

        captured = []
        monkeypatch.setattr(latency, "db", _make_mock_db(captured))
        monkeypatch.setattr(latency, "db_lock", MagicMock())

        latency._maybe_cleanup()

        delete_sqls = [s for s in captured if s.upper().startswith("DELETE")]
        assert any("datetime('now'" in s and "days" in s for s in delete_sqls), \
            f"SQLite 分支应保留 datetime('now','-N days')，实际: {delete_sqls}"

    def test_cleanup_old_latency_logs_uses_mysql_interval(self, monkeypatch):
        import src.mysql_compat as mc
        monkeypatch.setattr(mc, "get_db_backend", lambda: "mysql")

        captured = []
        monkeypatch.setattr(db_setup, "db", _make_mock_db(captured))
        monkeypatch.setattr(db_setup, "db_lock", MagicMock())

        db_setup._cleanup_old_latency_logs()

        delete_sqls = [s for s in captured if s.upper().startswith("DELETE")]
        assert any("NOW() - INTERVAL 7 DAY" in s for s in delete_sqls), \
            f"db_setup MySQL 分支应使用 NOW() - INTERVAL 7 DAY，实际: {delete_sqls}"
        assert not any("datetime('now'" in s for s in delete_sqls), \
            f"db_setup MySQL 分支不应出现 SQLite datetime() 语法，实际: {delete_sqls}"

    def test_cleanup_old_latency_logs_sqlite_keeps_datetime(self, monkeypatch):
        import src.mysql_compat as mc
        monkeypatch.setattr(mc, "get_db_backend", lambda: "sqlite")

        captured = []
        monkeypatch.setattr(db_setup, "db", _make_mock_db(captured))
        monkeypatch.setattr(db_setup, "db_lock", MagicMock())

        db_setup._cleanup_old_latency_logs()

        delete_sqls = [s for s in captured if s.upper().startswith("DELETE")]
        assert any("datetime('now', '-7 days')" in s for s in delete_sqls), \
            f"db_setup SQLite 分支应保留 datetime('now','-7 days')，实际: {delete_sqls}"
