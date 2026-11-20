"""Issue #2419：飞书通知扩覆盖——pending_review / awaiting_input 状态事件统一分发。

修复项 2（Spec v2，评论 200696064）：任务进入审核点状态（pending_review / awaiting_input）
的全部入口路径均须推送飞书卡片（复用 #1211 机制与卡片模板 + 本人 chat bot instance 路由）。

覆盖切片（一 seam 一测试）：
- S1 `_review_reject`（review-reject → awaiting_input）：审核打回转澄清 → ❓ 需求待澄清（orange）
- S2 `auto_finalize_issue_on_last_node` failed-node 回退 → pending_review：🔎 需求待审核（red）
- S3 `close_issue_tasks`（Issue 关闭兜底 → pending_review）
- S4 `_close_issue_tasks_bot_takeover`（bot 接管兜底 → pending_review）
- S5 `PUT /api/issues/{id}/status`（用户状态迁移入审核点）

观测 seam（与 #1211 同款）：任务会话模板消息（add_message 路径）+ im-notify 计数器
（attempted / skipped_no_ctx——测试环境无 chat bot 时走 skip 可观测路径）。

显式端点（pending-review / awaiting-input）与 #1211 stage 包装器已有通知（回归门见
tests/test_issue_1211_*.py、test_issue_pending_review.py、test_issues_awaiting.py）。
"""
import os
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, get_messages
from src.kernel.session_manager import db as user_db
from src.domains.mgmt.auth import grape_account_db
from src.domains.runtime import issues_center as ic

REPO = "gitcode:org/issue2419"
_uid = None


def setup_module():
    from src.api.db_setup import init_database
    init_database()
    global _uid
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='review_point_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='rp_eric'")
        db.commit()
    _uid = grape_account_db.create_grape_account("review_point_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("review_point_user")
    # arch11 适配（D105）：grape_accounts 主键为 UUIDv7 字符串，unified_user_mapping.user_id
    # 为 INTEGER PK——不做全列 REPLACE（混型必炸），改走 ensure 回填行的 UPDATE 通道：
    # 按 gitcode_user 定位（无行则 INSERT user_id=0 哨兵行，open_id 查询链路只依赖本行文本列）。
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='rp_eric'")
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (0,?,?,?,datetime('now'))", ("rp_eric", "ou_rp_2419", "Eric"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                  "VALUES (?,?,?,?,1,'bot_2419_token')", (REPO, "org", "issue2419", "org/issue2419"))
        db.commit()
    try:
        user_db.mark_user_repo_join(_uid["id"], REPO, True, True)
    except Exception:
        pass  # arch11 user 表结构差异：join 记录非通知链路断言必需


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='review_point_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='rp_eric'")
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _seed_issue(issue_number, status="processing", stage=None):
    """建档（issue 表直插，含 stage 列——不经 set_issue_stage 同步，保留构造态）。返回行 id。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "rp_eric", REPO, issue_number, "审核点通知需求", status, stage or ""))
        _cid = c.lastrowid
        db.commit()
        return _cid


def _issue_status(issue_number):
    with db_lock:
        return db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
            (REPO, issue_number)).fetchone()


def _conv_msg_count(issue_number, marker):
    """任务会话中含 marker 的模板消息条数（通知链路的会话兜底观测点）。"""
    with db_lock:
        row = db.cursor().execute(
            "SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=?",
            (REPO, issue_number)).fetchone()
    if not row or not row[0]:
        return 0, ""
    msgs = get_messages(row[0], limit=50)
    return len([m for m in msgs if marker in (m.get("content") or "")]), row[0]


def _seed_events(issue_number, statuses: dict):
    """按 agent_type 造事件（每 agent 一条最新事件，与 test_issue_656 同路径）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=? AND issue_id=?", (REPO, f"#{issue_number}"))
        for _ag, _st in statuses.items():
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) VALUES (?,?,?,?,?)",
                (REPO, f"#{issue_number}", "a2a_event", _ag, _st))
        db.commit()


_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}


