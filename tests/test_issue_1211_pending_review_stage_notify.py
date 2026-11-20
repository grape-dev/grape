"""Issue #1211 缺口 2：set_issue_stage 流转 pending_review 不通知的修复测试。

- wrapper 单元：set_issue_stage_with_pending_review_notify —— 仅 stage 真实变更为
  pending_review（且 issue.status 同步为 pending_review）才调度待审核通知；
- webhook 集成：POST /api/a2a 末节点完成 → stage=pending_review → 通知（复刻 #1085/#1087 场景）；
- flow/reset 非回归见 tests/test_event_conversation.py（to_stage 白名单不含 pending_review，
  该处为防御性接线，正常路径不触发通知）。
"""
import os
import json
import asyncio
import threading
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.domains.runtime import issues_center as ic
from src.session_manager import db, db_lock, get_messages
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

REPO = "gitcode:org/issue1211"
_uid = None


def setup_module():
    from src.api.db_setup import init_database
    init_database()
    global _uid
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='pr_merge_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='pr_merge_eric'")
        db.commit()
    _uid = grape_account_db.create_grape_account("pr_merge_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("pr_merge_user")
    with db_lock:
        c = db.cursor()
        # arch11 D105 适配：UUID 串主键 × INTEGER PK 混型必炸——插哨兵行。
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (0,?,?,?,datetime('now'))", ("pr_merge_eric", "ou_pr_merge", "Eric"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                  "VALUES ('gitcode:org/issue1211','org','issue1211','org/issue1211',1,'bot_1211_token')")
        db.commit()
    try:
        user_db.mark_user_repo_join(_uid["id"], REPO, True, True)
    except Exception:
        pass  # arch11 user 表结构差异：非本断言必需


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='pr_merge_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='pr_merge_eric'")
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _seed_stage_issue(issue_number, status="processing", stage="qa"):
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES (?,?,?,?,?,?)",
            (_uid["id"], "pr_merge_eric", REPO, issue_number, "插件需求", status))
        db.commit()
    if stage:
        set_issue_stage(REPO, f"#{issue_number}", stage, stage)


def _issue_row(issue_number):
    with db_lock:
        c = db.cursor()
        return c.execute("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
                         (REPO, issue_number)).fetchone()


def _review_msg_count(issue_number):
    with db_lock:
        row = db.cursor().execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=?",
                                  (REPO, issue_number)).fetchone()
    if not row or not row[0]:
        return 0, ""
    msgs = get_messages(row[0], limit=50)
    return len([m for m in msgs if "🔎 需要你审核确认" in (m.get("content") or "")]), row[0]


class TestStagePendingReviewNotifyWrapper:

    def test_real_change_to_pending_review_notifies(self):
        """stage qa→pending_review + status processing→pending_review（真实变更）→ 待审核通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_stage_issue(51, status="processing", stage="qa")
        ic.set_issue_stage_with_pending_review_notify(
            REPO, "#51", "pending_review", "qa", review_points="末节点已完成，请审核交付结果")
        st = _issue_row(51)
        assert st[0] == "pending_review" and st[1] == "pending_review"
        count, conv_id = _review_msg_count(51)
        assert count == 1, "真实变更应写入待审核通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "🔎 需要你审核确认" in (m.get("content") or "")), "")
        assert "末节点已完成，请审核交付结果" in body
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1  # 测试环境无 chat bot → skip 可观测

    def test_repeat_entry_no_duplicate(self):
        """已是 pending_review 再置 pending_review（stage 未变）→ 不重复通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_stage_issue(52, status="processing", stage="pending_review")
        ic.set_issue_stage_with_pending_review_notify(
            REPO, "#52", "pending_review", "qa", review_points="再次进入")
        count, _ = _review_msg_count(52)
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0

    def test_awaiting_input_status_not_notified(self):
        """status=awaiting_input（等澄清）时 stage 置 pending_review → status 保持、不通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_stage_issue(53, status="awaiting_input", stage="qa")
        ic.set_issue_stage_with_pending_review_notify(
            REPO, "#53", "pending_review", "qa", review_points="x")
        st = _issue_row(53)
        assert st[0] == "awaiting_input", "set_issue_stage 同步不应覆盖 awaiting_input"
        count, _ = _review_msg_count(53)
        assert count == 0

    def test_non_pending_stage_passthrough_no_notify(self):
        """非 pending_review stage → 纯透传（行为与 set_issue_stage 一致），不通知。"""
        ic._IM_NOTIFY_COUNTERS.clear()
        _seed_stage_issue(54, status="processing", stage="issue")
        ic.set_issue_stage_with_pending_review_notify(REPO, "#54", "coding", "issue")
        st = _issue_row(54)
        assert st[1] == "coding" and st[0] == "processing"
        count, _ = _review_msg_count(54)
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0


class TestA2AWebhookPendingReviewNotify:
    """webhook.py a2a 末节点 → pending_review → 通知（缺口 2 真实可达路径，复刻 #1085 场景）。"""

    def test_last_node_a2a_triggers_pending_review_notify(self, tmp_path, monkeypatch):
        ic._IM_NOTIFY_COUNTERS.clear()
        # 假场景（自定义节点 id，末节点 next=done）+ webhook 模块 __file__ 重定向
        # arch11 适配：webhook 落位 domains 深 1 级（dirname×4）——pipelines.json 建到 tmp/src/configs
        _cfg_dir = tmp_path / "src" / "configs"
        _cfg_dir.mkdir(parents=True)
        (_cfg_dir / "pipelines.json").write_text(json.dumps(
            {"scenes": {"scene-1211": {"nodes": [{"id": "custom-qa", "next": "done"}]}}}),
            encoding="utf-8")
        from src.domains.runtime.routes import webhook as webhook_mod
        from src.api import session_store
        _orig_gis = session_store.get_issue_scene
        # arch11 适配：webhook 落位 src/domains/runtime/routes/（读 pipelines.json 用 dirname×4）
        monkeypatch.setattr(webhook_mod, "__file__", str(tmp_path / "src/domains/runtime/routes/webhook.py"))

        def _fake_scene(rk, iid, *a, **k):
            if rk == REPO:
                return ("scene-1211", 0)
            return _orig_gis(rk, iid, *a, **k)
        monkeypatch.setattr(session_store, "get_issue_scene", _fake_scene)

        _seed_stage_issue(55, status="processing", stage="custom-qa")

        def _fire_inline(coro):
            t = threading.Thread(target=asyncio.run, args=(coro,))
            t.start()
            t.join()
        with patch("src.domains.runtime.issues_center._fire_notify_coro", side_effect=_fire_inline):
            r = client.post("/api/a2a",
                            headers={"Authorization": "Bearer bot_1211_token"},
                            json={"agent": "qa", "payload": {
                                "repository": "org/issue1211",
                                "account": "org/issue1211",
                                "issue_number": "55",
                                "pr_number": "",
                                "context": "QA 两轴审查完成，等待人工确认"}})
        assert r.status_code == 200, r.text
        st = _issue_row(55)
        assert st[0] == "pending_review", st
        assert st[1] == "pending_review", st
        count, conv_id = _review_msg_count(55)
        assert count == 1, "a2a 末节点进入 pending_review 应触发待审核通知"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "🔎 需要你审核确认" in (m.get("content") or "")), "")
        assert "QA 两轴审查完成，等待人工确认" in body, "A2A context 须作为审核点透传"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
