"""Issue #56：featured_templates 启动种子单元测试。

验证 _seed_featured_templates_from_json() 从 configs/project-templates.json
种入 DB 的行为：表空时写入正确数量，幂等（表非空时跳过）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_featured_tables():
    """先 init 全部基础表（含 featured_templates），供测试依赖。"""
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture()
def _empty_featured():
    """每个用例前清空 featured_templates，模拟同步脚本未跑通的空表状态。"""
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM featured_templates")
        db.commit()
    yield


def test_seed_populates_from_json(_empty_featured):
    """表空时从 JSON 种入全部优选项目（gitcode + github）。"""
    from src.api.db_setup import _seed_featured_templates_from_json
    from src.session_manager import db, db_lock
    _seed_featured_templates_from_json()
    with db_lock:
        c = db.cursor()
        total = c.execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0]
        gc = c.execute("SELECT COUNT(*) FROM featured_templates WHERE platform='gitcode'").fetchone()[0]
        gh = c.execute("SELECT COUNT(*) FROM featured_templates WHERE platform='github'").fetchone()[0]
    assert total == gc + gh, f"total {total} != gitcode {gc} + github {gh}"
    assert gc > 0, "gitcode featured should not be empty after seed"
    assert gh > 0, "github featured should not be empty after seed"
    # 与 JSON 源数据条数一致（145 = gitcode 100 + github 45）
    import json
    with open(os.path.join(os.path.dirname(__file__), "..", "configs", "project-templates.json"), encoding="utf-8") as f:
        cats = {c["id"]: c for c in json.load(f)["categories"]}
    expected = len(cats.get("gitcode-featured", {}).get("templates", [])) + len(cats.get("github-featured", {}).get("templates", []))
    assert total == expected, f"seeded {total} != json {expected}"


def test_seed_idempotent_when_table_nonempty(_empty_featured):
    """表非空时再次调用不重复写入（幂等）。"""
    from src.api.db_setup import _seed_featured_templates_from_json
    from src.session_manager import db, db_lock
    _seed_featured_templates_from_json()
    with db_lock:
        first = db.cursor().execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0]
    _seed_featured_templates_from_json()
    with db_lock:
        second = db.cursor().execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0]
    assert second == first, f"second call changed row count: {first} -> {second}"


def test_seed_self_heals_after_clear(_empty_featured):
    """DB 被清空后再次调用能自愈重新种入（启动自愈语义）。"""
    from src.api.db_setup import _seed_featured_templates_from_json
    from src.session_manager import db, db_lock
    _seed_featured_templates_from_json()
    with db_lock:
        db.cursor().execute("DELETE FROM featured_templates")
        db.commit()
        assert db.cursor().execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0] == 0
    _seed_featured_templates_from_json()
    with db_lock:
        total = db.cursor().execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0]
    assert total > 0, "table should self-heal after clear"
