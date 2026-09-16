"""Issue #179: Skill 远程同步 + 技能分类表独立化 — 单元测试

覆盖：
- skill_category 表建表 + seed 11 分类
- category_id 回填（sub_scene → slug 映射）
- list_skill_categories / get_skill_category 查询
- _sync_skills_to_db 写入 category_id + 变更检测
- upsert_skill_obs 写入 category_id
- agent-stats 返回 categories（替代 sub_scenes）
- /api/skills/categories 接口
- list_skills category_id 过滤 + 返回 category_name
- search_skills 返回 category
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_issue179.db"))

import json
import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)


def _jwt(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


@pytest.fixture(scope="module", autouse=True)
def _setup():
    """初始化测试 DB"""
    from src.api.db_setup import init_database
    init_database()
    yield


class TestSkillCategoryTable:
    """skill_category 表 + seed"""

    def test_table_exists(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM skill_category")
            assert c.fetchone()[0] == 11

    def test_seed_data(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT name, slug, icon, order_num FROM skill_category WHERE slug='ai-coding-review'")
            row = c.fetchone()
            assert row is not None
            assert row[0] == "AI辅助编码与评审"
            assert row[1] == "ai-coding-review"
            assert row[2] == "🤖"
            assert row[3] == 1

    def test_slug_not_null(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM skill_category WHERE slug IS NULL OR slug=''")
            assert c.fetchone()[0] == 0

    def test_unique_slug(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(DISTINCT slug), COUNT(*) FROM skill_category")
            distinct, total = c.fetchone()
            assert distinct == total == 11


class TestCategoryBackfill:
    """category_id 回填"""

    def test_official_skills_have_category_id(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM skill WHERE scope='official' AND category_id > 0")
            assert c.fetchone()[0] > 0

    def test_official_skills_zero_category_id(self):
        """official 技能 sub_scene 非空的应该全部回填"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM skill WHERE scope='official' "
                "AND sub_scene != '' AND category_id = 0"
            )
            assert c.fetchone()[0] == 0

    def test_category_id_matches_sub_scene(self):
        """category_id 和 sub_scene 对应关系正确"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT s.sub_scene, sc.slug FROM skill s "
                "JOIN skill_category sc ON s.category_id = sc.id "
                "WHERE s.scope='official' AND s.sub_scene != '' "
                "LIMIT 5"
            )
            for sub_scene, slug in c.fetchall():
                assert sub_scene == slug


class TestCategoryQueries:
    """list_skill_categories / get_skill_category"""

    def test_list_skill_categories(self):
        from src.user.db import list_skill_categories
        cats = list_skill_categories()
        assert len(cats) == 11
        assert all("id" in c and "name" in c and "slug" in c for c in cats)
        # 按 order_num 排序
        order_nums = [c["order_num"] for c in cats]
        assert order_nums == sorted(order_nums)

    def test_get_skill_category(self):
        from src.user.db import get_skill_category
        cat = get_skill_category(1)
        assert cat is not None
        assert cat["slug"] == "ai-coding-review"

    def test_get_skill_category_not_found(self):
        from src.user.db import get_skill_category
        cat = get_skill_category(9999)
        assert cat is None


class TestSyncSkillsToDb:
    """_sync_skills_to_db 写入 category_id + 变更检测"""

    def test_sync_returns_count(self):
        from src.storage.skill_sync import _sync_skills_to_db
        config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "skill-description.json")
        with open(config_path) as f:
            data = json.load(f)
        count = _sync_skills_to_db(data)
        assert count > 0

    def test_sync_writes_category_id(self):
        from src.storage.skill_sync import _sync_skills_to_db
        config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "skill-description.json")
        with open(config_path) as f:
            data = json.load(f)
        _sync_skills_to_db(data)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM skill WHERE scope='official' "
                "AND sub_scene != '' AND category_id = 0"
            )
            assert c.fetchone()[0] == 0

    def test_sync_no_updated_at_change_when_data_unchanged(self):
        """数据没变时 updated_at 不应该被刷新"""
        from src.storage.skill_sync import _sync_skills_to_db
        config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "skill-description.json")
        with open(config_path) as f:
            data = json.load(f)
        # 第一次同步
        _sync_skills_to_db(data)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_name, updated_at FROM skill WHERE scope='official' LIMIT 5")
            before = {r[0]: r[1] for r in c.fetchall()}
        # 第二次同步（数据没变）
        _sync_skills_to_db(data)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_name, updated_at FROM skill WHERE scope='official' LIMIT 5")
            after = {r[0]: r[1] for r in c.fetchall()}
        for name in before:
            assert before[name] == after.get(name), f"{name} updated_at changed unexpectedly"


class TestUpsertSkillObs:
    """upsert_skill_obs 写入 category_id"""

    def test_upsert_with_category_id(self):
        from src.user.db import upsert_skill_obs
        ok = upsert_skill_obs(
            scope="user", owner="test_179", skill_name="test-skill-179",
            agent_type="coding", description="test", object_key="test-key",
            created_by="test", category_id=3
        )
        assert ok is True
        with db_lock:
            c = db.cursor()
            c.execute("SELECT category_id FROM skill WHERE skill_name='test-skill-179' AND scope='user'")
            row = c.fetchone()
            assert row is not None
            assert row[0] == 3
        # cleanup
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name='test-skill-179' AND scope='user'")
            db.commit()


class TestApiEndpoints:
    """API 接口测试"""

    def _headers(self):
        from src.user.db import create_user, get_user_by_username
        uid = create_user("test_issue179", "test123")
        if not uid:
            uid = get_user_by_username("test_issue179")
        token = _jwt(uid["id"], uid["username"])
        return {"Authorization": f"Bearer {token}"}

    def test_categories_endpoint(self):
        headers = self._headers()
        r = client.get("/api/skills/categories", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "categories" in data
        assert len(data["categories"]) == 11

    def test_agent_stats_returns_categories(self):
        headers = self._headers()
        r = client.get("/api/skills/agent-stats?scope=official", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "categories" in data
        assert "sub_scenes" not in data
        assert len(data["categories"]) > 0

    def test_list_skills_returns_category(self):
        headers = self._headers()
        r = client.get("/api/skills?scope=official&page=1&page_size=5", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "skills" in data
        if data["skills"]:
            s = data["skills"][0]
            assert "category_id" in s
            assert "category_name" in s
            assert "sub_scene" not in s
            assert "sub_scene_id" not in s

    def test_list_skills_category_filter(self):
        headers = self._headers()
        # 先查有哪些分类
        r = client.get("/api/skills/categories", headers=headers)
        cat_id = r.json()["categories"][0]["id"]
        # 按分类过滤
        r2 = client.get(f"/api/skills?scope=official&category_id={cat_id}&page=1&page_size=20", headers=headers)
        assert r2.status_code == 200
        skills = r2.json()["skills"]
        for s in skills:
            assert s["category_id"] == cat_id

    def test_search_returns_category(self):
        headers = self._headers()
        r = client.get("/api/skills/search?q=vod", headers=headers)
        assert r.status_code == 200
        data = r.json()
        if data["skills"]:
            s = data["skills"][0]
            assert "category_id" in s
            assert "category_name" in s

    def test_featured_returns_category(self):
        headers = self._headers()
        r = client.get("/api/skills/featured", headers=headers)
        assert r.status_code == 200
        data = r.json()
        if data["skills"]:
            s = data["skills"][0]
            assert "category_id" in s
            assert "category_name" in s
