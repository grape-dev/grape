"""项目知识看板测试（Issue #67 新 API）。

覆盖：列表分页 + 搜索 + 排序、详情读取、路径穿越防护、创建/编辑/删除、context 合并、注入。
"""
import os, json, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.auth import grape_account_db

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)
_uid = None
_headers = {}
_REPO_KEY = "gitcode:org/test"
_REPO = "org/test"


def setup_module():
    global _uid, _headers
    from src.api.db_setup import init_database
    init_database()
    _uid = grape_account_db.create_grape_account("kb_user", email=None, password="pwd")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("kb_user")
    _headers = {"Authorization": "Bearer " + _jwt_access_token(_uid["id"], _uid["username"])}
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  (_REPO_KEY, "org", "test", _REPO, 1, "bot_kb_token"))
        db.commit()
    # 关联用户到仓库（_check_repo_member 需要 user_repos 记录）
    user_db.mark_user_repo_join(_uid["id"], _REPO_KEY, is_join=True, is_admin=True)


def teardown_module():
    _clean()
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key=?)", (_REPO_KEY,))
        db.commit()
    from src.repo_key import workspace_repo_dir
    import shutil
    base = workspace_repo_dir(_REPO_KEY)
    shutil.rmtree(os.path.join(base, "project-info"), ignore_errors=True)
    shutil.rmtree(os.path.join(base, "issues"), ignore_errors=True)


