"""项目知识看板测试（见 knowledge-board-design.md §7.1）。

覆盖：聚合查询（项目级 + Agent 维度）、详情读取、路径穿越防护、成员上传/删除、context 合并、注入补强。
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
    _headers = {"Authorization": "Bearer " + user_db.create_session_token(_uid["id"])}
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  (_REPO_KEY, "org", "test", _REPO, 1, "bot_kb_token"))
        db.commit()


def teardown_module():
    _clean()
    if _uid:
        user_db.delete_user(_uid["id"])


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (_REPO_KEY,))
        db.commit()
    from src.config import WORKSPACE_DIR
    import shutil
    shutil.rmtree(os.path.join(WORKSPACE_DIR, "org", "test", "project-info"), ignore_errors=True)


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
        assert d["project"]["files"] == []
        assert d["project"]["knowledge"] == []
        assert d["project"]["member_docs"] == []
        assert d["agents"] == {}

    def test_list_project_dimension(self):
        _write_pi("project-graph.md", "# Project Graph")
        _write_pi("project-capabilities.md", "# Project Capabilities")
        _write_pi(".metadata.json", json.dumps({"last_updated": "2026-08-17T10:00:00", "degraded": False, "last_commit_sha": "abc"}))
        _seed_knowledge(scope="project", content="此仓库 PR 走内部评审")
        r = client.get(f"/api/knowledge?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        names = [f["name"] for f in d["project"]["files"]]
        assert "project-info/project-graph.md" in names
        assert "project-info/project-capabilities.md" in names
        assert "project-info/.metadata.json" in names
        assert d["project"]["meta"]["degraded"] is False
        assert any("内部评审" in k["content"] for k in d["project"]["knowledge"])
        # §5.5 修复：知识沉淀条目的 path 为相对仓库根，content 接口可直接读
        pentry = next(k for k in d["project"]["knowledge"] if "内部评审" in k["content"])
        assert pentry["path"] == "project-info/knowledge.md"
        r2 = client.get(f"/api/knowledge/content?repo={_REPO}&file={pentry['path']}", headers=_headers)
        assert r2.status_code == 200, r2.text
        assert "内部评审" in r2.json()["content"]

    def test_list_agent_dimension(self):
        _seed_knowledge(agent="vod")
        _seed_knowledge(agent="coding")
        r = client.get(f"/api/knowledge?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "vod" in d["agents"] and "coding" in d["agents"]
        assert d["agents"]["vod"][0]["issue_num"] == 5
        assert d["agents"]["vod"][0]["scope"] == "issue_agent"

    def test_entry_path_is_repo_root_relative_and_readable(self):
        """知识条目 path 为「相对仓库根」，content 接口可直接读取（§5.5 修复）。"""
        _seed_knowledge(agent="vod")
        r = client.get(f"/api/knowledge?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        entry = r.json()["agents"]["vod"][0]
        assert entry["path"] == "issues/5/vod/knowledge.md"  # 剥掉 owner/repo 前缀
        # content 接口用该 path 可直接读到正文
        r2 = client.get(f"/api/knowledge/content?repo={_REPO}&file={entry['path']}", headers=_headers)
        assert r2.status_code == 200, r2.text
        assert "验收标准 = 接口可跑通" in r2.json()["content"]

    def test_list_repo_isolation(self):
        _seed_knowledge(agent="vod")
        r = client.get("/api/knowledge?repo=org/other", headers=_headers)
        assert r.status_code == 200, r.text
        assert r.json()["agents"] == {}

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


class TestKnowledgeUpload:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _upload(self, name="部署规范.md", scope="project", agent="", content="部署走灰度。"):
        files = {"file": (name, content.encode("utf-8"), "text/markdown")}
        data = {"repo": _REPO, "scope": scope}
        if agent:
            data["agent_type"] = agent
        return client.post("/api/knowledge", files=files, data=data, headers=_headers)

    def test_upload_markdown(self):
        r = self._upload()
        assert r.status_code == 200, r.text
        assert "上传成功" in r.json()["message"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT scope, created_by, file_path FROM agent_shared_knowledge WHERE scope='member' AND repo_key=? ORDER BY id DESC LIMIT 1",
                (_REPO_KEY,)).fetchone()
        assert row and row[0] == "member" and "project-info/member/" in row[2]

    def test_upload_agent_dimension(self):
        r = self._upload(scope="agent", agent="vod")
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT agent_type FROM agent_shared_knowledge WHERE scope='member' AND repo_key=? ORDER BY id DESC LIMIT 1",
                (_REPO_KEY,)).fetchone()
        assert row and row[0] == "vod"

    def test_upload_forbidden_ext(self):
        r = self._upload(name="x.py")
        assert r.status_code == 400

    def test_upload_too_large(self):
        r = self._upload(name="big.md", content="x" * (10 * 1024 * 1024 + 1))
        assert r.status_code == 400

    def test_upload_requires_auth(self):
        files = {"file": ("a.md", b"x", "text/markdown")}
        r = client.post("/api/knowledge", files=files, data={"repo": _REPO, "scope": "project"})
        assert r.status_code == 401

    def test_delete_member_doc(self):
        r = self._upload()
        path = r.json()["file_path"]
        r2 = client.delete(f"/api/knowledge?repo={_REPO}&file={path}", headers=_headers)
        assert r2.status_code == 200, r2.text
        from src.config import WORKSPACE_DIR
        assert not os.path.exists(os.path.join(WORKSPACE_DIR, "org", "test", path.replace("/", os.sep)))


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
        from src.config import WORKSPACE_DIR
        p = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "member", "kb_user", "部署规范.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("部署走灰度发布")
        r = client.get(f"/api/knowledge/context?repo={_REPO}", headers=_headers)
        assert r.status_code == 200, r.text
        assert "部署规范.md" in r.json()["context"]


class TestInjection:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_member_doc_injected_into_prompt(self):
        from src.config import WORKSPACE_DIR
        p = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "member", "kb_user", "部署规范.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("部署走灰度发布")
        from src.prompt_builder import build_conversation_prompt
        evt = {"seq": 1, "repo_key": _REPO_KEY, "issue_id": "#5", "agent_type": "vod",
               "event_type": "issue_opened", "title": "t", "status": "completed", "result": "r"}
        prompt = build_conversation_prompt(evt, "你好", [], "vod", 5)
        assert "部署规范.md" in prompt

    def test_project_info_files_include_member_docs(self):
        from src.config import WORKSPACE_DIR
        p = os.path.join(WORKSPACE_DIR, "org", "test", "project-info", "member", "kb_user", "环境说明.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"env": "prod"}')
        from src.runtime.service import enqueue_task
        tid = enqueue_task(agent_session_id="sess-kb", runtime_id=1, prompt="p",
                           repo_key=_REPO_KEY, agent_type="vod", issue_id="#5")
        from src.runtime import service
        task = service.get_task(tid)
        pif = json.loads(task["project_info_files"] or "{}")
        assert any("环境说明.json" in rel for rel in pif)
        kf = json.loads(task["knowledge_files"] or "{}")
        assert any("环境说明.json" in rel for rel in kf)
