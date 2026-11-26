"""Issue #4024：pending_review 审核点态读回口径统一（置位 → 立即读回 / 双 runtime 对账）。

验收对应：
- ① pending-review 置位后立即调用 `issues status get`，读数与置位响应一致；
- ② `issues detail.pipeline.review_status` / pipeline 路由不滞留 in_progress；
- ③ 双 runtime（status get 与 detail/pipeline 两侧）对账读数一致；
- ④ 聚合口径来源：`derive_review_status(issue.stage, issue.status)` 单一事实源。

测试不依赖账号/映射表，直接写入 issue 行（user_id=0 跳过会话/通知写侧），
以 `issue.id` 为待审核系列端点定位键，覆盖 README 中的 tk_ 调用形态。
"""
import os
import uuid
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.runtime.routes import issues as issues_mod
from src.kernel.session_manager import db, db_lock
from src.api.pipeline_status import (
    derive_review_status,
    REVIEW_STATUS_PENDING_REVIEW,
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_CANCELLED,
    REVIEW_STATUS_AWAITING_INPUT,
    REVIEW_STATUS_IN_PROGRESS,
    REVIEW_STATUS_UNKNOWN,
)

client = TestClient(app)

REPO_KEY = "gitcode:org/test4024"


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


def _session_headers(issue_number, agent_type="qa", repo_key=REPO_KEY):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i4024-{issue_number}-{uuid.uuid4().hex}", repo_key=repo_key,
        agent_type=agent_type, user_id=0)
    return {"Authorization": f"Bearer {tok}"}


def _seed(issue_number=4024, status="processing", stage="qa", repo_key=REPO_KEY):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (repo_key, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, "
            "status, stage, owner_agent) VALUES (0,'',?,?,?,?,?,?,?)",
            (repo_key, issue_number, "审核点态读回", "d", status, stage, "qa"))
        cid = c.lastrowid
        db.commit()
    return cid


def _persisted(issue_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage FROM issue WHERE id=?", (issue_id,)).fetchone()


def _post_pending_review(issue_id, issue_number, headers=None):
    with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
        return client.post(
            f"/api/issues/{issue_id}/pending-review",
            headers=headers or _session_headers(issue_number),
            json={"review_points": "请审核确认交付结果", "references": "PR#1"})


class TestPendingReviewReadbackUnified:
    def test_set_then_immediate_status_get_consistent(self):
        """① 置位响应与紧随其后的 status get 读数逐字段一致。"""
        cid = _seed(issue_number=4101)
        set_resp = _post_pending_review(cid, 4101)
        assert set_resp.status_code == 200
        set_data = set_resp.json()
        assert set_data["changed"] is True
        assert set_data["status"] == "pending_review"
        assert set_data["review_status"] == REVIEW_STATUS_PENDING_REVIEW

        get_data = client.get(
            f"/api/issues/{cid}/status", headers=_session_headers(4101)).json()
        assert get_data["status"] == set_data["status"]
        assert get_data["review_status"] == set_data["review_status"]
        assert get_data["stage"] == "qa"
        assert _persisted(cid)[0] == "pending_review"

    def test_pipeline_and_detail_review_status_not_in_progress(self):
        """② 置位后 pipeline / detail 的 review_status 立即为 pending_review。"""
        cid = _seed(issue_number=4102)
        _post_pending_review(cid, 4102)
        h = _session_headers(4102)

        detail = client.get("/api/issues/org/test4024/4102", headers=h).json()
        assert detail["task_status"] == "pending_review"
        assert detail["pipeline"]["review_status"] == REVIEW_STATUS_PENDING_REVIEW
        assert detail["pipeline"]["issue_task_status"] == "pending_review"

        pipeline = client.get("/api/issues/org/test4024/4102/pipeline", headers=h).json()
        assert pipeline["review_status"] == REVIEW_STATUS_PENDING_REVIEW
        assert pipeline["review_status"] != REVIEW_STATUS_IN_PROGRESS
        assert pipeline["issue_task_status"] == "pending_review"

    def test_multi_platform_alias_rows_single_source(self):
        """⑤ 多平台别名行（同 number 不同 repo_key）下四处读数一致，不被别名终态行带偏。"""
        alias_key = "github:org/test4024"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (alias_key, 4105))
            db.commit()
        canonical = _seed(issue_number=4105, status="processing", stage="qa")
        alias = _seed(issue_number=4105, status="completed", stage="done", repo_key=alias_key)
        assert alias > canonical
        h = _session_headers(4105)

        detail_before = client.get("/api/issues/org/test4024/4105", headers=h).json()
        assert detail_before["issue_id"] == canonical

        set_data = _post_pending_review(canonical, 4105).json()
        get_data = client.get(f"/api/issues/{canonical}/status", headers=h).json()
        detail = client.get("/api/issues/org/test4024/4105", headers=h).json()
        pipeline = client.get("/api/issues/org/test4024/4105/pipeline", headers=h).json()

        assert set_data["status"] == get_data["status"] == "pending_review"
        assert detail["task_status"] == pipeline["issue_task_status"] == "pending_review"
        assert (set_data["review_status"] == get_data["review_status"]
                == detail["pipeline"]["review_status"] == pipeline["review_status"]
                == REVIEW_STATUS_PENDING_REVIEW)
        assert get_data["stage"] == detail["stage"] == "qa"
        with db_lock:
            _alias_status = db.cursor().execute(
                "SELECT status FROM issue WHERE id=?", (alias,)).fetchone()[0]
        assert _alias_status == "completed"

    def test_multi_platform_alias_smaller_id_reverse_consistent(self):
        """⑤-reverse 别名在途行 id 更小（规范行非最小 id 方向）时四处读数仍单源一致。"""
        alias_key = "github:org/test4024"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (alias_key, 4106))
            db.commit()
        # 别名（在途）先入库 → id 更小；主平台（终态）后入库 → id 更大。
        alias = _seed(issue_number=4106, status="processing", stage="qa", repo_key=alias_key)
        primary = _seed(issue_number=4106, status="completed", stage="done")
        assert alias < primary
        h = _session_headers(4106, repo_key=alias_key)

        detail_before = client.get("/api/issues/org/test4024/4106", headers=h).json()
        assert detail_before["issue_id"] == alias

        set_data = _post_pending_review(alias, 4106, headers=h).json()
        get_data = client.get(f"/api/issues/{alias}/status", headers=h).json()
        detail = client.get("/api/issues/org/test4024/4106", headers=h).json()
        pipeline = client.get("/api/issues/org/test4024/4106/pipeline", headers=h).json()

        assert set_data["changed"] is True
        assert set_data["status"] == get_data["status"] == "pending_review"
        assert detail["task_status"] == pipeline["issue_task_status"] == "pending_review"
        assert (set_data["review_status"] == get_data["review_status"]
                == detail["pipeline"]["review_status"] == pipeline["review_status"]
                == REVIEW_STATUS_PENDING_REVIEW)
        assert get_data["stage"] == detail["stage"] == "qa"
        with db_lock:
            _primary_status = db.cursor().execute(
                "SELECT status FROM issue WHERE id=?", (primary,)).fetchone()[0]
        assert _primary_status == "completed"

    def test_dual_runtime_reconciliation_two_sides_agree(self):
        """③ 双 runtime 对账：status get 与 detail/pipeline 两侧 review_status 读数一致。"""
        cid = _seed(issue_number=4103)
        _post_pending_review(cid, 4103)
        h = _session_headers(4103)

        status_side = client.get(f"/api/issues/{cid}/status", headers=h).json()["review_status"]
        detail_side = client.get(
            "/api/issues/org/test4024/4103", headers=h).json()["pipeline"]["review_status"]
        pipeline_side = client.get(
            "/api/issues/org/test4024/4103/pipeline", headers=h).json()["review_status"]
        assert status_side == detail_side == pipeline_side == REVIEW_STATUS_PENDING_REVIEW

    def test_idempotent_set_keeps_readback_same(self):
        """重复置位（已 pending_review）幂等，读回仍同源同值。"""
        cid = _seed(issue_number=4104)
        _post_pending_review(cid, 4104)
        again = _post_pending_review(cid, 4104).json()
        assert again["changed"] is False and again["idempotent"] is True
        assert again["status"] == "pending_review"
        assert again["review_status"] == REVIEW_STATUS_PENDING_REVIEW
        get_data = client.get(
            f"/api/issues/{cid}/status", headers=_session_headers(4104)).json()
        assert get_data["review_status"] == again["review_status"]


