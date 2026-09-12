"""Issues 对外接口测试（/api/issues）。

覆盖：仓库 Issue 列表（stage/status 聚合）、创建 Issue、详情（stage+events+pipeline）、
事件列表、pipeline 路线图；认证、聚合隔离、404 兜底。
"""
import os, json, io, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_headers = {}

OWNER = "test_issues"
REPO = "hello"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def setup_module():
    global _uid, _headers
    _uid = user_db.create_user("test_issues", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_issues")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 确保基础表已建（issue_state/tasks/events）
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass


def teardown_module():
    with db_lock:
        c = db.cursor()
        for t in ("events", "issue_state", "tasks", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _mk_issue_state(number, stage="qa", owner="qa", scene="default", assignee="grape_bot"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent, assignee, scene_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (REPO_KEY, f"#{number}", stage, owner, assignee, scene))
        db.commit()


def _mk_event(number, ev_type="issue_opened", agent="vod", status="completed"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (REPO_KEY, f"#{number}", ev_type, agent, f"Title #{number}", f"Summary #{number}", status))
        seq = c.lastrowid
        db.commit()
        return seq


def _mk_task(number, status="processing"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO tasks (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_uid["id"], "grape_bot", REPO_KEY, number, f"Task #{number}", status))
        db.commit()


# ===================== 列表 =====================

class TestListIssues:
    def test_empty_repo(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["issues"] == [] and data["total"] == 0

    def test_aggregates_stage_and_status(self):
        _mk_issue_state(5, stage="qa", owner="qa", scene="default")
        _mk_event(5, "issue_opened", "vod")
        _mk_event(5, "pull_request_updated", "qa")
        _mk_task(5, status="processing")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        assert resp.status_code == 200
        issues = resp.json()["issues"]
        assert len(issues) >= 1
        it = next(i for i in issues if i["number"] == 5)
        assert it["stage"] == "qa"
        assert it["task_status"] == "processing"
        assert it["owner_agent"] == "qa"
        assert it["scene_id"] == "default"
        assert it["events_count"] == 2
        assert it["latest_event"]["event_type"] == "pull_request_updated"

    def test_events_only_scoped_by_issue(self):
        _mk_issue_state(6, stage="sre")
        _mk_event(6, "issue_opened", "vod")
        _mk_event(6, "pull_request_merged", "sre")
        _mk_issue_state(7, stage="vod")
        _mk_event(7, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        it6 = next(i for i in resp.json()["issues"] if i["number"] == 6)
        it7 = next(i for i in resp.json()["issues"] if i["number"] == 7)
        assert it6["events_count"] == 2
        assert it7["events_count"] == 1

    def test_requires_auth(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}")
        assert resp.status_code == 401


# ===================== 详情 =====================

class TestIssueDetail:
    def test_detail_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99999", headers=_headers)
        assert resp.status_code == 404

    def test_detail_aggregate(self):
        _mk_issue_state(8, stage="coding", owner="coding", scene="iac")
        _mk_event(8, "issue_opened", "vod")
        _mk_event(8, "pull_request_opened", "coding")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/8", headers=_headers)
        assert resp.status_code == 200
        d = resp.json()
        assert d["stage"] == "coding" and d["owner_agent"] == "coding"
        assert d["scene_id"] == "iac"
        assert len(d["events"]) == 2
        assert "pipeline" in d

    def test_detail_no_issue_state_fallback(self):
        _mk_event(9, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/9", headers=_headers)
        assert resp.status_code == 200
        d = resp.json()
        assert d["stage"] == "vod"  # 无 issue_state → 兜底 vod
        assert len(d["events"]) == 1


# ===================== 事件列表 =====================

class TestIssueEvents:
    def test_events_scope(self):
        _mk_issue_state(10, stage="qa")
        _mk_event(10, "issue_opened", "vod")
        _mk_event(10, "pull_request_updated", "qa")
        _mk_issue_state(11, stage="vod")
        _mk_event(11, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/10/events", headers=_headers)
        assert resp.status_code == 200
        evs = resp.json()["events"]
        assert len(evs) == 2
        assert all(e["event_type"] in ("issue_opened", "pull_request_updated") for e in evs)

    def test_events_empty(self):
        _mk_issue_state(12, stage="done")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/12/events", headers=_headers)
        assert resp.status_code == 200
        assert resp.json() == {"events": [], "total": 0}

    def test_events_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99998/events", headers=_headers)
        assert resp.status_code == 404


# ===================== Pipeline =====================

class TestIssuePipeline:
    def test_pipeline_without_archive(self):
        _mk_issue_state(13, stage="sre")
        _mk_event(13, "issue_opened", "vod")
        _mk_event(13, "pull_request_merged", "sre")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/13/pipeline", headers=_headers)
        assert resp.status_code == 200
        p = resp.json()
        assert "segments" in p
        assert len(p["segments"]) >= 1
        assert p["segments"][0]["seq"] >= 1

    def test_pipeline_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99997/pipeline", headers=_headers)
        assert resp.status_code == 404


# ===================== 创建 =====================

class TestCreateIssue:
    def test_create_missing_fields(self):
        resp = client.post(f"/api/issues/{OWNER}/{REPO}", json={"title": ""}, headers=_headers)
        assert resp.status_code == 400

    def test_create_ok(self):
        from unittest.mock import patch
        _mk_issue_state(20, stage="vod")  # 预置避免走 GitCode API
        # 用 mock 覆盖 GitCode create + token，防止真实 API 调用
        with patch("src.api.routes.tasks._find_open_issue_by_title", return_value=None), \
             patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"), \
             patch("src.api.routes.tasks._gitcode_api_with_refresh") as _mock_api, \
             patch("src.api.routes.tasks.ensure_issue_task") as _mock_ensure:
            _mock_api.return_value = {"number": 20}
            _mock_ensure.return_value = 20
            resp = client.post(
                f"/api/issues/{OWNER}/{REPO}",
                json={"title": "新需求", "description": "描述", "repo_key": REPO_KEY},
                headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("issue_number") == 20
        assert data.get("created") is True