class TestCloseIssueTasksNotify:
    """S3：Issue 关闭兜底 close_issue_tasks → pending_review → 🔎 需求待审核通知。

    覆盖两分支：无 issue_state/非终态且无 running agent（active/processing → pending_review）；
    stage 已是 pending_review（active → pending_review）。stage=done → completed 不通知。
    """

    def test_issue_closed_no_running_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(67, status="processing", stage=None)
        n = ic.close_issue_tasks(REPO, 67)
        assert _issue_status(67)[0] == "pending_review"
        assert n == 1
        count, conv_id = _conv_msg_count(67, "🔎 需要你审核确认")
        assert count == 1, "Issue 关闭兜底转待审核应写入审核通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "🔎 需要你审核确认" in (m.get("content") or "")), "")
        assert "Issue 已关闭" in body, "审核点文案应说明关闭来源"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1

    def test_issue_closed_stage_pending_review_active_promotes(self):
        """stage=pending_review 分支：active 任务推进到 pending_review 也须通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(68, status="active", stage="pending_review")
        ic.close_issue_tasks(REPO, 68)
        assert _issue_status(68)[0] == "pending_review"
        count, _ = _conv_msg_count(68, "🔎 需要你审核确认")
        assert count == 1

    def test_issue_closed_stage_done_completes_no_notify(self):
        """回归：stage=done → 直接 completed，不触发待审核通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(69, status="processing", stage="done")
        ic.close_issue_tasks(REPO, 69)
        assert _issue_status(69)[0] == "completed"
        count, _ = _conv_msg_count(69, "🔎 需要你审核确认")
        assert count == 0

    def test_issue_closed_repeat_no_duplicate(self):
        """重复调用（状态已 pending_review）→ UPDATE 0 行，不重复通知（幂等）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(70, status="pending_review", stage=None)
        ic.close_issue_tasks(REPO, 70)
        count, _ = _conv_msg_count(70, "🔎 需要你审核确认")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0


class TestAutoFinalizeFallbackNotify:
    """S2：自动结单 failed-node 回退 pending_review → 🔎 需求待审核通知（#2419 扩覆盖）。"""

    def test_failed_node_fallback_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(65, status="processing", stage="sre")
        _seed_events(65, {**_ALL_COMPLETED, "qa": "failed"})
        out = ic.auto_finalize_issue_on_last_node(REPO, "#65", "sre", source="test")
        assert out == "pending_review_fallback", out
        assert _issue_status(65)[0] == "pending_review"
        count, conv_id = _conv_msg_count(65, "🔎 需要你审核确认")
        assert count == 1, "failed-node 回退待审核应写入审核通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "🔎 需要你审核确认" in (m.get("content") or "")), "")
        assert "qa" in body, "审核点文案应指明失败节点，供用户拍板后续处理"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1

    def test_auto_done_no_review_point_notify(self):
        """回归：全节点完成 auto_done（completed 终态）→ 不发待审核通知（仅审核点进入触发）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(66, status="processing", stage="sre")
        _seed_events(66, _ALL_COMPLETED)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.get_issue_state.return_value = "open"
            _cli.return_value.close_issue.return_value = {"ok": True}
            _cli.return_value.list_prs.return_value = []
            out = ic.auto_finalize_issue_on_last_node(REPO, "#66", "sre", source="test")
        assert out == "auto_done", out
        count, _ = _conv_msg_count(66, "🔎 需要你审核确认")
        assert count == 0, "auto_done 非 pending_review，不应触发待审核通知"


class TestBotTakeoverNotify:
    """S4：bot 接管兜底 → pending_review → 🔎 需求待审核通知（#2419 扩覆盖）。"""

    def test_takeover_no_running_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(71, status="processing", stage=None)
        n = ic._close_issue_tasks_bot_takeover(REPO, 71)
        assert _issue_status(71)[0] == "pending_review"
        assert n == 1
        count, conv_id = _conv_msg_count(71, "🔎 需要你审核确认")
        assert count == 1, "bot 接管转待审核应写入审核通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "🔎 需要你审核确认" in (m.get("content") or "")), "")
        assert "接管" in body, "审核点文案应说明接管来源"
        assert ic.get_im_notify_counters().get("attempted") == 1

    def test_takeover_stage_done_early_return_no_notify(self):
        """回归：stage=done 早退（本已终态），不通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(72, status="completed", stage="done")
        n = ic._close_issue_tasks_bot_takeover(REPO, 72)
        assert n == 0
        count, _ = _conv_msg_count(72, "🔎 需要你审核确认")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0

    def test_takeover_repeat_no_duplicate(self):
        """重复接管（已 pending_review）→ 0 行变更，不重复通知（幂等）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(73, status="pending_review", stage=None)
        ic._close_issue_tasks_bot_takeover(REPO, 73)
        count, _ = _conv_msg_count(73, "🔎 需要你审核确认")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0


