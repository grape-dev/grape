"""Issue #4103：pending-review 回执 prior_status/changed 对账语义测试。

覆盖三类验收：
1. 幂等：写入前 status=pending_review 且无并发事件时，changed=false 且不产生状态写入；
2. 字段一致：回执 prior_status 与写入前 `issues status get` 的 status 完全一致；
3. 并发挤出：审核点被派发挤出为 active 后复置，回执 prior_status=active /
   status=pending_review / changed=true，调用方无需额外复读推断。
"""
import os
import uuid
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api import auth_token
from src.domains.runtime.routes import issues as issues_mod
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

_REPO = "gitcode:org/issue4103"
_NUM = 4103


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


def _headers(agent_type="qa"):
    tok = auth_token.issue_session_token(
        session_key=f"i4103-{uuid.uuid4().hex}", repo_key=_REPO,
        agent_type=agent_type, user_id="0")
    return {"Authorization": f"Bearer {tok}"}


def _ensure_probe():
    """状态写入探针：记录 issue.status 的真实 UPDATE（用于断言「无写入」）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS issue_status_probe ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, repo_key TEXT, issue_number INTEGER, "
            "old_status TEXT, new_status TEXT, at TEXT)")
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_issue_status_probe "
            "AFTER UPDATE OF status ON issue "
            "WHEN NEW.status IS NOT OLD.status "
            "BEGIN "
            "INSERT INTO issue_status_probe (repo_key, issue_number, old_status, new_status, at) "
            "VALUES (OLD.repo_key, OLD.issue_number, OLD.status, NEW.status, datetime('now')); "
            "END")
        db.commit()


def setup_module():
    from src.api.db_setup import init_database
    init_database()
    _ensure_probe()


def teardown_module():
    """清理探针触发器/表，避免全局 CREATE TRIGGER 污染共享 test_sessions.db。"""
    with db_lock:
        c = db.cursor()
        c.execute("DROP TRIGGER IF EXISTS trg_issue_status_probe")
        c.execute("DROP TABLE IF EXISTS issue_status_probe")
        db.commit()


def _clear_probe(issue_number=_NUM):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue_status_probe WHERE repo_key=? AND issue_number=?",
                  (_REPO, issue_number))
        db.commit()


def _probe_rows(issue_number=_NUM):
    with db_lock:
        return db.cursor().execute(
            "SELECT old_status, new_status FROM issue_status_probe "
            "WHERE repo_key=? AND issue_number=? ORDER BY id",
            (_REPO, issue_number)).fetchall()


def _seed(status="processing", issue_number=_NUM, stage="vod"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?",
                  (_REPO, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, stage, owner_agent) VALUES (?,?,?,?,?,?,?,?,?)",
            (0, "", _REPO, issue_number, "Issue #4103 测试", "d", status, stage, stage))
        card_id = c.lastrowid
        db.commit()
    _clear_probe(issue_number)
    return card_id


def _status(issue_number=_NUM):
    with db_lock:
        return (db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
            (_REPO, issue_number)).fetchone() or [""])[0]


class TestPendingReviewPriorStatus:
    def test_idempotent_returns_false_without_status_write(self):
        cid = _seed(status="pending_review")
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow) as _ct:
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_headers(), json={"review_points": "再次提交"})
        assert r.status_code == 200
        data = r.json()
        assert data["prior_status"] == "pending_review"
        assert data["status"] == "pending_review"
        assert data["changed"] is False
        assert data["idempotent"] is True
        # 不放宽为「状态仍等于 pending_review」：要求本次真实零状态写入。
        assert _probe_rows() == []
        _ct.assert_not_called()

    def test_prior_status_matches_status_get_before_write(self):
        cid = _seed(status="processing")
        pre = client.get(f"/api/issues/{cid}/status", headers=_headers())
        assert pre.status_code == 200
        before = pre.json()["status"]
        assert before == "processing"

        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_headers(), json={"review_points": "请确认合入"})
        assert r.status_code == 200
        data = r.json()
        assert data["prior_status"] == before
        assert data["status"] == "pending_review"
        assert data["changed"] is True
        assert _probe_rows() == [("processing", "pending_review")]

    def test_concurrent_eviction_then_restore_is_self_explanatory(self):
        cid = _seed(status="pending_review")
        # 模拟派发侧并发事件把审核点挤出为 active（#4103 复置场景）。
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET status='active' WHERE id=?", (cid,))
            db.commit()
        _clear_probe()

        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_headers(), json={"review_points": "被挤出后复置"})
        assert r.status_code == 200
        data = r.json()
        assert data["prior_status"] == "active"
        assert data["status"] == "pending_review"
        assert data["changed"] is True
        assert data["idempotent"] is False
        assert _probe_rows() == [("active", "pending_review")]
        assert _status() == "pending_review"

    def test_cas_miss_does_not_report_changed_or_notify(self):
        """读后、UPDATE 前他进程抢先置位 → 本次条件更新命中 0 行。

        必须以 rowcount 判定 changed：结果为 prior=active/status=pending_review
        但本次零写入，changed=false 且不得重复触发待审核通知。
        """
        cid = _seed(status="active")
        _clear_probe()

        from src.domains.runtime import issues_center as ic
        real_read = ic._read_issue_status
        calls = {"n": 0}

        def _read_then_steal(repo_key, issue_number, c=None):
            value = real_read(repo_key, issue_number, c=c)
            calls["n"] += 1
            if calls["n"] == 1:
                # 模拟另一进程在 SELECT 与 UPDATE 之间把状态抢占为 pending_review。
                with db_lock:
                    c2 = db.cursor()
                    c2.execute("UPDATE issue SET status='pending_review' WHERE id=?", (cid,))
                    db.commit()
            return value

        with patch.object(ic, "_read_issue_status", side_effect=_read_then_steal):
            with patch.object(issues_mod.asyncio, "create_task",
                              side_effect=_swallow) as _ct:
                r = client.post(f"/api/issues/{cid}/pending-review",
                                headers=_headers(), json={"review_points": "CAS 未命中"})

        assert r.status_code == 200
        data = r.json()
        assert data["prior_status"] == "active"
        assert data["status"] == "pending_review"
        assert data["changed"] is False
        assert data["idempotent"] is True
        _ct.assert_not_called()
        # 仅外部抢占那一次写入，本次条件更新未命中 → 探针无第二条记录。
        assert _probe_rows() == [("active", "pending_review")]
