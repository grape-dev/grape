"""单信号收尾状态机测试（weekly-05 stream-end-finalize-single-signal-design.md）。

覆盖：流结束即终态（不再等双信号/不再区分自报技能）、未自报回落流文本、非 0 exit_code 判失败、
set_task_signal 幂等、tasks complete 仅记录 self_report（pending_finalize）、流结束收敛、宽限窗补挂/提升。
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
        c.execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
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


def test_stream_end_finalizes_with_self_report():
    _seed_task()
    service.set_task_signal(TASK, "self_reported", json.dumps({"actions": [], "conclusion": "结构化结论"}, ensure_ascii=False))
    assert service.finalize_by_stream_end(TASK, "stream text", 0) is True
    t = service.get_task(TASK)
    assert t["status"] == "completed"
    assert json.loads(t["result_text"])["conclusion"] == "结构化结论"


def test_stream_end_fallback_stream_text():
    _seed_task()
    assert service.finalize_by_stream_end(TASK, "plain stream conclusion", 0) is True
    assert service.get_task(TASK)["status"] == "completed"


def test_stream_end_nonzero_exit_failed():
    _seed_task()
    assert service.finalize_by_stream_end(TASK, "whatever", 1) is True
    assert service.get_task(TASK)["status"] == "failed"


def test_signal_idempotent_second_set_returns_false():
    _seed_task()
    assert service.set_task_signal(TASK, "self_reported", "x") is True
    assert service.set_task_signal(TASK, "self_reported", "y") is False
    assert service.get_task(TASK)["result_text"] == "x"  # 不覆盖首次提交


def test_result_text_not_truncated_persisted_full():
    _seed_task()
    long_text = "已回复 Issue #3 并归档为终态。" + "长。" * 300  # >200 字符
    service.set_task_signal(TASK, "self_reported", json.dumps({"actions": [], "conclusion": long_text}, ensure_ascii=False))
    service.finalize_by_stream_end(TASK, "", 0)
    t = service.get_task(TASK)
    assert len(t["result_text"]) > 200
    assert json.loads(t["result_text"])["conclusion"] == long_text


def test_endpoint_records_self_report_pending_finalize():
    _seed_task()
    token = auth_token.issue_session_token(session_key=TASK, repo_key=REPO, agent_type="vod")
    r = _client.post("/api/tasks/complete", json={"result_text": "叙述先行"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    t = service.get_task(TASK)
    # 流未结束 → 不终态，但 self_reported 已记录、result_text 未丢
    assert t["status"] == "running"
    assert t["self_reported"] == 1
    assert json.loads(t["result_text"])["conclusion"] == "叙述先行"
    # 随后流结束 → 单信号收敛，采用自报结构化结论
    assert service.finalize_by_stream_end(TASK, "", 0) is True
    assert service.get_task(TASK)["status"] == "completed"
    # 重复显式 → already（幂等）
    r2 = _client.post("/api/tasks/complete", json={"result_text": "again"}, headers={"Authorization": f"Bearer {token}"})
    assert r2.json()["already"] is True


def test_endpoint_regular_skill_same_semantics():
    # 不再区分自报/普通：任何 skill 都走单信号
    _seed_task(skill="issue")
    token = auth_token.issue_session_token(session_key=TASK, repo_key=REPO, agent_type="vod")
    r = _client.post("/api/tasks/complete", json={"conclusion": "普通技能同样记录自报"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert service.get_task(TASK)["self_reported"] == 1