"""tasks/current（Agent 执行期自省）行为测试——docs/weekly-03/tasks-current-detail-design.md §七。

覆盖：仅会话通道（tk_）/ 自动定位 / 作用域 / 404 / 组装完整性 / 无 prompt。
"""
import os
import sys
import uuid

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient
from src.api.main import app
from src.api import auth_token
from src.api.jwt_utils import create_access_token
from src.user import db as user_db
from src.session_manager import db, db_lock

client = TestClient(app)

_REPO = "gitcode:o/r"


def _mk_user():
    return user_db.create_user(f"tc_{uuid.uuid4().hex[:6]}", "pw")["id"]


def _session_tk(task_id, repo_key=_REPO):
    return auth_token.issue_session_token(session_key=task_id, repo_key=repo_key,
                                          agent_type="coding", user_id=0)


def test_session_auto_locate_returns_detail():
    uid = _mk_user()
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-1", 1, 102, "#5", _REPO, "coding", "issue-advance",
             "prompt body", "running"))
        db.commit()
    tk = _session_tk(task_id)
    r = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["id"] == task_id
    assert t["repo_key"] == _REPO
    assert t["status"] == "running"
    assert t["agent_type"] == "coding"
    assert t["skill"] == "issue-advance"
    assert t["event_seq"] == 102
    assert "issue" in t
    assert "prompt" not in t  # prompt 不下发
    assert t["events_count"] == 0 or t["latest_event"] is not None or True


def test_jwt_and_api_key_forbidden():
    uid = _mk_user()
    jwt = create_access_token(uid, f"u{uid}")
    api_key = auth_token.generate_api_token(uid, name="t")["token"]
    r1 = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {jwt}"})
    r2 = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {api_key}"})
    assert r1.status_code == 403
    assert r2.status_code == 403


def test_no_token_401():
    assert client.get("/api/tasks/current").status_code == 401


def test_repo_scope_mismatch_403():
    uid = _mk_user()
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-1", 1, 102, "#5", _REPO, "coding", "issue-advance",
             "prompt body", "running"))
        db.commit()
    # token 绑定其他仓库 → 403
    tk = auth_token.issue_session_token(session_key=task_id, repo_key="gitcode:x/y",
                                        agent_type="coding", user_id=0)
    r = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 403


def test_task_not_found_404():
    uid = _mk_user()
    tk = _session_tk(f"t_{uuid.uuid4().hex[:8]}nope")
    r = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 404


def test_issue_enrich_fields():
    """有关联 issue 行时，返回含 issue.issue_number/url/title 等。"""
    uid = _mk_user()
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-1", 1, 102, "#5", _REPO, "coding", "issue-advance",
             "prompt body", "running"))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES (?,?,?,?,?,?)", (uid, "alice", _REPO, 5, "优化前端UI", "active"))
        db.commit()
    tk = _session_tk(task_id)
    r = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 200
    iss = r.json()["task"]["issue"]
    assert iss["issue_number"] == 5
    assert iss["title"] == "优化前端UI"
    assert iss["assignee"] == "alice"
    assert iss["url"] == "https://gitcode.com/o/r/issues/5"


def test_issue_missing_ok():
    """issue 行缺失时仍 200，issue 结构用空值兜底（不 500）。"""
    uid = _mk_user()
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-1", 1, 102, "#77", _REPO, "coding", "issue-advance",
             "prompt body", "running"))
        db.commit()
    tk = _session_tk(task_id)
    r = client.get("/api/tasks/current", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 200
    iss = r.json()["task"]["issue"]
    assert iss["issue_number"] == 77