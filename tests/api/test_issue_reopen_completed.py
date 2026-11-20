"""已完成需求「回到处理中」回退口（2026-11-20 主人需求）。

背景：`_TASK_STATUS_FLOWS['completed']` 只放行 → pending_review，而前端对 completed 任务把
确认完成/取消整排按钮隐藏，于是误触「确认完成」后页面上没有任何入口能继续该需求。

口径 B（主人选定）：`POST /api/issues/{id}/reopen` 只做三件事——status→processing、
清 completed_at/completed_reason、**清空 stage**；不自动 A2A 重派，续跑由用户在事件页手动重派。
为什么不复用 PUT /{id}/status：流转表不允许 completed→processing，且那条通用写路径不会清 stage
（stage='done' 会让前端 mapStatusL 继续判成「已完成」，按钮点了也像没生效）。
"""
import os
import sys
import uuid

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.jwt_utils import create_access_token
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

REPO_KEY = "gitcode:oreopen/rreopen"


def _db():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


def _user() -> tuple:
    u = grape_account_db.create_grape_account(f"oreopen_{uuid.uuid4().hex[:6]}", email=None, password="pw")
    return u["id"], u["username"]


def _auth(uid: int, uname: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid, uname)}"}


def _clear(repo_key: str = REPO_KEY) -> None:
    db, db_lock = _db()
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        db.commit()


def _seed(uid: int, status: str = "completed", issue_number: int = 90201,
          stage: str = "done", rows: int = 1) -> list:
    """插入 1..n 行同 repo_key+issue_number 的需求卡，返回 id 列表。"""
    db, db_lock = _db()
    ids = []
    with db_lock:
        c = db.cursor()
        for _ in range(rows):
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, "
                "stage, completed_at, completed_reason, org_id) "
                "VALUES (?, ?, ?, ?, 'reopen case', ?, ?, "
                "CASE WHEN ?='completed' THEN '2026-11-20 09:00:00' ELSE NULL END, "
                "CASE WHEN ?='completed' THEN 'manual' ELSE NULL END, 0)",
                (uid, "oreopen_user", REPO_KEY, issue_number, status, stage, status, status))
            ids.append(c.lastrowid)
        db.commit()
    return ids


def _row(issue_id: int) -> tuple:
    db, _ = _db()
    return db.cursor().execute(
        "SELECT status, stage, completed_at, completed_reason FROM issue WHERE id=?",
        (issue_id,)).fetchone()


@pytest.fixture()
def seeded():
    uid, uname = _user()
    ids = _seed(uid)
    yield uid, uname, ids
    _clear()


def test_reopen_completed_back_to_processing(seeded):
    """主路径：completed → processing，stage/done 与完成信息全部清掉。"""
    uid, uname, ids = seeded
    r = client.post(f"/api/issues/{ids[0]}/reopen", headers=_auth(uid, uname))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "processing" and body["stage"] == ""
    status, stage, completed_at, completed_reason = _row(ids[0])
    assert status == "processing", "状态必须回到处理中"
    assert (stage or "") == "", "stage 必须清空：留着 done 前端仍显示「已完成」"
    assert completed_at is None and completed_reason is None, "完成时间/原因必须清掉"


def test_reopen_rejects_non_completed(seeded):
    """只有 completed 可回退；processing 再点一次必须 400（防重复点）。"""
    uid, uname, ids = seeded
    assert client.post(f"/api/issues/{ids[0]}/reopen", headers=_auth(uid, uname)).status_code == 200
    r = client.post(f"/api/issues/{ids[0]}/reopen", headers=_auth(uid, uname))
    assert r.status_code == 400, r.text
    assert "仅已完成需求可回到处理中" in r.text


def test_reopen_other_users_issue_is_404():
    """他人需求按 404 处理（与 resume 同口径：查询就带 user_id 过滤，不泄漏存在性）。"""
    owner_id, _ = _user()
    stranger_id, stranger_name = _user()
    ids = _seed(owner_id)
    try:
        r = client.post(f"/api/issues/{ids[0]}/reopen", headers=_auth(stranger_id, stranger_name))
        assert r.status_code == 404, r.text
        assert _row(ids[0])[0] == "completed", "别人点不动"
    finally:
        _clear()


def test_reopen_scoped_to_target_issue_only():
    """回退按 repo_key+issue_number 落库（#1741 口径），必须只命中目标需求。

    同仓另一条需求若被顺手改掉，等于「点一个、完成态被别人抹了」。
    """
    uid, uname = _user()
    target = _seed(uid, issue_number=90311)[0]
    neighbour = _seed(uid, issue_number=90312)[0]
    try:
        r = client.post(f"/api/issues/{target}/reopen", headers=_auth(uid, uname))
        assert r.status_code == 200, r.text
        assert _row(target)[0] == "processing" and (_row(target)[1] or "") == ""
        status, stage, completed_at, _reason = _row(neighbour)
        assert status == "completed" and stage == "done" and completed_at, "同仓其它需求不得被牵连"
    finally:
        _clear()


def test_reopen_does_not_redispatch(monkeypatch, seeded):
    """口径 B：不得自动派发（不碰 A2A / 不建新事件）。"""
    uid, uname, ids = seeded
    import urllib.request

    called = []

    def _boom(*a, **kw):
        called.append(a)
        raise AssertionError("reopen 不应发起任何派发请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    r = client.post(f"/api/issues/{ids[0]}/reopen", headers=_auth(uid, uname))
    assert r.status_code == 200, r.text
    assert called == [], "reopen 必须只改状态，不得自动重派"