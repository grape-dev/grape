"""Issue #667：issues detail / awaiting-input 兜底解析与 404 语义区分测试。

覆盖：
- detail 三表未命中 → VCS 核实存在 → 自动补建内部行并 200（synced_by_fallback）
- detail 404 reason=issue_not_found（VCS 侧明确 404）
- detail 404 reason=internal_sync_missing（VCS 无法核实）
- awaiting-input 卡片 id 缺行 → body.issue_number 兜底补建并迁移 awaiting_input
- _inject_issue_pr_env：GRAPE_ISSUE_ID 空值时自动补建行后重解析
"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import issues as issues_mod
from src.api import event_pipeline as ep_mod
from src.session_manager import db, db_lock
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
OWNER = "i667_org"
REPO = "repo"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _session_headers(repo_key=REPO_KEY, agent_type="vod"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i667-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


def _clean(number):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO_KEY, number))
        c.execute("DELETE FROM events WHERE repo_key=? AND issue_id=?", (REPO_KEY, f"#{number}"))
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key=? AND issue_id=?", (REPO_KEY, f"#{number}"))
        except Exception:
            pass
        db.commit()


def _has_row(number):
    with db_lock:
        return db.cursor().execute(
            "SELECT id, status FROM issue WHERE repo_key=? AND issue_number=?",
            (REPO_KEY, number)).fetchone()


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    _uid = grape_account_db.create_grape_account("i667_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("i667_user")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        db.commit()


# ===================== issues detail 兜底解析 =====================

class TestDetailFallback:
    def test_detail_fallback_creates_row_and_returns_200(self):
        _clean(501)
        with patch.object(ep_mod, "fetch_issue_via_vcs",
                          return_value=("exists", {"title": "新 Issue 667"})):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/501",
                           headers={"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["number"] == 501
        assert d.get("synced_by_fallback") is True
        assert d["issue_id"]
        row = _has_row(501)
        assert row and row[0] == d["issue_id"]

    def test_detail_second_call_hits_internal_no_vcs(self):
        # 首次兜底补建后，第二次不再触发 VCS（synced_by_fallback 不出现）
        _clean(502)
        _jwt = {"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"}
        with patch.object(ep_mod, "fetch_issue_via_vcs",
                          return_value=("exists", {"title": "T"})) as m:
            r1 = client.get(f"/api/issues/{OWNER}/{REPO}/502", headers=_jwt)
            r2 = client.get(f"/api/issues/{OWNER}/{REPO}/502", headers=_jwt)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json().get("synced_by_fallback") is True
        assert "synced_by_fallback" not in r2.json()
        assert m.call_count == 1

    def test_detail_404_reason_issue_not_found(self):
        _clean(503)
        with patch.object(ep_mod, "fetch_issue_via_vcs", return_value=("missing", {})):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/503",
                           headers={"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"})
        assert r.status_code == 404
        det = r.json()["detail"]
        assert det["reason"] == "issue_not_found"
        assert "suggestion" in det
        assert not _has_row(503)

    def test_detail_404_reason_internal_sync_missing(self):
        _clean(504)
        with patch.object(ep_mod, "fetch_issue_via_vcs", return_value=("unknown", {})):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/504",
                           headers={"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"})
        assert r.status_code == 404
        det = r.json()["detail"]
        assert det["reason"] == "internal_sync_missing"
        assert "suggestion" in det
        assert not _has_row(504)


# ===================== awaiting-input 兜底解析 =====================

class TestAwaitingInputFallback:
    def test_awaiting_input_issue_number_fallback(self):
        _clean(505)
        with patch.object(ep_mod, "fetch_issue_via_vcs",
                          return_value=("exists", {"title": "AI 缺行"})):
            with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post("/api/issues/999999/awaiting-input",
                                headers=_session_headers(agent_type="vod"),
                                json={"questions": "请补充复现步骤", "issue_number": 505})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["changed"] is True
        assert d["status"] == "awaiting_input"
        row = _has_row(505)
        assert row and row[1] == "awaiting_input"

    def test_awaiting_input_fallback_vcs_missing_404(self):
        _clean(506)
        with patch.object(ep_mod, "fetch_issue_via_vcs", return_value=("missing", {})):
            r = client.post("/api/issues/999998/awaiting-input",
                            headers=_session_headers(agent_type="vod"),
                            json={"questions": "q", "issue_number": 506})
        assert r.status_code == 404
        det = r.json()["detail"]
        assert det["reason"] == "issue_not_found"

    def test_awaiting_input_no_fallback_reason_internal_card_missing(self):
        r = client.post("/api/issues/999997/awaiting-input",
                        headers=_session_headers(agent_type="vod"),
                        json={"questions": "q"})
        assert r.status_code == 404
        det = r.json()["detail"]
        assert det["reason"] == "internal_card_missing"
        assert "hint" in det

    def test_awaiting_input_fallback_scoped_to_session_repo(self):
        # issue_number 兜底仅在会话仓库作用域内解析：他仓会话看不到本仓卡行 → 404（不跨仓泄漏）
        _clean(507)
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, stage) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (0, "", REPO_KEY, 507, "本仓行", "d", "active", "vod"))
            db.commit()
        with patch.object(ep_mod, "fetch_issue_via_vcs", return_value=("unknown", {})):
            r = client.post("/api/issues/999996/awaiting-input",
                            headers=_session_headers(repo_key="gitcode:org/other", agent_type="vod"),
                            json={"questions": "q", "issue_number": 507})
        assert r.status_code == 404
        det = r.json()["detail"]
        assert det["reason"] == "internal_card_missing"
        # 本仓卡行未被误消费，状态保持 active
        assert _has_row(507)[1] == "active"


# ===================== GRAPE_ISSUE_ID 注入时序修复 =====================

class TestInjectEnvBackfill:
    def test_inject_env_backfills_missing_card_row(self):
        _clean(508)
        env = {}
        evt = {"issue_id": "508", "title": "时序缺行", "event_type": "issue_opened", "payload": {}}
        ep_mod._inject_issue_pr_env(env, evt, REPO_KEY)
        assert env["ISSUE_NUMBER"] == "508"
        assert env["GRAPE_ISSUE_ID"]
        row = _has_row(508)
        assert row and str(row[0]) == env["GRAPE_ISSUE_ID"]

    def test_inject_env_existing_row_no_extra_write(self):
        _clean(509)
        from src.api.issues_center import ensure_issue_task
        cid = ensure_issue_task(REPO_KEY, 509, title="已有", event_type="issue_opened")
        env = {}
        evt = {"issue_id": "509", "event_type": "issue_comment", "payload": {}}
        with patch.object(ep_mod, "resolve_issue_card_id", wraps=ep_mod.resolve_issue_card_id) as m:
            ep_mod._inject_issue_pr_env(env, evt, REPO_KEY)
        assert env["GRAPE_ISSUE_ID"] == str(cid)
        assert m.call_count == 1  # 命中即不再补建重解析
