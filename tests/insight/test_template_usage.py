"""template_usage 表结构验证（功能已在 a34315f2 重构为 featured_templates）"""
import os

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock


def test_template_usage_table_exists():
    """template_usage 表结构验证（表保留，功能已在 a34315f2 重构为 featured_templates）"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(template_usage)").fetchall()]
    assert "template_id" in cols
    assert "user_key" in cols


# ==== appended from weekly-08 ====
def test_featured_templates_table_exists():
    """featured_templates 表结构验证（template_usage 的承接表）。"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(featured_templates)").fetchall()]
    assert "platform" in cols
    assert "repo_full_name" in cols
    assert "category" in cols
    # 唯一契约：同平台同仓库只有一行
    with db_lock:
        idx = [r[1] for r in db.cursor().execute(
            "PRAGMA index_list(featured_templates)").fetchall()]
        uniq = [db.cursor().execute(
            f"PRAGMA index_info({i})").fetchall() for i in idx]
    uniq_cols = {tuple(sorted(r[2] for r in u)) for u in uniq if u}
    assert ("platform", "repo_full_name") in {tuple(c) for c in uniq_cols}


# ==== appended from weekly-08 ====
def test_template_usage_table_retired():
    """template_usage 已退役：无 DDL、无引用（防误判为缺口重新立项）。"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(template_usage)").fetchall()]
    assert cols == []
