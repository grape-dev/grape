"""template_usage 表结构验证（功能已在 a34315f2 重构为 featured_templates）"""
import os

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.session_manager import db, db_lock


def test_template_usage_table_exists():
    """template_usage 表结构验证（表保留，功能已在 a34315f2 重构为 featured_templates）"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(template_usage)").fetchall()]
    assert "template_id" in cols
    assert "user_key" in cols