class TestReviewStatusAggregationSource:
    def test_status_get_derives_from_issue_row(self):
        """④ status get 的 review_status 由同一 issue 行的 stage/status 派生。"""
        cases = [
            ("processing", "qa", REVIEW_STATUS_IN_PROGRESS),
            ("active", "qa", REVIEW_STATUS_IN_PROGRESS),
            ("awaiting_input", "qa", REVIEW_STATUS_AWAITING_INPUT),
            ("pending_review", "qa", REVIEW_STATUS_PENDING_REVIEW),
            ("processing", "done", REVIEW_STATUS_COMPLETED),
            ("completed", "qa", REVIEW_STATUS_COMPLETED),
            ("cancelled", "qa", REVIEW_STATUS_CANCELLED),
        ]
        for idx, (status, stage, expected) in enumerate(cases):
            num = 4200 + idx
            cid = _seed(issue_number=num, status=status, stage=stage)
            got = client.get(
                f"/api/issues/{cid}/status", headers=_session_headers(num)).json()
            assert got["status"] == status
            assert got["stage"] == stage
            assert got["review_status"] == expected

    def test_derive_review_status_contract(self):
        """锁定聚合口径优先级：pending_review > cancelled > completed/done > awaiting > 在途 > unknown。"""
        assert derive_review_status("qa", "pending_review") == REVIEW_STATUS_PENDING_REVIEW
        assert derive_review_status("qa", "cancelled") == REVIEW_STATUS_CANCELLED
        assert derive_review_status("done", "processing") == REVIEW_STATUS_COMPLETED
        assert derive_review_status("qa", "completed") == REVIEW_STATUS_COMPLETED
        assert derive_review_status("qa", "awaiting_input") == REVIEW_STATUS_AWAITING_INPUT
        assert derive_review_status("qa", "processing") == REVIEW_STATUS_IN_PROGRESS
        assert derive_review_status("", "active") == REVIEW_STATUS_IN_PROGRESS
        assert derive_review_status("", "") == REVIEW_STATUS_UNKNOWN