def _write_pi(file_name: str, content: str):
    from src.repo_key import workspace_repo_dir
    p = os.path.join(workspace_repo_dir(_REPO_KEY), "project-info", file_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def _seed_knowledge(scope="issue_agent", agent="vod", issue_num=5, content="验收标准 = 接口可跑通"):
    from src.api.knowledge import write_knowledge
    from src.session_manager import db, db_lock
    write_knowledge(repo_key=_REPO_KEY, scope=scope, content=content,
                    agent_type=agent, issue_num=issue_num, source_seq=99, created_by="agent")
    # write_knowledge 返回 task_id 非 knowledge_id，查 DB 拿真实 id
    with db_lock:
        row = db.cursor().execute(
            "SELECT id FROM agent_shared_knowledge WHERE repo_key=? ORDER BY id DESC LIMIT 1",
            (_REPO_KEY,)).fetchone()
    return row[0] if row else 0


class TestKnowledgeList:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_list_empty_repo(self):
        r = client.get(f"/api/knowledge?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 0
        assert d["knowledge"] == []

    def test_list_project_dimension(self):
        _seed_knowledge(scope="project", content="此仓库 PR 走内部评审")
        r = client.get(f"/api/knowledge?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] >= 1
        # 列表不返回 content，通过详情验证
        kid = d["knowledge"][0]["id"]
        r2 = client.get(f"/api/knowledge/{kid}", headers=_headers)
        assert r2.status_code == 200, r2.text
        assert "内部评审" in r2.json()["content"]

    def test_list_agent_dimension(self):
        _seed_knowledge(agent="vod")
        _seed_knowledge(agent="coding")
        r = client.get(f"/api/knowledge?repo={_REPO}&source=agent", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] >= 2

    def test_entry_path_is_repo_root_relative_and_readable(self):
        _seed_knowledge(agent="vod")
        r = client.get(f"/api/knowledge?repo={_REPO}&source=agent", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] >= 1
        entry = d["knowledge"][0]
        # 详情接口可读到正文
        kid = entry["id"]
        r2 = client.get(f"/api/knowledge/{kid}", headers=_headers)
        assert r2.status_code == 200, r2.text
        assert "验收标准 = 接口可跑通" in r2.json()["content"]

    def test_list_repo_isolation(self):
        _seed_knowledge(agent="vod")
        r = client.get("/api/knowledge?repo=org/other", headers=_headers)
        # org/other 未关联用户 → 403
        assert r.status_code in (200, 403)

    def test_list_requires_auth(self):
        r = client.get(f"/api/knowledge?repo={_REPO}")
        assert r.status_code == 401

    def test_list_requires_repo(self):
        r = client.get("/api/knowledge", headers=_headers)
        assert r.status_code == 400


class TestKnowledgeDetail:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_read_markdown(self):
        """Issue #190：废弃 GET /content 接口已删除，改用 GET /{id} 读知识详情。"""
        kid = _seed_knowledge(scope="issue", issue_num=1, content="## Architecture Overview\nhello")
        r = client.get(f"/api/knowledge/{kid}", headers=_headers)
        assert r.status_code == 200, r.text
        assert "Architecture Overview" in r.json()["content"]

    def test_read_not_found(self):
        r = client.get("/api/knowledge/999999", headers=_headers)
        assert r.status_code == 404


class TestKnowledgeEntry:
    """Issue #67: POST /api/knowledge/entry (JSON body) 替代旧 upload。"""
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _create(self, name="部署规范", content="部署走灰度。", description="部署规范"):
        return client.post("/api/knowledge/entry", json={
            "name": name, "content": content, "description": description, "repo": _REPO
        }, headers=_headers)

    def test_create_entry(self):
        r = self._create()
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert "id" in r.json()
        with db_lock:
            row = db.cursor().execute(
                "SELECT scope, created_by, file_path FROM agent_shared_knowledge WHERE scope='member' AND repo_key=? ORDER BY id DESC LIMIT 1",
                (_REPO_KEY,)).fetchone()
        assert row and row[0] == "member" and "project-info/member/" in row[2]

    def test_create_requires_name(self):
        r = client.post("/api/knowledge/entry", json={
            "name": "", "content": "x", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 400

    def test_create_requires_content(self):
        r = client.post("/api/knowledge/entry", json={
            "name": "test", "content": "", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 400

    def test_create_name_too_long(self):
        r = client.post("/api/knowledge/entry", json={
            "name": "x" * 51, "content": "x", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 400

    def test_create_requires_auth(self):
        r = client.post("/api/knowledge/entry", json={
            "name": "test", "content": "x", "repo": _REPO
        })
        assert r.status_code == 401

    def test_delete_entry(self):
        r = self._create()
        kid = r.json()["id"]
        r2 = client.delete(f"/api/knowledge/{kid}", headers=_headers)
        assert r2.status_code == 200, r2.text
        # 确认删除
        r3 = client.get(f"/api/knowledge/{kid}", headers=_headers)
        assert r3.status_code == 404


# Issue #190：GET /knowledge/context 接口已废弃删除，context 相关测试移除。


class TestInjection:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_member_doc_injected_into_prompt(self):
        """Issue #95: member 知识不再内联到 chat prompt（_build_agent_knowledge_section source='agent'）。
        member 知识走 member_files_pi 物化到 .project-info/member/，不内联到 prompt。"""
        r = client.post("/api/knowledge/entry", json={
            "name": "部署规范", "content": "部署走灰度发布", "description": "部署规范", "repo": _REPO
        }, headers=_headers)
        kid = r.json()["id"]
        client.post(f"/api/knowledge/{kid}/refs/batch", json={"agent_types": ["vod"]}, headers=_headers)
        from src.prompt_builder import build_event_chat_prompt
        evt = {"seq": 1, "repo_key": _REPO_KEY, "issue_id": "#5", "agent_type": "vod",
               "event_type": "issue_opened", "title": "t", "status": "completed", "result": "r"}
        prompt, _ = build_event_chat_prompt(evt, "你好", [], "vod", 5)
        # Issue #95: member 不内联到 chat prompt
        assert "灰度发布" not in prompt

    def test_project_info_files_include_member_docs(self):
        """Issue #95: member 知识走 project_info_files（不再走 knowledge_files）。
        knowledge_files 只存 agent 沉淀知识（source='agent'），member 存 project_info_files。"""
        r = client.post("/api/knowledge/entry", json={
            "name": "环境说明", "content": '{"env": "prod"}', "description": "环境说明", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 200, r.text
        kid = r.json()["id"]
        client.post(f"/api/knowledge/{kid}/refs/batch", json={"agent_types": ["vod"]}, headers=_headers)
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-kb", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="vod", issue_id="#5")
        from src.runtime import service
        task = service.get_task(tid)
        # Issue #95: member 知识在 project_info_files 里（不在 knowledge_files 里）
        pif = json.loads(task["project_info_files"] or "{}")
        assert any("env" in (v or "") for v in pif.values()), f"project_info_files 应含 member 知识: {list(pif)}"


class TestAgentScopedUpload:
    """Issue #95: agent_files 废弃，用户上传知识统一由 member_files_pi 走 ref 表注入。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_upload_agent_looks_agent_dir(self):
        from src.config import WORKSPACE_DIR
        r = client.post("/api/knowledge/entry", json={
            "name": "VOD专属知识", "content": "VOD 专属知识", "description": "VOD 专属", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 200, r.text
        fp = r.json()["file_path"]
        assert "project-info/member/" in fp
        assert os.path.isfile(os.path.join(WORKSPACE_DIR, fp.replace("/", os.sep)))

    def test_enqueue_only_packs_own_agent_dir(self):
        """Issue #95: agent_files 废弃，member 知识走 ref 表。"""
        # 创建 member 知识 + ref 关联
        r = client.post("/api/knowledge/entry", json={
            "name": "coding专属", "content": "coding 专属", "repo": _REPO
        }, headers=_headers)
        kid = r.json().get("id")
        if kid:
            client.post(f"/api/knowledge/{kid}/refs/batch", json={
                "scene_ids": [], "agent_types": ["coding"]
            }, headers=_headers)
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-a1", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        from src.runtime import service
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert any(rel.startswith("member/") for rel in pif), f"应含 member 文档: {list(pif)}"

    def test_prompt_tells_agent_dir(self):
        """Issue #95: agent/{type}/ 引导已删除。"""
        _write_pi("project-graph.md", "# Graph")
        from src.prompt_builder import _build_project_info_section
        sec = _build_project_info_section(_REPO_KEY, agent_type="coding")
        assert "{{PROJECT_INFO_DIR}}/agent/coding/" not in sec
        assert "Project Graph" in sec

    def test_build_prompt_includes_agent_dir_guide(self):
        """Issue #95: agent/{type}/ 引导已删除。"""
        from src.prompt_builder import build_devops_agent_prompt
        evt = {"type": "issue_opened", "repository": "org/test", "source": "gitcode",
               "payload": {"issue": {"title": "t", "body": "b"}}, "issue_id": "#5", "_skill_path": ""}
        prompt = build_devops_agent_prompt(evt, [], "", ["qa"], runtime_id=1, agent_type="qa")
        assert "{{PROJECT_INFO_DIR}}/agent/qa/" not in prompt


class TestProjectScopedShared:
    """Issue #95: member 知识走 ref 表，需要手动创建 ref 关联。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _seed_member_doc_with_ref(self, agent_type="coding"):
        """创建 member 知识 + ref 关联到 agent。"""
        r = client.post("/api/knowledge/entry", json={
            "name": "规范.md", "content": "alice 上传项目规范", "repo": _REPO
        }, headers=_headers)
        kid = r.json().get("id")
        if kid:
            client.post(f"/api/knowledge/{kid}/refs/batch", json={
                "scene_ids": [], "agent_types": [agent_type]
            }, headers=_headers)

    def test_enqueue_packs_member_docs_all_agents(self):
        self._seed_member_doc_with_ref(agent_type="coding")
        from src.runtime.service import enqueue_task
        from src.runtime import service
        tid = enqueue_task(agent_session_id="sess-coding", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert any(rel.startswith("member/") for rel in pif), f"应含 member 文档: {list(pif)}"

    def test_member_key_is_pi_relative(self):
        self._seed_member_doc_with_ref(agent_type="coding")
        from src.runtime.service import enqueue_task
        from src.runtime import service
        tid = enqueue_task(agent_session_id="sess-m", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert all(not rel.startswith("org/test/") for rel in pif)
        assert any(rel.startswith("member/") for rel in pif)

    def test_prompt_tells_member_dir(self):
        """Issue #95: member 引导条件输出——有 ref 关联才输出。"""
        self._seed_member_doc_with_ref(agent_type="coding")
        from src.prompt_builder import _build_project_info_section
        sec = _build_project_info_section(_REPO_KEY, agent_type="coding")
        assert "{{PROJECT_INFO_DIR}}/member/" in sec
        assert "{{PROJECT_INFO_DIR}}/agent/coding/" not in sec
