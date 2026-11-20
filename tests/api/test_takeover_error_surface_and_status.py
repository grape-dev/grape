"""接管错误面（P0-2）与状态归一（P1-8）回归。

覆盖两批加固：
1. `start_takeover` 只抛 TakeoverError：锁不可用降级、未预期异常兜成
   `500 internal_error` + log.exception、release_lock 失败不回滚成功；
   失败体统一带 request_id（插件/前端可直接回执定位日志）。
2. `agent_statuses` 把 events.status='takeover' 归一到 'running'，与流水线卡片同口径。
"""

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.domains.runtime.engine import takeover as svc
from src.domains.runtime.issues_center import _batch_fetch_agent_statuses
from src.kernel.ports import PortNotBound

REPO = "gitcode:probe/errsurf"


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    """种子与读取绑定同一个连接（全树收集时其它模块会换库）。"""
    monkeypatch.setattr(svc, "db", svc.db, raising=False)


def _cleanup():
    db, lock = svc.db, svc.db_lock
    with lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM takeover_sessions WHERE repo_key=?", (REPO,)),
            ("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,)),
            ("DELETE FROM events WHERE repo_key=?", (REPO,)),
            ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
            ("DELETE FROM session_api_tokens WHERE session_key LIKE 'takeover:%'", ()),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _seed(issue_number=101, task_id="t_err_101", seq=9101, event_status="running"):
    db, lock = svc.db, svc.db_lock
    with lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage,"
            " owner_agent, scene_id, assignee) VALUES (?,?,?,?,?,?,?,?,?)",
            (1, REPO, issue_number, "错误面测试", "processing", "coding", "coding", "default", ""),
        )
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status,"
            " source, title, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, REPO, "a2a_event", f"#{issue_number}", "coding", event_status, "a2a", "t", "{}"),
        )
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, status, agent_type, skill, prompt,"
            " env_extra, cwd, runtime_id, event_seq, repo_key, issue_id, user_id, account, skill_files)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, f"ses_{issue_number}", "running", "coding", "implementation", "跑起来", "{}",
             f"/tmp/takeover-errsurf/{issue_number}/coding", 0, seq, REPO, f"#{issue_number}", 1, "", "{}"),
        )
        db.commit()


@pytest.fixture()
def client():
    _cleanup()
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "tester", "auth_type": "api_key", "token": "x", "token_type": "api_key",
    }
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def _start(client):
    return client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": "101", "platform": "dsh", "host": "test-host",
    })


def _get(sql, args=()):
    db, lock = svc.db, svc.db_lock
    with lock:
        c = db.cursor()
        c.execute(sql, args)
        return c.fetchone()


