"""Issue #179: Skill 远程同步 + 技能分类表独立化 — 单元测试

覆盖：
- skill_category 表建表 + seed 15 分类
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

os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_issue179.db")

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
    """初始化测试 DB，结束时清理测试数据"""
    from src.api.db_setup import init_database
    init_database()
    # init_database 不再 seed skill，手动同步测试数据
    from src.storage.skill_sync import _sync_skills_to_db
    import json as _json
    import os as _os
    _json_path = _os.path.join(_os.path.dirname(__file__), "..", "configs", "skill-description.json")
    if _os.path.exists(_json_path):
        with open(_json_path, "r", encoding="utf-8") as f:
            _sync_skills_to_db(_json.load(f))
    yield
    # 清理测试用户和技能
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM skill WHERE skill_name='test-skill-179' AND scope='user'")
            c.execute("DELETE FROM user_accounts WHERE username='test_issue179'")
            db.commit()
        except Exception:
            pass


class TestSkillCategoryTable:
    """skill_category 表 + seed"""

    def test_table_exists(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM skill_category")
            assert c.fetchone()[0] == 15

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
            assert distinct == total == 15


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
        assert len(cats) == 15
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
        assert len(data["categories"]) == 15

    def test_agent_stats_returns_agents(self):
        headers = self._headers()
        r = client.get("/api/skills/agent-stats?scope=official", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "agents" in data
        assert "categories" not in data  # categories 由 /api/skills/categories 独立获取

    def test_list_skills_returns_category(self):
        headers = self._headers()
        r = client.get("/api/skills?scope=official&page=1&page_size=5", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "skills" in data
        assert data["skills"], "no skills returned"
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
        assert data["skills"], "no search results"
        s = data["skills"][0]
        assert "category_id" in s
        assert "category_name" in s

    def test_featured_returns_category(self):
        headers = self._headers()
        # 手动设一个 official skill 为 featured
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE skill SET is_featured=1 WHERE id=(SELECT id FROM skill WHERE scope='official' LIMIT 1)")
            db.commit()
        r = client.get("/api/skills/featured", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["skills"], "no featured skills"
        s = data["skills"][0]
        assert "category_id" in s
        assert "category_name" in s


class TestUploadCategoryValidation:
    """upload category_id 存在性校验"""

    def _headers(self):
        from src.user.db import create_user, get_user_by_username
        uid = create_user("test_issue179", "test123")
        if not uid:
            user = get_user_by_username("test_issue179")
            uid = int(user["id"]) if user else 1
        uid = int(uid)
        from src.api.jwt_utils import create_access_token
        from src.user.db import get_token_version
        token = create_access_token(uid, "test_issue179", get_token_version(uid))
        return {"Authorization": f"Bearer {token}"}

    def test_upload_invalid_category_id(self):
        """category_id=9999 不存在 -> 400"""
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("test-skill-179/SKILL.md", "---\nname: test-skill-179\ndescription: test\n---\n# Test")
        buf.seek(0)
        headers = self._headers()
        r = client.post(
            "/api/skills/upload",
            headers=headers,
            files={"file": ("test-skill-179.zip", buf, "application/zip")},
            data={"scope": "user", "agent_type": "coding", "category_id": "9999"},
        )
        assert r.status_code == 400
        assert "9999" in r.json()["detail"]["message"]


class TestConvertSkillCategory:
    """convert_skill category_id 传递"""

    def test_list_skills_returns_category_id_for_convert(self):
        """list_skills 返回 category_id 字段（convert_skill 依赖）"""
        from src.user.db import list_skills
        skills = list_skills(2)
        if skills:
            assert "category_id" in skills[0], "list_skills missing category_id field"
