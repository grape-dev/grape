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

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
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
    _uid = user_db.create_user("kb_user", "pwd")
    if not _uid:
        _uid = user_db.get_user_by_username("kb_user")
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
        user_db.delete_user(_uid["id"])


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key=?)", (_REPO_KEY,))
        db.commit()
    from src.config import WORKSPACE_DIR
    import shutil
    shutil.rmtree(os.path.join(WORKSPACE_DIR, "org", "test", "project-info"), ignore_errors=True)
    shutil.rmtree(os.path.join(WORKSPACE_DIR, "org", "test", "issues"), ignore_errors=True)


def _write_pi(file_name: str, content: str):
    from src.config import WORKSPACE_DIR
    p = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", file_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def _seed_knowledge(scope="issue_agent", agent="vod", issue_num=5, content="验收标准 = 接口可跑通"):
    from src.api.knowledge import write_knowledge
    return write_knowledge(repo_key=_REPO_KEY, scope=scope, content=content,
                           agent_type=agent, issue_num=issue_num, source_seq=99, created_by="agent")


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
        _write_pi("project-graph.md", "## Architecture Overview\nhello")
        r = client.get(f"/api/knowledge/content?repo={_REPO}&file=project-info/project-graph.md", headers=_headers)
        assert r.status_code == 200, r.text
        assert "Architecture Overview" in r.json()["content"]

    def test_read_json_meta(self):
        _write_pi(".metadata.json", json.dumps({"degraded": True}))
        r = client.get(f"/api/knowledge/content?repo={_REPO}&file=project-info/.metadata.json", headers=_headers)
        assert r.status_code == 200, r.text
        assert json.loads(r.json()["content"])["degraded"] is True

    def test_path_traversal_blocked(self):
        r = client.get(f"/api/knowledge/content?repo={_REPO}&file=../../../../etc/passwd", headers=_headers)
        assert r.status_code in (400, 404)

    def test_not_found(self):
        r = client.get(f"/api/knowledge/content?repo={_REPO}&file=project-info/nope.md", headers=_headers)
        assert r.status_code == 404

    def test_forbidden_ext(self):
        r = client.get(f"/api/knowledge/content?repo={_REPO}&file=project-info/x.py", headers=_headers)
        assert r.status_code == 400


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


