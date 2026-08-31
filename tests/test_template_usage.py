"""模板使用量（精选推荐「用户创建最多」）测试：template_usage 表 / 计数去重 / 回填幂等 / 匹配"""
import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.user.db import get_meta_value, set_meta_value
from src.api.routes import templates as tpl


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM template_usage")
        c.execute("DELETE FROM meta WHERE key='tmpl_usage_backfilled'")
        db.commit()


def test_template_usage_table_exists():
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(template_usage)").fetchall()]
    assert "template_id" in cols
    assert "user_key" in cols


def test_incr_dedupe_by_user():
    """同用户创建多次只计 1 次（口径 B：不同用户数）"""
    _clean()
    tpl._incr_template_usage("hello-flask", "alice")
    tpl._incr_template_usage("hello-flask", "alice")
    tpl._incr_template_usage("hello-flask", "bob")
    usage = tpl._load_template_usage()
    assert usage.get("hello-flask") == 2
    _clean()


def test_match_template_longest_prefix():
    """HELLO-BLOG-SYS 必须归属 hello-blog-sys，不能被 hello-blog 抢占"""
    assert tpl._match_template_id("HELLO-BLOG-SYS") == "hello-blog-sys"
    assert tpl._match_template_id("HELLO-BLOG-SYS-3") == "hello-blog-sys"
    assert tpl._match_template_id("team/hello-blog-sys22") == "hello-blog-sys"
    assert tpl._match_template_id("hello-flask2") == "hello-flask"
    assert tpl._match_template_id("team/hello-todo") == "hello-todo"
    # 无法归属的自定义仓库名
    assert tpl._match_template_id("redbook-dev") is None
    assert tpl._match_template_id("my-skill") is None


def test_backfill_idempotent():
    """回填：从 events(source=template) 统计不同用户数，且重复执行不叠加"""
    _clean()
    with db_lock:
        c = db.cursor()
        for rk in ("gitcode:alice/HELLO-BLOG-SYS",
                   "gitcode:bob/hello-flask",
                   "gitcode:alice/hello-todo",
                   "gitcode:custom/nonexistent-repo"):
            c.execute(
                "INSERT INTO events (repo_key, source, event_type) "
                "VALUES (?, 'template', 'onboarding_exp')", (rk,))
        db.commit()
    tpl._backfill_template_usage(force=True)
    first = tpl._load_template_usage()
    assert first.get("hello-blog-sys") == 1, first
    assert first.get("hello-flask") == 1, first
    assert first.get("hello-todo") == 1, first
    # 自定义名不归属
    assert "custom-nonexistent-repo" not in first
    # 幂等：再跑不叠加
    tpl._backfill_template_usage(force=True)
    assert tpl._load_template_usage() == first
    # meta 守卫：常规调用直接跳过
    tpl._backfill_template_usage()
    assert get_meta_value("tmpl_usage_backfilled") == "1"
    _clean()