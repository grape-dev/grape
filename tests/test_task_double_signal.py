"""双重标记完成状态机测试（issue-219-task-complete-double-signal-design.md）。

覆盖：双标记未齐不终态、两种并发时序（流先/自报先）、重复标记幂等、
agent 显式路径（skill_self_reports=True）、result_text 不丢、非自报技能走旧路径。
"""
import os
import sys
import json

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_task_double_signal.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.session_manager import db, db_lock
from src.api.db_setup import init_database
from src.runtime import service

init_database()

REPO = "gitcode:o/r"
TASK = "t_double"

_client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()


def _seed_task(skill="vod-cli", status="running"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, skill, prompt, status) "
            "VALUES (?, 'sess', 1, ?, 'vod', ?, 'p', ?)",
            (TASK, REPO, skill, status),
        )
        db.commit()


def _task_row() -> dict:
    t = service.get_task(TASK)
    return t or {}


def test_two_signals_required_before_finalize():
    _seed_task()
    assert service.set_task_signal(TASK, "stream_ended") is True
    assert service.task_double_signal_ready(TASK) is False
    assert service.finalize_task(TASK) is False
    assert service.get_task(TASK)["status"] == "running"


def test_worker_stream_first_then_agent_self_report():
    _seed_task()
    service.set_task_signal(TASK, "stream_ended")
    service.set_task_signal(TASK, "self_reported", json.dumps({"actions": [], "conclusion": "流先自报后"}, ensure_ascii=False))
    assert service.task_double_signal_ready(TASK) is True
    assert service.finalize_task(TASK) is True
    t = service.get_task(TASK)
    assert t["status"] == "completed"
    # result_text 不丢：完整结构化全文
    d = json.loads(t["result_text"])
    assert d["conclusion"] == "流先自报后"


def test_agent_self_report_first_then_worker_stream():
    _seed_task()
    service.set_task_signal(TASK, "self_reported", json.dumps({"actions": [], "conclusion": "自报先于流"}, ensure_ascii=False))
    assert service.finalize_task(TASK) is False  # stream 未齐不终态
    service.set_task_signal(TASK, "stream_ended")
    assert service.finalize_task(TASK) is True
    t = service.get_task(TASK)
    assert t["status"] == "completed"
    assert json.loads(t["result_text"])["conclusion"] == "自报先于流"


def test_signal_idempotent_second_set_returns_false():
    _seed_task()
    assert service.set_task_signal(TASK, "self_reported", "x") is True
    assert service.set_task_signal(TASK, "self_reported", "y") is False
    assert service.get_task(TASK)["result_text"] == "x"  # 不覆盖首次提交


def test_result_text_not_truncated_persisted_full():
    _seed_task()
    long_text = "已回复 Issue #3 并归档为终态。" + "长。" * 300  # >200 字符
    service.set_task_signal(TASK, "stream_ended")
    service.set_task_signal(TASK, "self_reported", json.dumps({"actions": [], "conclusion": long_text}, ensure_ascii=False))
    service.finalize_task(TASK)
    t = service.get_task(TASK)
    assert len(t["result_text"]) > 200
    assert json.loads(t["result_text"])["conclusion"] == long_text


def test_endpoint_agent_explicit_waits_for_stream_end(monkeypatch):
    monkeypatch.setattr(service, "skill_self_reports", lambda s: True)
    _seed_task()
    token = auth_token.issue_session_token(session_key=TASK, repo_key=REPO, agent_type="vod")
    r = _client.post("/api/tasks/complete", json={"result_text": "叙述先行"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    t = service.get_task(TASK)
    # stream 未结束 → 不终态，但 self_reported 已落、result_text 已存（不丢）
    assert t["status"] == "running"
    assert t["self_reported"] == 1
    assert json.loads(t["result_text"])["conclusion"] == "叙述先行"
    # worker 隐式后到 → 双满 → 收敛终态
    service.set_task_signal(TASK, "stream_ended")
    assert service.finalize_task(TASK) is True
    assert service.get_task(TASK)["status"] == "completed"
    # 重复显式 → already
    r2 = _client.post("/api/tasks/complete", json={"result_text": "again"}, headers={"Authorization": f"Bearer {token}"})
    assert r2.json()["already"] is True


def test_endpoint_non_self_report_skill_legacy_complete(monkeypatch):
    monkeypatch.setattr(service, "skill_self_reports", lambda s: False)
    _seed_task(skill="issue")
    token = auth_token.issue_session_token(session_key=TASK, repo_key=REPO, agent_type="vod")
    r = _client.post("/api/tasks/complete", json={"conclusion": "非自报技能直接完成"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert service.get_task(TASK)["status"] == "completed"