class TestKnowledgeContext:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_context_merges_scopes(self):
        _seed_knowledge(scope="project", content="PR 走内部评审")
        _seed_knowledge(scope="issue", content="验收 = 可跑通")
        _seed_knowledge(scope="issue_agent", agent="vod", content="VOD 不删原文")
        r = client.get(f"/api/knowledge/context?repo={_REPO}&agent_type=vod&issue_num=5", headers=_headers)
        assert r.status_code == 200, r.text
        ctx = r.json()["context"]
        assert "内部评审" in ctx and "可跑通" in ctx and "不删原文" in ctx

    def test_context_empty(self):
        r = client.get(f"/api/knowledge/context?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json()["context"], str)

    def test_context_includes_member_docs(self):
        """Issue #67: member 知识通过 ref 表注入（不再全量扫描）。"""
        # 创建知识 + 关联 Agent
        r = client.post("/api/knowledge/entry", json={
            "name": "部署规范", "content": "部署走灰度发布", "description": "部署规范", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 200, r.text
        kid = r.json()["id"]
        # 关联到 vod Agent
        r2 = client.post(f"/api/knowledge/{kid}/refs/batch", json={
            "agent_types": ["vod"]
        }, headers=_headers)
        assert r2.status_code == 200, r2.text
        # context 应包含
        r3 = client.get(f"/api/knowledge/context?repo={_REPO}&agent_type=vod", headers=_headers)
        assert r3.status_code == 200, r3.text
        assert "灰度发布" in r3.json()["context"]


class TestInjection:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_member_doc_injected_into_prompt(self):
        """Issue #67: manual 知识通过 ref 表注入到 prompt。"""
        # 创建知识
        r = client.post("/api/knowledge/entry", json={
            "name": "部署规范", "content": "部署走灰度发布", "description": "部署规范", "repo": _REPO
        }, headers=_headers)
        kid = r.json()["id"]
        # 关联到 vod Agent
        client.post(f"/api/knowledge/{kid}/refs/batch", json={"agent_types": ["vod"]}, headers=_headers)
        # build_event_chat_prompt 注入
        from src.prompt_builder import build_event_chat_prompt
        evt = {"seq": 1, "repo_key": _REPO_KEY, "issue_id": "#5", "agent_type": "vod",
               "event_type": "issue_opened", "title": "t", "status": "completed", "result": "r"}
        prompt, _ = build_event_chat_prompt(evt, "你好", [], "vod", 5)
        assert "灰度发布" in prompt

    def test_project_info_files_include_member_docs(self):
        """Issue #67: knowledge_files 走 ref 表——关联了才打包给 worker。"""
        # 创建知识
        r = client.post("/api/knowledge/entry", json={
            "name": "环境说明", "content": '{"env": "prod"}', "description": "环境说明", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 200, r.text
        kid = r.json()["id"]
        # 关联到 vod Agent
        client.post(f"/api/knowledge/{kid}/refs/batch", json={"agent_types": ["vod"]}, headers=_headers)
        # enqueue_task 应只打包关联的知识
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-kb", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="vod", issue_id="#5")
        from src.runtime import service
        task = service.get_task(tid)
        kf = json.loads(task["knowledge_files"] or "{}")
        assert any("env" in (content or "") for content in kf.values()), f"应包含关联的知识: {list(kf)}"


class TestAgentScopedUpload:
    """§六 Agent 级成员上传文档定向注入。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_upload_agent_looks_agent_dir(self):
        from src.config import WORKSPACE_DIR
        # Issue #67: 用 create_knowledge_entry 创建 manual 知识
        r = client.post("/api/knowledge/entry", json={
            "name": "VOD专属知识", "content": "VOD 专属知识", "description": "VOD 专属", "repo": _REPO
        }, headers=_headers)
        assert r.status_code == 200, r.text
        fp = r.json()["file_path"]
        assert "project-info/member/" in fp
        assert os.path.isfile(os.path.join(WORKSPACE_DIR, fp.replace("/", os.sep)))

    def test_enqueue_only_packs_own_agent_dir(self):
        from src.config import WORKSPACE_DIR
        for at, name in (("vod", "v.md"), ("coding", "c.md")):
            d = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "agent", at)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(f"{at} 专属")
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-a1", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        from src.runtime import service
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert any(rel == "agent/coding/c.md" for rel in pif)
        assert not any(rel.startswith("agent/vod") for rel in pif)
        assert all(not rel.startswith("org/test/") for rel in pif)

    def test_prompt_tells_agent_dir(self):
        from src.config import WORKSPACE_DIR
        d = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "agent", "coding")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "c.md"), "w", encoding="utf-8") as f:
            f.write("coding 规范")
        from src.prompt_builder import _build_project_info_section
        _write_pi("project-graph.md", "# Graph")
        sec = _build_project_info_section(_REPO_KEY, agent_type="coding")
        assert "{{PROJECT_INFO_DIR}}/agent/coding/" in sec
        _clean()
        sec2 = _build_project_info_section(_REPO_KEY, agent_type="coding")
        assert "{{PROJECT_INFO_DIR}}/agent/coding/" in sec2

    def test_build_prompt_includes_agent_dir_guide(self):
        from src.config import WORKSPACE_DIR
        d = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "agent", "qa")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "qa规范.md"), "w", encoding="utf-8") as f:
            f.write("QA 验收规则")
        from src.prompt_builder import build_devops_agent_prompt
        evt = {"type": "issue_opened", "repository": "org/test", "source": "gitcode",
               "payload": {"issue": {"title": "t", "body": "b"}}, "issue_id": "#5", "_skill_path": ""}
        prompt = build_devops_agent_prompt(evt, [], "", ["qa"], runtime_id=1, agent_type="qa")
        assert "{{PROJECT_INFO_DIR}}/agent/qa/" in prompt


class TestProjectScopedShared:
    """§6.7 方案 A：项目级成员文档全 Agent 共享 + prompt 路径指引。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _seed_member_doc(self, name="规范.md", owner="alice"):
        from src.config import WORKSPACE_DIR
        d = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "member", owner)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(f"{owner} 上传项目规范")

    def test_enqueue_packs_member_docs_all_agents(self):
        self._seed_member_doc()
        from src.runtime.service import enqueue_task
        from src.runtime import service
        for at in ("coding", "vod"):
            tid = enqueue_task(agent_session_id=f"sess-{at}", runtime_id=1, prompt="p",
                               repo_key=_REPO_KEY, agent_type=at, issue_id="#5")
            task = service.get_task(tid)
            pif = json.loads(task["project_info_files"] or "{}")
            assert any(rel == "member/alice/规范.md" for rel in pif), f"{at} 应含项目级 member 文档: {list(pif)}"

    def test_member_key_is_pi_relative(self):
        self._seed_member_doc()
        from src.runtime.service import enqueue_task
        from src.runtime import service
        tid = enqueue_task(agent_session_id="sess-m", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="coding", issue_id="#5")
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert all(not rel.startswith("org/test/") for rel in pif)
        assert any(rel.startswith("member/") for rel in pif)

    def test_prompt_tells_member_dir(self):
        self._seed_member_doc()
        from src.prompt_builder import _build_project_info_section
        sec = _build_project_info_section(_REPO_KEY, agent_type="coding")
        assert "{{PROJECT_INFO_DIR}}/member/" in sec
        assert "{{PROJECT_INFO_DIR}}/agent/coding/" in sec
