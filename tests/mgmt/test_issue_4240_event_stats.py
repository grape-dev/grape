"""Issue #4240: Agent 事件统计卡与总量口径对齐。

覆盖：
1. `metrics.event_stats` 固定下发五状态键（缺省 0，含 cancelled）；
2. `total_events == sum(event_stats)`，状态卡之和等于总量；
3. `total_events_raw` 为该 repo 集合 + agent_type 的全量事件数，不受 keyword 影响；
4. 未知状态不进状态卡字典（只计入原始总数），不再出现加不平组合；
5. `events_total` / `events_page` / `events_page_size` 语义保持不变。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user, get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 42401
USERNAME = "agent4240"
RK = "gitcode:org/gg4240"


@pytest.fixture(autouse=True)
def _client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "api_key"}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "api_key"}
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:org/gg4240%'")
        cur.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:org/gg4240%'")
        db.commit()


def _seed_session(agent_type="issue"):
    sid = "agsess-%s" % uuid.uuid4().hex[:8]
    with db_lock:
        cur = db.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO devops_agent_session "
            "(id, repo_key, agent_type, runtime_id, skill, status, runtime_session_id, "
            "repo_gitcode_token, enable, subscribe_events) VALUES (?,?,?,?,?,?,?,?,1,'[]')",
            (sid, RK, agent_type, None, "askill", "idle", "sess_x", "tok"))
        db.commit()


def _seed_events(statuses, agent_type="issue"):
    with db_lock:
        cur = db.cursor()
        for st in statuses:
            cur.execute(
                "INSERT INTO events (repo_key, event_type, agent_type, title, payload, status) "
                "VALUES (?,?,?,?,?,?)",
                (RK, "issue_opened", agent_type, "t", "{}", st))
        db.commit()


def _detail(client, query=""):
    return client.get(f"/api/agents/issue/org/gg4240{query}")


def test_event_stats_has_five_keys_and_total_is_status_sum(_client):
    _seed_session()
    _seed_events(["completed", "failed", "cancelled", "running"])
    r = _detail(_client)
    assert r.status_code == 200
    metrics = r.json()["metrics"]
    assert set(metrics["event_stats"].keys()) == {
        "pending", "running", "completed", "failed", "cancelled"}
    assert metrics["event_stats"] == {
        "pending": 0, "running": 1, "completed": 1, "failed": 1, "cancelled": 1}
    assert metrics["total_events"] == sum(metrics["event_stats"].values()) == 4
    assert metrics["total_events_raw"] == 4


def test_only_failed_and_cancelled_adds_up(_client):
    _seed_session()
    _seed_events(["failed", "cancelled"])
    r = _detail(_client)
    assert r.status_code == 200
    metrics = r.json()["metrics"]
    assert metrics["event_stats"]["failed"] == 1
    assert metrics["event_stats"]["cancelled"] == 1
    assert metrics["event_stats"]["completed"] == 0
    assert metrics["event_stats"]["running"] == 0
    assert metrics["total_events"] == 2
    assert metrics["total_events"] == sum(metrics["event_stats"].values())
    assert metrics["total_events_raw"] == 2


def test_unknown_status_not_in_status_cards_but_in_raw_total(_client):
    _seed_session()
    _seed_events(["weird_status"])
    r = _detail(_client)
    assert r.status_code == 200
    metrics = r.json()["metrics"]
    assert set(metrics["event_stats"].keys()) == {
        "pending", "running", "completed", "failed", "cancelled"}
    assert metrics["total_events"] == 0
    assert metrics["total_events_raw"] == 1


def test_total_events_raw_ignores_keyword_and_paging_semantics_unchanged(_client):
    _seed_session()
    _seed_events(["completed", "completed", "completed"])
    r = _detail(_client, "?keyword=no-match&page=2&page_size=50")
    assert r.status_code == 200
    data = r.json()
    metrics = data["metrics"]
    assert metrics["total_events"] == 3
    assert metrics["total_events_raw"] == 3
    assert data["events_total"] == 0
    assert data["events_page"] == 2
    assert data["events_page_size"] == 50
    assert metrics["total_events"] == sum(metrics["event_stats"].values())
