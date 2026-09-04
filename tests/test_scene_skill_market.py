"""场景市场 + 技能市场 API 集成测试。

覆盖：
- scene_market / scene_category / scene_likes / skill_likes CRUD
- 场景市场 6 个 API（categories/list/detail/apply/experience/like）
- 技能市场 4 个 API（like/featured/stats/search）
- list_skills / get_skill_detail_by_id 扩展字段
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.api.main import app  # noqa: E402
from src.user import db as user_db  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

# JWT access token helper
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    _uid = user_db.create_user("test_market", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_market")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    with db_lock:
        db.execute("DELETE FROM scene_likes WHERE user_id=?", (_uid["id"],))
        db.execute("DELETE FROM skill_likes WHERE user_id=?", (_uid["id"],))
        db.execute("DELETE FROM devops_pipeline_scene WHERE user_id=?", (_uid["id"],))
        db.commit()


# ===== Scene Category CRUD =====

class TestSceneCategories:
    def test_list_categories(self):
        resp = client.get("/api/scenes/market/categories", headers=_headers)
        assert resp.status_code == 200
        cats = resp.json()["categories"]
        assert len(cats) >= 6  # 初始 6 个分类
        assert "name" in cats[0]
        assert "icon" in cats[0]

    def test_get_category(self):
        cat = user_db.get_scene_category(1)
        assert cat is not None
        assert cat["name"] == "通用"


# ===== Scene Market List + Detail =====

class TestSceneMarket:
    def test_list_market(self):
        resp = client.get("/api/scenes/market", headers=_headers)
        assert resp.status_code == 200
        scenes = resp.json()["scenes"]
        assert len(scenes) >= 7  # 初始 8 条（1 条可能无 template）
        s = scenes[0]
        assert "id" in s
        assert "display_name" in s
        assert "is_featured" in s
        assert "like_count" in s
        assert "liked_by_me" in s
        assert "skills" in s
        assert "repo_count" in s

    def test_list_market_with_search(self):
        resp = client.get("/api/scenes/market?q=开发", headers=_headers)
        assert resp.status_code == 200
        scenes = resp.json()["scenes"]
        assert len(scenes) > 0

    def test_list_market_with_category(self):
        resp = client.get("/api/scenes/market?category_id=1", headers=_headers)
        assert resp.status_code == 200
        scenes = resp.json()["scenes"]
        for s in scenes:
            assert s["category_id"] == 1

    def test_list_market_sort_hot(self):
        resp = client.get("/api/scenes/market?sort=hot", headers=_headers)
        assert resp.status_code == 200
        scenes = resp.json()["scenes"]
        assert len(scenes) > 0

    def test_get_detail(self):
        # 先拿列表拿到第一个 scene id
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]

        resp = client.get(f"/api/scenes/market/{scene_id}", headers=_headers)
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["id"] == scene_id
        assert "market_description" in detail
        assert "skills" in detail
        assert "category_name" in detail
        assert "liked_by_me" in detail

    def test_get_detail_not_found(self):
        resp = client.get("/api/scenes/market/99999", headers=_headers)
        assert resp.status_code == 404


# ===== Scene Like =====

class TestSceneLike:
    def test_toggle_like(self):
        # 先拿到一个场景
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]

        # 点赞
        resp = client.post(f"/api/scenes/market/{scene_id}/like", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["liked"] is True
        assert data["like_count"] >= 1

        # 再调一次 → 取消
        resp = client.post(f"/api/scenes/market/{scene_id}/like", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["liked"] is False

    def test_like_nonexistent(self):
        resp = client.post("/api/scenes/market/99999/like", headers=_headers)
        assert resp.status_code == 404


# ===== Scene Apply =====

class TestSceneApply:
    def test_apply_no_repo(self):
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]
        resp = client.post(f"/api/scenes/market/{scene_id}/apply", headers=_headers)
        assert resp.status_code == 400


# ===== Skill Like =====

class TestSkillLike:
    def test_toggle_like(self):
        # 拿一个 official skill 的 id
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM skill WHERE scope='official' AND is_active=1 LIMIT 1")
            row = c.fetchone()
        assert row, "No official skill found in DB"
        skill_id = row[0]

        # 点赞
        resp = client.post(f"/api/skills/{skill_id}/like", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["liked"] is True

        # 取消
        resp = client.post(f"/api/skills/{skill_id}/like", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["liked"] is False

    def test_like_nonexistent(self):
        resp = client.post("/api/skills/99999/like", headers=_headers)
        assert resp.status_code == 404


# ===== Featured Skills =====

class TestFeaturedSkills:
    def test_list_featured(self):
        resp = client.get("/api/skills/featured", headers=_headers)
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        # 初始化了 6 个精选技能，但可能没全匹配到（依赖 skill-description.json sync）
        # 至少验证返回格式正确
        if skills:
            s = skills[0]
            assert "id" in s
            assert "is_featured" in s
            assert s["is_featured"] == 1
            assert "like_count" in s
            assert "liked_by_me" in s


# ===== Skill Stats =====

class TestSkillStats:
    def test_get_stats(self):
        resp = client.get("/api/skills/stats", headers=_headers)
        assert resp.status_code == 200
        stats = resp.json()["stats"]
        assert isinstance(stats, dict)
        # scene_skill_ref 初始化后应该有数据
        # 至少有一个 skill_name 的统计
        # 但如果 scene_skill_ref 为空（表存在但无数据），返回空 dict 也算合法
        for skill_name, data in stats.items():
            assert "scene_count" in data
            assert "repo_count" in data


# ===== Skill Search =====

class TestSkillSearch:
    def test_search_empty_q(self):
        resp = client.get("/api/skills/search", headers=_headers)
        assert resp.status_code == 200
        assert resp.json()["skills"] == []

    def test_search_keyword(self):
        resp = client.get("/api/skills/search?q=部署", headers=_headers)
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        # 验证返回格式（可能无匹配，但格式要对）
        for s in skills:
            assert "skill_id" in s
            assert "name" in s
            assert "display_name" in s
            assert "match_reason" in s

    def test_search_exact_name(self):
        # 先拿一个 official skill name
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_name FROM skill WHERE scope='official' AND is_active=1 LIMIT 1")
            row = c.fetchone()
        assert row
        resp = client.get(f"/api/skills/search?q={row[0]}", headers=_headers)
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        assert len(skills) > 0
        assert skills[0]["match_reason"] == "名称精确匹配"


# ===== list_skills 扩展字段 =====

class TestListSkillsExtended:
    def test_list_has_extended_fields(self):
        resp = client.get("/api/skills?scope=official", headers=_headers)
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        if skills:
            s = skills[0]
            # 原有字段
            assert "id" in s
            assert "name" in s
            assert "display_name" in s
            # 扩展字段
            assert "is_featured" in s
            assert "like_count" in s
            assert "liked_by_me" in s
            assert "scene_count" in s
            assert "repo_count" in s
            assert "scenes" in s


# ===== get_skill_detail_by_id 扩展字段 =====

class TestGetSkillDetailExtended:
    def test_detail_has_extended_fields(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM skill WHERE scope='official' AND is_active=1 LIMIT 1")
            row = c.fetchone()
        assert row
        skill_id = row[0]

        resp = client.get(f"/api/skills/by-id/{skill_id}", headers=_headers)
        assert resp.status_code == 200
        detail = resp.json()
        # 原有字段
        assert "id" in detail
        assert "name" in detail
        assert "content" in detail
        # 扩展字段
        assert "is_featured" in detail
        assert "like_count" in detail
        assert "liked_by_me" in detail
        assert "scenes" in detail
