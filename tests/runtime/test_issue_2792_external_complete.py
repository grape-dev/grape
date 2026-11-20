"""Issue #2792 切片 4：`POST /api/tasks/complete` 用户态外部执行结论上报（集成 seam）。

Spec（评论 id 201190348）实现路径 4 / 验收①④：外部执行者（用户态、无 task_id）
带 issue + 节点 + 结论上报时，结论写成**对应节点事件**（`source=external`、
`status=completed`），并按 `(issue, node, session)` 幂等；重复上报不产生重复事件、
不覆盖终态；既有会话 token（`tk_`）路径零回归。
"""
from __future__ import annotations

import json
import os
import uuid

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_2792_external.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.db_setup import init_database
from src.kernel.auth_sdk.jwt_utils import create_access_token
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db

init_database()

REPO_KEY = "gitcode:o/r"
REPO_FULL = "o/r"
ISSUE_NUM = 2792
SESSION_TASK = "t_2792_ext_regress"

_client = TestClient(app)


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM api_tokens WHERE user_id IN "
                  "(SELECT id FROM grape_accounts WHERE username LIKE 'i2792_%')")
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'i2792_%'")
        db.commit()


@pytest.fixture(autouse=True)
def _isolate():
    _clean()
    yield
    _clean()


def _user_with_repo(managed: bool = True) -> dict:
    name = f"i2792_{uuid.uuid4().hex[:6]}"
    u = grape_account_db.create_grape_account(name, email=None, password="pw")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES (?,?,?,?)", (REPO_KEY, "o", "r", REPO_FULL))
        if managed:
            c.execute("INSERT INTO user_repos (repo_key, user_id, is_join, is_admin) "
                      "VALUES (?,?,1,1)", (REPO_KEY, u["id"]))
        db.commit()
    return {"user": u, "jwt": create_access_token(u["id"], u["username"])}


def _post(token: str, payload: dict):
    return _client.post("/api/tasks/complete", json=payload,
                        headers={"Authorization": f"Bearer {token}"})


def _events(issue_num: int = ISSUE_NUM):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT seq, event_type, source, agent_type, status, issue_id, payload, result "
            "FROM events WHERE repo_key=? AND issue_id=? ORDER BY seq",
            (REPO_KEY, f"#{issue_num}")).fetchall()


def _external_body(**over) -> dict:
    body = {
        "repository": REPO_FULL,
        "issue_number": ISSUE_NUM,
        "node": "issue",
        "conclusion": "Spec 已确认，标签/里程碑/负责人已就绪",
        "artifacts": ["评论 #201190348"],
        "session_id": "sess-ext-1",
    }
    body.update(over)
    return body


def test_external_report_writes_node_event():
    """外部结论落为对应节点事件：agent_type=node、source=external、completed。"""
    ctx = _user_with_repo()
    r = _post(ctx["jwt"], _external_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["external"] is True
    assert data["seq"] > 0
    assert data["task_id"], "外部上报应解析/补建 Issue 任务"
    rows = _events()
    assert len(rows) == 1, rows
    seq, etype, source, agent_type, status, issue_id, payload, result = rows[0]
    assert etype == "a2a_event"
    assert source == "external"
    assert agent_type == "issue"
    assert status == "completed"
    assert issue_id == f"#{ISSUE_NUM}"
    meta = json.loads(payload or "{}")
    assert meta["external"] is True and meta["node"] == "issue"
    assert meta["session"] == "sess-ext-1"
    assert "Spec 已确认" in (result or "")


def test_repeat_report_is_idempotent_and_keeps_first_conclusion():
    """同 (issue,node,session) 重复上报：不产生重复事件、不覆盖原结论（验收④）。"""
    ctx = _user_with_repo()
    assert _post(ctx["jwt"], _external_body()).status_code == 200
    again = _post(ctx["jwt"], _external_body(conclusion="后到的另一份结论"))
    assert again.status_code == 200, again.text
    assert again.json()["already"] is True
    rows = _events()
    assert len(rows) == 1, rows
    assert "Spec 已确认" in (rows[0][7] or "")
    assert "后到的另一份结论" not in (rows[0][7] or "")


def test_different_session_writes_its_own_event():
    """换会话是另一份独立结论：按 (issue,node,session) 三元区分，不误判重复。"""
    ctx = _user_with_repo()
    assert _post(ctx["jwt"], _external_body()).status_code == 200
    other = _post(ctx["jwt"], _external_body(session_id="sess-ext-2", node="coding",
                                            conclusion="实现完成，PR #1"))
    assert other.status_code == 200, other.text
    assert other.json().get("already") is not True
    assert len(_events()) == 2


def test_user_without_repo_access_is_forbidden():
    """防 IDOR：非本人托管仓库不得写入节点事件。"""
    ctx = _user_with_repo(managed=False)
    r = _post(ctx["jwt"], _external_body())
    assert r.status_code == 403, r.text
    assert _events() == []


def test_missing_targets_still_rejected():
    """既没有 task_id 也没有 issue+node → 维持原 400（不含模糊写入）。"""
    ctx = _user_with_repo()
    r = _post(ctx["jwt"], {"result_text": "x"})
    assert r.status_code == 400, r.text
    assert _events() == []


def test_session_token_path_still_completes_task_without_external_event():
    """会话 token 既有路径零回归：仍按 session_key 收尾，不因带 issue/node 转外部上报。"""
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "prompt, status) VALUES (?, 'sess', 1, ?, 'issue', 'p', 'running')",
            (SESSION_TASK, REPO_KEY))
        db.commit()
    tok = auth_token.issue_session_token(
        session_key=SESSION_TASK, repo_key=REPO_KEY, agent_type="issue")
    r = _post(tok, {"result_text": "会话内完成", "issue_number": ISSUE_NUM, "node": "issue"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("external") is not True
    assert data.get("task_id") == SESSION_TASK, data
    assert data.get("self_reported") is True, data
    assert _events() == [], "会话 token 路径不得写外部结论事件"