class TestPutStatusRouteNotify:
    """S5：用户 PUT /api/issues/{id}/status 迁移入审核点状态 → 🔎/❓ 卡片通知（#2419 扩覆盖）。"""

    def _put(self, cid, status):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user_or_api_key
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {
            "id": _uid["id"], "username": "review_point_user", "auth_type": "api_key"}
        try:
            client = TestClient(app)
            return client.put(f"/api/issues/{cid}/status", json={"status": status})
        finally:
            app.dependency_overrides.pop(get_current_user_or_api_key, None)

    def test_put_to_pending_review_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        cid = _seed_issue(74, status="processing")
        r = self._put(cid, "pending_review")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert _issue_status(74)[0] == "pending_review"
        count, _ = _conv_msg_count(74, "🔎 需要你审核确认")
        assert count == 1, "PUT 迁移入待审核应触发审核通知"
        assert ic.get_im_notify_counters().get("attempted") == 1

    def test_put_to_awaiting_input_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        cid = _seed_issue(75, status="processing")
        r = self._put(cid, "awaiting_input")
        assert r.status_code == 200, r.text
        count, _ = _conv_msg_count(75, "❓ 需要你补充信息")
        assert count == 1, "PUT 迁移入待澄清应触发澄清通知"
        assert ic.get_im_notify_counters().get("attempted") == 1

    def test_put_same_status_no_notify(self):
        """同状态重写（cur==ns 非真实变更）→ 不通知（幂等守卫）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        cid = _seed_issue(76, status="pending_review")
        r = self._put(cid, "pending_review")
        assert r.status_code == 200, r.text
        count, _ = _conv_msg_count(76, "🔎 需要你审核确认")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0

    def test_put_to_cancelled_no_notify(self):
        """非审核点状态（cancelled）→ 不通知（防误报守卫）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        cid = _seed_issue(77, status="processing")
        r = self._put(cid, "cancelled")
        assert r.status_code == 200, r.text
        assert _issue_status(77)[0] == "cancelled"
        assert ic.get_im_notify_counters().get("attempted", 0) == 0


class TestReviewRejectAwaitingNotify:
    """S1：review-reject → awaiting_input 真实变更 → ❓ 需求待澄清通知（#2419 扩覆盖）。"""

    def test_reject_to_awaiting_notifies(self):
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(61, status="pending_review")
        out = ic._review_reject(REPO, 61, target="awaiting_input", reason="部署/运维二选一待用户确认")
        assert out == "rejected_awaiting_input", out
        assert _issue_status(61)[0] == "awaiting_input"
        count, conv_id = _conv_msg_count(61, "❓ 需要你补充信息")
        assert count == 1, "打回转澄清应写入待澄清通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "❓ 需要你补充信息" in (m.get("content") or "")), "")
        assert "部署/运维二选一待用户确认" in body, "reject reason 应作为澄清问题透传"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1  # 测试环境无 chat bot → skip 可观测

    def test_reject_to_processing_no_notify(self):
        """target=processing → 不通知（仅审核点状态触发，防误报）。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(62, status="pending_review")
        out = ic._review_reject(REPO, 62, target="processing", reason="继续修改")
        assert out == "rejected_processing", out
        count, _ = _conv_msg_count(62, "❓ 需要你补充信息")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0

    def test_reject_repeat_no_duplicate(self):
        """已离开 pending_review 再 reject → noop 幂等，不重复通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_issue(63, status="awaiting_input")
        out = ic._review_reject(REPO, 63, target="awaiting_input", reason="再打回")
        # arch11 适配（#1599）：noop 返回细化分类（noop:already_target 等），前缀匹配。
        assert out.startswith("noop"), out
        count, _ = _conv_msg_count(63, "❓ 需要你补充信息")
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0
