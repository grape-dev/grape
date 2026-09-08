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


# ===== Scene Applied Check =====

class TestSceneAppliedCheck:
    def test_check_applied_no_repo(self):
        """无 owner/repo 参数时返回 applied=False"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]
        resp = client.get(f"/api/scenes/market/{scene_id}/applied", headers=_headers)
        assert resp.status_code == 200
        assert resp.json()["applied"] is False

    def test_check_applied_with_repo_not_applied(self):
        """有 owner/repo 但未应用时返回 applied=False"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]
        resp = client.get(f"/api/scenes/market/{scene_id}/applied?owner=test&repo=test", headers=_headers)
        assert resp.status_code == 200
        assert resp.json()["applied"] is False

    def test_check_applied_not_found(self):
        """不存在的市场场景返回 404"""
        resp = client.get("/api/scenes/market/99999/applied?owner=test&repo=test", headers=_headers)
        assert resp.status_code == 404


# ===== Scene Experience Check =====

class TestSceneExperienceCheck:
    def test_experience_check_not_found(self):
        """不存在的市场场景返回 404"""
        resp = client.get("/api/scenes/market/99999/experience-check", headers=_headers)
        assert resp.status_code == 404

    def test_experience_check_no_template(self):
        """无推荐模板的场景返回空值"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scenes = resp.json()["scenes"]
        no_tpl_id = None
        for s in scenes:
            detail = client.get(f"/api/scenes/market/{s['id']}", headers=_headers).json()
            if not detail.get("recommended_template"):
                no_tpl_id = s["id"]
                break
        if no_tpl_id:
            resp = client.get(f"/api/scenes/market/{no_tpl_id}/experience-check", headers=_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["repo_exists"] is False
            assert data["scene_applied"] is False
            assert data["full_name"] == ""

    def test_experience_check_success(self):
        """有推荐模板的场景返回正确格式"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scenes = resp.json()["scenes"]
        tpl_id = None
        for s in scenes:
            detail = client.get(f"/api/scenes/market/{s['id']}", headers=_headers).json()
            if detail.get("recommended_template"):
                tpl_id = s["id"]
                break
        if tpl_id:
            resp = client.get(f"/api/scenes/market/{tpl_id}/experience-check", headers=_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "repo_exists" in data
            assert "scene_applied" in data
            assert "full_name" in data
            assert "scene_name" in data


# ===== Scene Experience =====

class TestSceneExperience:
    def test_experience_not_found(self):
        """不存在的市场场景返回 404"""
        resp = client.post("/api/scenes/market/99999/experience", headers=_headers)
        assert resp.status_code == 404

    def test_experience_no_template(self):
        """无推荐模板的场景返回 400"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scenes = resp.json()["scenes"]
        no_tpl_id = None
        for s in scenes:
            detail = client.get(f"/api/scenes/market/{s['id']}", headers=_headers).json()
            if not detail.get("recommended_template"):
                no_tpl_id = s["id"]
                break
        if no_tpl_id:
            resp = client.post(f"/api/scenes/market/{no_tpl_id}/experience", headers=_headers)
            assert resp.status_code == 400

    def test_experience_detail_skills_format(self):
        """验证 get_scene_market_detail 的 skills_detail 批量查询结果格式正确"""
        resp = client.get("/api/scenes/market", headers=_headers)
        scene_id = resp.json()["scenes"][0]["id"]
        resp = client.get(f"/api/scenes/market/{scene_id}", headers=_headers)
        assert resp.status_code == 200
        detail = resp.json()
        skills = detail.get("skills", [])
        for sk in skills:
            assert "skill_id" in sk
            assert "name" in sk
            assert "display_name" in sk
            assert "agent_type" in sk
            assert "role_desc" in sk


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


# ===== Scene Market CRUD (Issue #54) =====

class TestSceneMarketCRUD:
    """场景市场创建/更新/删除/重名检查测试（Issue #54）"""

    def test_create_scene(self):
        """创建仓库场景"""
        body = {
            "name": "test-crud-scene",
            "display_name": "测试CRUD场景",
            "description": "测试场景描述",
            "scope": "repo",
            "nodes": [{"id": "vod", "tag": "vod", "skill": "", "skill_id": 0, "enabled": True}],
            "category_id": 1,
        }
        resp = client.post("/api/scenes/market?repo=test/test-repo", headers=_headers, json=body)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data.get("id") > 0
        assert data.get("message") == "场景创建成功"

    def test_create_scene_duplicate_name(self):
        """重复 name 返回 409"""
        body = {
            "name": "test-crud-scene",
            "display_name": "重复场景",
            "scope": "repo",
            "nodes": [],
            "category_id": 1,
        }
        resp = client.post("/api/scenes/market?repo=test/test-repo", headers=_headers, json=body)
        assert resp.status_code == 409

    def test_check_scene_name_exists(self):
        """检查场景名是否存在"""
        resp = client.get("/api/scenes/check-name?repo=test/test-repo&name=test-crud-scene&scope=repo", headers=_headers)
        assert resp.status_code == 200
        assert resp.json().get("exists") is True

    def test_check_scene_name_available(self):
        """检查不存在的场景名"""
        resp = client.get("/api/scenes/check-name?repo=test/test-repo&name=nonexistent-xyz-123&scope=repo", headers=_headers)
        assert resp.status_code == 200
        assert resp.json().get("exists") is False

    def test_update_scene(self):
        """更新场景"""
        create_body = {
            "name": "test-crud-update",
            "display_name": "更新前",
            "scope": "repo",
            "nodes": [],
            "category_id": 1,
        }
        resp = client.post("/api/scenes/market?repo=test/test-repo", headers=_headers, json=create_body)
        scene_id = resp.json().get("id")

        update_body = {
            "name": "test-crud-update",
            "display_name": "更新后",
            "description": "更新后的描述",
            "scope": "repo",
            "nodes": [],
            "category_id": 1,
        }
        resp = client.put(f"/api/scenes/market/{scene_id}?repo=test/test-repo", headers=_headers, json=update_body)
        assert resp.status_code == 200
        assert resp.json().get("message") == "场景更新成功"

    def test_delete_scene(self):
        """删除场景"""
        create_body = {
            "name": "test-crud-delete",
            "display_name": "待删除",
            "scope": "repo",
            "nodes": [],
            "category_id": 1,
        }
        resp = client.post("/api/scenes/market?repo=test/test-repo", headers=_headers, json=create_body)
        scene_id = resp.json().get("id")

        resp = client.delete(f"/api/scenes/market/{scene_id}", headers=_headers)
        assert resp.status_code == 200

        resp = client.get(f"/api/scenes/market/{scene_id}", headers=_headers)
        assert resp.status_code == 404

    def test_delete_official_scene_forbidden(self):
        """删除 official 场景被拒绝"""
        resp = client.get("/api/scenes/market?scope=official&page_size=1", headers=_headers)
        scenes = resp.json().get("scenes", [])
        if scenes:
            scene_id = scenes[0]["id"]
            resp = client.delete(f"/api/scenes/market/{scene_id}", headers=_headers)
            assert resp.status_code in (403, 404)


# ===== list_skills_for_agent (Issue #54) =====

class TestListSkillsForAgent:
    """技能选择接口测试（Issue #54）"""

    def test_list_skills_official(self):
        """列出 official 技能"""
        resp = client.get("/api/scenes/skills/vod?scope=official&page_size=5", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data
        assert "total" in data

    def test_list_skills_with_search(self):
        """搜索技能"""
        resp = client.get("/api/scenes/skills/vod?q=blog&page_size=5", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        if data["total"] > 0:
            assert "display_name" in data["skills"][0]

    def test_list_skills_with_scope_filter(self):
        """scope 过滤只返回对应范围"""
        resp = client.get("/api/scenes/skills/vod?scope=official&page_size=20", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        for sk in data.get("skills", []):
            assert sk.get("scope") == "official"

    def test_list_skills_pagination(self):
        """分页参数正确"""
        resp1 = client.get("/api/scenes/skills/vod?page=1&page_size=2", headers=_headers)
        assert resp1.status_code == 200
        d1 = resp1.json()
        assert len(d1.get("skills", [])) <= 2

        resp2 = client.get("/api/scenes/skills/vod?page=2&page_size=2", headers=_headers)
        assert resp2.status_code == 200
        d2 = resp2.json()
        ids1 = {s["skill_name"] for s in d1.get("skills", [])}
        ids2 = {s["skill_name"] for s in d2.get("skills", [])}
        assert not (ids1 & ids2)