def test_lock_unavailable_degrades_instead_of_500(client, monkeypatch):
    """锁抛 PortNotBound（端口未绑定）时必须降级成功，不再冒泡成裸 500。"""
    _seed()

    def _boom(*a, **k):
        raise PortNotBound("pipeline")

    monkeypatch.setattr(svc, "acquire_lock", _boom)
    r = _start(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert _get("SELECT status FROM takeover_sessions WHERE repo_key=?", (REPO,))[0] == "active"
    assert _get("SELECT status FROM agent_tasks WHERE id='t_err_101'")[0] == "cancelled"


def test_lock_busy_still_409_without_side_effects(client, monkeypatch):
    """锁被别人持有（返回 False）仍是 409，且不得写会话、不得取消任务。"""
    _seed()
    monkeypatch.setattr(svc, "acquire_lock", lambda *a, **k: False)
    r = _start(client)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "taken_by_other"
    assert r.json()["request_id"]
    assert _get("SELECT COUNT(*) FROM takeover_sessions WHERE repo_key=?", (REPO,))[0] == 0
    assert _get("SELECT status FROM agent_tasks WHERE id='t_err_101'")[0] == "running"


def test_unexpected_exception_becomes_structured_500_with_traceback(client, monkeypatch):
    """未预期异常 → 500 internal_error，响应可诊断，服务端留 log.exception。"""
    _seed()
    logged = []

    def _boom(**kwargs):
        raise RuntimeError("bundle exploded")

    monkeypatch.setattr(svc, "acquire_lock", lambda *a, **k: True)
    monkeypatch.setattr(svc, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(svc, "build_bundle", _boom)
    monkeypatch.setattr(svc.log, "exception", lambda msg, *a, **k: logged.append(msg))

    r = _start(client)
    assert r.status_code == 500, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "internal_error"
    assert body["message"]
    assert body["request_id"]
    assert logged and "bundle exploded" in logged[0]
    assert "probe/errsurf" in logged[0] and "#101" in logged[0]


def test_release_lock_failure_does_not_turn_success_into_500(client, monkeypatch):
    """事务已提交后 release_lock 抛错只记 warning，成功响应不得变 500。"""
    _seed()
    monkeypatch.setattr(svc, "acquire_lock", lambda *a, **k: True)

    def _boom(*a, **k):
        raise RuntimeError("release failed")

    monkeypatch.setattr(svc, "release_lock", _boom)
    r = _start(client)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_agent_statuses_normalizes_takeover_to_running(client):
    """events.status='takeover' → agent_statuses 返回 running/status_num=1，原始值留 raw_status。"""
    _cleanup()
    _seed(event_status="takeover")
    out = _batch_fetch_agent_statuses([(REPO, "#101")])
    rows = out[(REPO, "#101")]
    coding = next(r for r in rows if r["agent"] == "coding")
    assert coding["status"] == "running"
    assert coding["status_num"] == 1
    assert coding.get("takeover") is True
    assert coding.get("raw_status") == "takeover"
    # 其它节点不受影响
    vod = next(r for r in rows if r["agent"] == "vod")
    assert vod["status"] == "pending" and vod["status_num"] == 0

def test_public_facade_forwards_every_member_the_route_calls():
    """mgmt 接管路由用到的 svc.* 成员必须在 runtime.public 门面上存在。

    issue-3201 把路由改成 `from src.domains.runtime import public as svc`，但漏转发
    skipped_dispatch_count / skipped_dispatch_message → GET /api/takeover/sessions
    与 GET /api/takeover/sessions/{id} 恒 500（AttributeError，日志无堆栈）。此守卫防复发。
    """
    import re
    from pathlib import Path

    from src.domains.runtime import public as pub

    src = Path(__file__).resolve().parents[2] / "src/domains/mgmt/routes/takeover.py"
    used = sorted(set(re.findall(r"\bsvc\.([A-Za-z_]\w*)", src.read_text(encoding="utf-8"))))
    missing = [n for n in used if not hasattr(pub, n)]
    assert used, "路由文件未被解析到 svc.* 调用"
    assert missing == [], f"runtime.public 门面漏转发：{missing}"


def test_list_and_get_session_endpoints_are_200(client):
    """列表/详情两个端点在存在 active 会话时必须 200（此前因门面漏转发恒 500）。"""
    _seed()
    sid = _start(client).json()["takeover_id"]
    lst = client.get("/api/takeover/sessions", params={"repo_key": REPO, "status": "active"})
    assert lst.status_code == 200, lst.text
    sessions = lst.json()["sessions"]
    assert sessions and sessions[0]["id"] == sid
    assert "skipped_dispatches" in sessions[0]
    got = client.get(f"/api/takeover/sessions/{sid}")
    assert got.status_code == 200, got.text
    assert got.json()["session"]["id"] == sid


UUID_USER = "aebb29af-64b1-40ce-84fc-02ba3e335066"


def _as(client, uid, name="uuid-user"):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": uid, "username": name, "auth_type": "api_key", "token": "x", "token_type": "api_key",
    }


def test_uuid_account_can_start_and_read_takeover(client):
    """账号 id 是 uuid（D105）时接管全链路可用：原来 int(user["id"]) 直接 ValueError → 500。"""
    _seed()
    _as(client, UUID_USER)
    r = _start(client)
    assert r.status_code == 200, r.text
    sid = r.json()["takeover_id"]
    assert _get("SELECT user_id FROM takeover_sessions WHERE id=?", (sid,))[0] == UUID_USER
    # 幂等重连（_rebuild_for_session 同样走归属校验 + 重签 token）
    assert _start(client).json()["reused"] is True
    # 列表 / 详情 / 进展 三条读路径在 uuid 下都必须通
    assert client.get("/api/takeover/sessions", params={"repo_key": REPO, "status": "active"}).status_code == 200
    assert client.get(f"/api/takeover/sessions/{sid}").status_code == 200
    assert client.post(f"/api/takeover/sessions/{sid}/progress",
                       json={"messages": [{"type": "text", "content": "hi"}]}).status_code == 200
    # 归属校验对别的 uuid 用户仍然拒绝
    _as(client, "00000000-0000-4000-8000-000000000000", "other")
    other = client.get(f"/api/takeover/sessions/{sid}")
    assert other.status_code == 403, other.text
    assert other.json()["code"] == "not_owner"


def test_same_user_helper_handles_uuid_and_legacy_int():
    """归属比较：uuid 原样比、空值哨兵一致、历史整数 id 与字符串形态等价。"""
    assert svc._same_user(UUID_USER, {"id": UUID_USER}) is True
    assert svc._same_user(UUID_USER, {"id": "someone-else"}) is False
    assert svc._same_user(3, {"id": 3}) is True
    assert svc._same_user("3", {"id": 3}) is True
    assert svc._same_user(0, {"id": ""}) is True
    assert svc._same_user(None, {"id": 0}) is True
    assert svc._same_user(0, {"id": 2}) is False
