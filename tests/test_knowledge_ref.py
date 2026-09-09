"""Issue #67 项目知识库优化 — 单元测试

测试覆盖：
- scene_knowledge_ref 表创建 + 唯一约束
- 知识 CRUD（POST /entry, GET /{id}, PUT /{id}, DEL /{id}）
- 关联管理（GET/POST /{id}/refs, PUT/DEL /ref/{id}）
- source 权限校验（system/agent 只读，manual 可编辑/删除）
- prompt_builder 注入（查 scene_knowledge_ref + 返回值类型）
- 归档 API
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.api.main import app  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402
from src.user import db as user_db  # noqa: E402

# JWT access token helper
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


client = TestClient(app)

TEST_REPO = "gitcode:test-org67/test-repo"
_uid = None


@pytest.fixture(scope="module", autouse=True)
def setup_test_user():
    """创建测试用户。"""
    global _uid
    user_db.create_user("test_user67", "test123")
    _uid = user_db.get_user_by_username("test_user67")
    yield
    # 清理
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key=?)", (TEST_REPO,))
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (TEST_REPO,))
        db.commit()


@pytest.fixture
def auth_headers():
    token = _jwt_access_token(_uid["id"], _uid["username"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def test_knowledge():
    """插入测试知识并返回 id。"""
    with db_lock:
        c = db.cursor()
        # 给测试用户加仓库托管者记录（system/agent 知识关联管理需要）
        from src.repo_key import from_repo_key
        c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (_uid["id"], TEST_REPO))
        c.execute(
            "INSERT INTO user_repos (user_id, repo_key, owner, name, description, role, is_admin, is_join, created_at) "
            "VALUES (?, ?, 'test-org67', 'test-repo', '', 'admin', 1, 1, datetime('now'))",
            (_uid["id"], TEST_REPO)
        )
        # manual 知识
        c.execute(
            "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, archived) "
            "VALUES ('member', ?, '', 0, 'project-info/member/test_user67/test.md', '', '', 'accepted', 0, 'test_user67', 'manual', 0)",
            (TEST_REPO,)
        )
        manual_id = c.lastrowid

        # agent 知识（project 级）
        c.execute(
            "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, archived) "
            "VALUES ('project', ?, '', 0, 'project-info/knowledge.md', '', '', 'accepted', 0, 'agent', 'agent', 0)",
            (TEST_REPO,)
        )
        agent_id = c.lastrowid

        # system 知识（baseline）
        c.execute(
            "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, archived) "
            "VALUES ('baseline', ?, '', 0, 'project-info/project-graph.md', '', '', 'accepted', 0, 'vod', 'system', 0)",
            (TEST_REPO,)
        )
        system_id = c.lastrowid

        # 为 agent 知识生成关联记录
        c.execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by) "
            "VALUES (?, '', '', 1, 'system')",
            (agent_id,)
        )
        db.commit()

    yield {"repo_key": TEST_REPO, "manual_id": manual_id, "agent_id": agent_id, "system_id": system_id}

    # 清理
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key=?)", (TEST_REPO,))
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (TEST_REPO,))
        db.commit()


class TestKnowledgeRefTable:
    """scene_knowledge_ref 表结构测试"""

    def test_table_exists(self):
        with db_lock:
            row = db.cursor().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scene_knowledge_ref'"
            ).fetchone()
        assert row is not None, "scene_knowledge_ref table should exist"

    def test_unique_constraint(self, test_knowledge):
        """三元组唯一约束——重复插入报错"""
        import sqlite3
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by) "
                "VALUES (?, 'scene1', 'coding', 1, 'test')",
                (test_knowledge["manual_id"],)
            )
            db.commit()
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, enabled, created_by) "
                    "VALUES (?, 'scene1', 'coding', 1, 'test')",
                    (test_knowledge["manual_id"],)
                )
            c.execute("DELETE FROM scene_knowledge_ref WHERE scene_id='scene1' AND knowledge_id=?", (test_knowledge["manual_id"],))
            db.commit()


class TestKnowledgeCRUD:
    """知识 CRUD API 测试"""

    def test_get_detail(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge/{test_knowledge['manual_id']}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == test_knowledge["manual_id"]
        assert data["source"] == "manual"

    def test_get_detail_not_found(self, auth_headers):
        r = client.get("/api/knowledge/99999", headers=auth_headers)
        assert r.status_code == 404

    def test_update_manual(self, auth_headers, test_knowledge):
        r = client.put(f"/api/knowledge/{test_knowledge['manual_id']}",
                       json={"content": "updated content"}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_update_system_forbidden(self, auth_headers, test_knowledge):
        r = client.put(f"/api/knowledge/{test_knowledge['system_id']}",
                       json={"content": "hacked"}, headers=auth_headers)
        assert r.status_code == 403

    def test_update_agent_forbidden(self, auth_headers, test_knowledge):
        r = client.put(f"/api/knowledge/{test_knowledge['agent_id']}",
                       json={"content": "hacked"}, headers=auth_headers)
        assert r.status_code == 403

    def test_delete_manual(self, auth_headers, test_knowledge):
        """创建一个新的 manual 知识然后删除"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, archived) "
                "VALUES ('member', ?, '', 0, 'project-info/member/test_user67/delete_me.md', '', '', 'accepted', 0, 'test_user67', 'manual', 0)",
                (TEST_REPO,)
            )
            del_id = c.lastrowid
            db.commit()
        r = client.delete(f"/api/knowledge/{del_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_delete_system_forbidden(self, auth_headers, test_knowledge):
        r = client.delete(f"/api/knowledge/{test_knowledge['system_id']}", headers=auth_headers)
        assert r.status_code == 403


class TestKnowledgeRef:
    """关联管理 API 测试"""

    def test_get_refs(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge/{test_knowledge['agent_id']}/refs", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "refs" in data
        assert len(data["refs"]) >= 1

    def test_add_ref(self, auth_headers, test_knowledge):
        r = client.post(f"/api/knowledge/{test_knowledge['manual_id']}/refs",
                        json={"scene_id": "test-scene-uuid", "agent_type": "coding"},
                        headers=auth_headers)
        assert r.status_code == 200

    def test_add_ref_duplicate(self, auth_headers, test_knowledge):
        client.post(f"/api/knowledge/{test_knowledge['manual_id']}/refs",
                    json={"scene_id": "dup-scene", "agent_type": "qa"},
                    headers=auth_headers)
        r = client.post(f"/api/knowledge/{test_knowledge['manual_id']}/refs",
                        json={"scene_id": "dup-scene", "agent_type": "qa"},
                        headers=auth_headers)
        assert r.status_code == 409

    def test_toggle_ref(self, auth_headers, test_knowledge):
        client.post(f"/api/knowledge/{test_knowledge['manual_id']}/refs",
                    json={"scene_id": "toggle-scene", "agent_type": ""},
                    headers=auth_headers)
        r2 = client.get(f"/api/knowledge/{test_knowledge['manual_id']}/refs", headers=auth_headers)
        ref_id = None
        for ref in r2.json()["refs"]:
            if ref.get("scene_id") == "toggle-scene":
                ref_id = ref["id"]
                break
        assert ref_id is not None
        r3 = client.put(f"/api/knowledge/ref/{ref_id}", json={"enabled": False}, headers=auth_headers)
        assert r3.status_code == 200

    def test_delete_ref(self, auth_headers, test_knowledge):
        client.post(f"/api/knowledge/{test_knowledge['manual_id']}/refs",
                    json={"scene_id": "del-scene", "agent_type": "sre"},
                    headers=auth_headers)
        r = client.get(f"/api/knowledge/{test_knowledge['manual_id']}/refs", headers=auth_headers)
        ref_id = None
        for ref in r.json()["refs"]:
            if ref.get("scene_id") == "del-scene":
                ref_id = ref["id"]
                break
        assert ref_id is not None
        r2 = client.delete(f"/api/knowledge/ref/{ref_id}", headers=auth_headers)
        assert r2.status_code == 200


class TestKnowledgeList:
    """知识列表 API 测试"""

    def test_list_returns_unified(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge?repo={TEST_REPO}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "knowledge" in data
        assert "total" in data
        assert isinstance(data["knowledge"], list)

    def test_stats_independent(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge/stats?repo={TEST_REPO}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "system" in data
        assert "agent" in data
        assert "manual" in data

    def test_list_has_source_field(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge?repo={TEST_REPO}", headers=auth_headers)
        data = r.json()
        for k in data["knowledge"]:
            assert "source" in k

    def test_list_has_refs(self, auth_headers, test_knowledge):
        r = client.get(f"/api/knowledge?repo={TEST_REPO}", headers=auth_headers)
        data = r.json()
        for k in data["knowledge"]:
            if k.get("id") and k["id"] > 0:
                assert "refs" in k
                assert "ref_summary" in k


class TestArchive:
    """归档 API 测试"""

    def test_archive_knowledge(self, auth_headers, test_knowledge):
        r = client.put(f"/api/knowledge/{test_knowledge['agent_id']}/archive",
                       json={"archived": True}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["archived"] in (1, True)

    def test_unarchive_knowledge(self, auth_headers, test_knowledge):
        client.put(f"/api/knowledge/{test_knowledge['agent_id']}/archive",
                   json={"archived": True}, headers=auth_headers)
        r = client.put(f"/api/knowledge/{test_knowledge['agent_id']}/archive",
                       json={"archived": False}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["archived"] in (0, False)


class TestPromptBuilder:
    """prompt_builder 注入逻辑测试"""

    def test_inject_by_ref(self, test_knowledge):
        from src.prompt_builder import _build_knowledge_section
        text, ids = _build_knowledge_section(
            test_knowledge["repo_key"], agent_type="", issue_num=0, scene_id=""
        )
        assert isinstance(text, str)
        assert isinstance(ids, list)

    def test_inject_empty_repo(self):
        from src.prompt_builder import _build_knowledge_section
        text, ids = _build_knowledge_section(
            "gitcode:empty/empty", agent_type="", issue_num=0, scene_id=""
        )
        assert isinstance(text, str)
        assert isinstance(ids, list)

    def test_return_type(self, test_knowledge):
        from src.prompt_builder import _build_knowledge_section
        result = _build_knowledge_section(
            test_knowledge["repo_key"], agent_type="", issue_num=0, scene_id=""
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], list)
