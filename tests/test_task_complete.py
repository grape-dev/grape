"""任务自报完成端点测试（issue-45-task-complete-endpoint-design.md）。

覆盖：会话 token 自动定位（T1）、作用域不匹配（T2/T3）、JWT 用户维度（T4 正/T5 反）、
终态幂等（T6）、未认证（T7）。
"""
import os
import sys
import uuid

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_task_complete.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.jwt_utils import create_access_token
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api.db_setup import init_database

init_database()

REPO = "gitcode:o/r"
TASK = "t_complete_test"

_client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM api_tokens")
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM api_tokens")
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO,))
        db.commit()


def _seed_task(status="running"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, prompt, status) "
            "VALUES (?, 'sess', 1, ?, 'vod', 'p', ?)",
            (TASK, REPO, status),
        )
        db.commit()


def _session_token(repo=REPO, session_key=TASK) -> str:
    return auth_token.issue_session_token(session_key=session_key, repo_key=repo, agent_type="vod")


def _task_status() -> str:
    with db_lock:
        r = db.cursor().execute("SELECT status FROM agent_tasks WHERE id=?", (TASK,)).fetchone()
        return r[0] if r else None


def _post_complete(token, payload, headers=None):
    h = {"Authorization": f"Bearer {token}"}
    if headers:
        h.update(headers)
    return _client.post("/api/tasks/complete", json=payload, headers=h)


def test_t1_session_token_auto_locates_and_completes():
    _seed_task("running")
    tok = _session_token()
    r = _post_complete(tok, {"result_text": "已完成审视。"})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert _task_status() == "completed"


def test_t2_foreign_session_cannot_target_other_task():
    _seed_task("running")
    tok = _session_token(session_key="t_another")  # 该 token 只能定位它自己的 session
    # 会话 token 派生的 task_id=自己的 session_key（t_another），查无此任务 → 404（天然防他写）
    assert _post_complete(tok, {"result_text": "x"}).status_code == 404
    assert _task_status() == "running"


def test_t3_repo_mismatch_forbidden():
    _seed_task("running")
    tok = _session_token(repo="gitcode:other/r")
    assert _post_complete(tok, {"result_text": "x"}).status_code == 403


def test_t4_jwt_user_with_managed_repo_allowed():
    _seed_task("running")
    u = user_db.create_user(f"tc_{uuid.uuid4().hex[:6]}", "pw")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                  (REPO, "o", "r", "o/r"))
        c.execute("INSERT INTO user_repos (repo_key, user_id, is_join, is_admin) VALUES (?,?,1,1)",
                  (REPO, u["id"]))
        db.commit()
    jwt = create_access_token(u["id"], u["username"])
    r = _post_complete(jwt, {"task_id": TASK, "result_text": "用户维度完成"})
    assert r.status_code == 200
    assert _task_status() == "completed"


def test_t5_jwt_user_without_repo_forbidden():
    _seed_task("running")
    u = user_db.create_user(f"tc2_{uuid.uuid4().hex[:6]}", "pw")
    jwt = create_access_token(u["id"], u["username"])
    assert _post_complete(jwt, {"task_id": TASK, "result_text": "x"}).status_code == 403
    assert _task_status() == "running"


def test_t6_terminal_idempotent():
    _seed_task("completed")
    tok = _session_token()
    r = _post_complete(tok, {"result_text": "x"})
    assert r.status_code == 200
    assert r.json()["already"] is True
    assert r.json()["status"] == "completed"


def test_t7_no_token_401():
    assert _client.post("/api/tasks/complete", json={"result_text": "x"}).status_code == 401