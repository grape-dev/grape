"""Issue #1211：QA 通过出 PR 后「📮 有 PR 待合入」通知测试。

切片（seam 计划见 Issue 评论）：
- A 通知 runner：_run_pr_pending_merge_notify（卡片内容/计数/会话模板兜底）
- B 发现+去重：maybe_notify_prs_pending_merge（open PR 扫描/关联匹配/(repo_key, pr_number) 幂等）
- C review-pass 端点接线：POST /api/issues/{id}/review-pass（target=done + pr_number 透传）
- D 自动结单挂钩：auto_finalize_issue_on_last_node（auto_done → 通知）
"""
import os
import json
import asyncio
from unittest.mock import patch, AsyncMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.domains.runtime import issues_center as ic
from src.session_manager import db, db_lock, get_messages, create_conversation
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

REPO = "gitcode:org/issue1211"
_uid = None

_PR_INFO = {
    "number": 12, "title": "feat: #34 插件需求", "url": "https://gitcode.com/org/test/pulls/12",
    "head": "feat/issue-34", "base": "weekly-1102-zrr",
    "files": 5, "additions": 120, "deletions": 30,
}


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


def _clear_state():
    ic._IM_NOTIFY_COUNTERS.clear()
    ic._PR_PENDING_MERGE_NOTIFIED.clear()


def _seed_issue(issue_number=34, status="processing", stage="qa", conv_id=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage, conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_uid["id"], "pr_merge_eric", REPO, issue_number, "插件需求 #34", status, stage, conv_id))
        db.commit()


def _issue_conv_count(issue_number=34):
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=?",
                        (REPO, issue_number)).fetchone()
    if not row or not row[0]:
        return 0, ""
    msgs = get_messages(row[0], limit=50)
    return len([m for m in msgs if "📮 PR #" in (m.get("content") or "")]), row[0]


def _pr(n=12, title="feat: #34 插件需求", state="open", body="", extra=None):
    pr = {"number": n, "title": title, "state": state, "body": body,
          "html_url": f"https://gitcode.com/org/issue1211/pulls/{n}",
          "head": {"ref": f"feat/issue-34"}, "base": {"ref": "weekly-1102-zrr"}}
    if extra:
        pr.update(extra)
    return pr


# ===== 切片 B：发现 + 去重 + 入口 =====

class TestMaybeNotifyPrsPendingMerge:

    def test_scan_matches_pr_by_title_ref_and_notifies(self):
        """扫描 open PR：title 含 #34 引用的 PR 命中 → 会话收到待合入模板 + skip 计数（无 chat bot）。"""
        _clear_state()
        _seed_issue()
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.list_prs.return_value = [
                _pr(12, title="feat: #34 插件需求"),
                _pr(13, title="chore: 无关改动"),
            ]
            _cli.return_value.get_pr_files.return_value = [
                {"additions": 120, "deletions": 30}, {"additions": 5, "deletions": 1}]
            n = ic.maybe_notify_prs_pending_merge(REPO, 34, qa_summary="两轴审查通过")
            assert n == 1, "仅关联 PR 通知一次"
            _cli.return_value.list_prs.assert_called_once()
        count, conv_id = _issue_conv_count()
        assert count == 1, "会话应写入一条待合入模板"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "📮 PR #" in (m.get("content") or "")), "")
        assert "📮 PR #12 待合入" in body and "pulls/12" in body
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1
        assert counts.get("sent_ok", 0) == 0

    def test_scan_matches_native_association(self):
        """GitCode 原生关联（issues 数组 / inner_issue_nums）也命中。"""
        _clear_state()
        _seed_issue(issue_number=35)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.list_prs.return_value = [
                _pr(21, title="add plugin", extra={"issues": [{"number": 35}]}),
                _pr(22, title="fix bug", extra={"inner_issue_nums": "org/issue1211#35"}),
            ]
            _cli.return_value.get_pr_files.return_value = []
            n = ic.maybe_notify_prs_pending_merge(REPO, 35)
            assert n == 2, "两条原生关联 PR 均应通知"
        count, _ = _issue_conv_count(35)
        assert count == 2

    def test_same_pr_second_review_not_duplicate(self):
        """同一 PR 二次 QA 复审通过 → 不重复发（skipped_duplicate 计数、会话不新增）。"""
        _clear_state()
        _seed_issue()
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.list_prs.return_value = [_pr(12, title="feat: #34 插件需求")]
            _cli.return_value.get_pr_files.return_value = []
            assert ic.maybe_notify_prs_pending_merge(REPO, 34, qa_summary="一审通过") == 1
            assert ic.maybe_notify_prs_pending_merge(REPO, 34, qa_summary="复审通过") == 0
        count, _ = _issue_conv_count()
        assert count == 1, "复审不应重复写模板"
        counts = ic.get_im_notify_counters()
        assert counts.get("skipped_duplicate") == 1
        assert counts.get("attempted") == 1

    def test_no_open_pr_skips(self):
        """无关联 open PR → skipped_no_open_pr，不解析目标、不写会话。"""
        _clear_state()
        _seed_issue()
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.list_prs.return_value = [_pr(13, title="chore: 无关改动")]
            assert ic.maybe_notify_prs_pending_merge(REPO, 34) == 0
        count, _ = _issue_conv_count()
        assert count == 0
        assert ic.get_im_notify_counters().get("skipped_no_open_pr") == 1

    def test_hint_path_uses_get_pr_and_open_state_gate(self):
        """pr_number_hint 透传 → 直接取详情；state 非 open（已合入）不通知。"""
        _clear_state()
        _seed_issue(issue_number=36)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.get_pr.return_value = _pr(40, title="feat: #36 需求", state="open")
            _cli.return_value.get_pr_files.return_value = [{"additions": 10, "deletions": 2}]
            assert ic.maybe_notify_prs_pending_merge(REPO, 36, qa_summary="QA 通过", pr_number_hint=40) == 1
            _cli.return_value.list_prs.assert_not_called()
            _cli.return_value.get_pr.assert_called_once_with(40)
        count, _ = _issue_conv_count(36)
        assert count == 1

        # 已合入（closed/merged）→ 不通知
        _clear_state()
        _seed_issue(issue_number=37)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.get_pr.return_value = _pr(41, title="feat: #37 需求", state="merged")
            assert ic.maybe_notify_prs_pending_merge(REPO, 37, pr_number_hint=41) == 0
        count, _ = _issue_conv_count(37)
        assert count == 0
        assert ic.get_im_notify_counters().get("skipped_no_open_pr") == 1


# ===== 切片 A：通知 runner =====

class TestRunnerPrPendingMerge:

    def test_send_ok_card_content_and_counters(self):
        """有 chat bot → 卡片发出：title/template/PR 链接/分支/增删行数/QA 结论齐备 + sent_ok。"""
        _clear_state()
        ctx = type("Ctx", (), {"name": "chat_bot_cli_x"})()
        api = AsyncMock()
        api.send_card_to_user = AsyncMock(return_value="mid")
        asyncio.run(ic._run_pr_pending_merge_notify(
            ctx, api, 1, "", "ou_1", dict(_PR_INFO), "两轴审查通过，可以合入",
            REPO, 34, {}))
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("sent_ok") == 1
        api.send_card_to_user.assert_awaited_once()
        kw = api.send_card_to_user.await_args.kwargs
        assert kw["title"] == "📮 有 PR 待合入"
        assert kw["template"] == "green"
        flat = json.dumps(kw["fields"], ensure_ascii=False)
        assert "pulls/12" in flat, "卡片须含 PR 链接"
        assert "feat/issue-34" in flat and "weekly-1102-zrr" in flat, "卡片须含分支"
        assert "+120" in flat and "-30" in flat, "卡片须含增删行数"
        assert "两轴审查通过，可以合入" in flat, "卡片须含 QA 结论摘要"
        actions_flat = json.dumps(kw["actions"], ensure_ascii=False)
        assert "pulls/12" in actions_flat, "快捷按钮须含查看 PR"

    def test_no_ctx_writes_conv_template_and_skips(self):
        """未配置 chat bot → 任务会话仍写入「📮 PR #n 待合入」模板消息 + skipped_no_ctx 可观测。"""
        _clear_state()
        conv_id = create_conversation(_uid["id"], REPO, title="Issue #34 · x",
                                      conv_type="task", issue_number=34)
        asyncio.run(ic._run_pr_pending_merge_notify(
            None, None, _uid["id"], conv_id, "", dict(_PR_INFO), "QA 通过",
            REPO, 34, {}))
        msgs = get_messages(conv_id, limit=30)
        _tpl = [m for m in msgs if "📮 PR #12 待合入" in (m.get("content") or "")]
        assert _tpl, "会话兜底模板消息必须写入"
        _body = _tpl[0]["content"]
        assert "feat/issue-34" in _body and "pulls/12" in _body
        assert "QA 通过" in _body
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1
        assert counts.get("skipped_no_ctx") == 1
        assert counts.get("sent_ok", 0) == 0

    def test_send_failed_counter(self):
        """飞书发送失败 → sent_failed 计数（不抛出）。"""
        _clear_state()
        ctx = type("Ctx", (), {"name": "chat_bot_cli_x"})()
        api = AsyncMock()
        api.send_card_to_user = AsyncMock(side_effect=RuntimeError("feishu down"))
        asyncio.run(ic._run_pr_pending_merge_notify(
            ctx, api, 1, "", "ou_1", dict(_PR_INFO), "QA 通过", REPO, 34, {}))
        counts = ic.get_im_notify_counters()
        assert counts.get("sent_failed") == 1


# ===== 切片 C：review-pass 端点接线 =====

def _session_headers(agent_type="qa"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"prm-{uuid.uuid4().hex}", repo_key=REPO, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def _seed_issue_card(issue_number=38, status="pending_review", stage="sre"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "pr_merge_eric", REPO, issue_number, "插件需求", status, stage))
        card_id = c.lastrowid
        db.commit()
        return card_id


def _fire_inline():
    """_fire_notify_coro 测试替身：独立线程 asyncio.run + join（端点事件循环内不冲突、同步可断言）。"""
    import threading

    def _run(coro):
        t = threading.Thread(target=asyncio.run, args=(coro,))
        t.start()
        t.join()
    return _run


class TestReviewPassWiring:

    def test_review_pass_done_with_pr_number_notifies(self):
        """target=done + pr_number 透传 → passed_done 且会话收到「📮 PR #n 待合入」模板（含 QA 结论）。"""
        _clear_state()
        cid = _seed_issue_card(issue_number=38)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli, \
             patch("src.domains.runtime.issues_center._fire_notify_coro", side_effect=_fire_inline()):
            _cli.return_value.get_pr.return_value = _pr(50, title="feat: #38 插件需求", state="open")
            _cli.return_value.get_pr_files.return_value = [{"additions": 8, "deletions": 2}]
            r = client.post(f"/api/issues/{cid}/review-pass",
                            headers=_session_headers(agent_type="qa"),
                            json={"target": "done", "evidence": "两轴审查通过，建议合入", "pr_number": 50})
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "passed_done"
        count, conv_id = _issue_conv_count(38)
        assert count == 1, "会话应写入待合入模板"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "📮 PR #" in (m.get("content") or "")), "")
        assert "📮 PR #50 待合入" in body
        assert "两轴审查通过，建议合入" in body, "QA 结论摘要须随通知透传"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1

    def test_review_pass_done_without_open_pr_no_template(self):
        """无 open PR → 不写模板（skipped_no_open_pr）。"""
        _clear_state()
        cid = _seed_issue_card(issue_number=39)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli, \
             patch("src.domains.runtime.issues_center._fire_notify_coro", side_effect=_fire_inline()):
            _cli.return_value.list_prs.return_value = []
            r = client.post(f"/api/issues/{cid}/review-pass",
                            headers=_session_headers(agent_type="qa"),
                            json={"target": "done", "evidence": "通过"})
        assert r.status_code == 200 and r.json()["result"] == "passed_done"
        count, _ = _issue_conv_count(39)
        assert count == 0
        assert ic.get_im_notify_counters().get("skipped_no_open_pr") == 1

    def test_review_pass_processing_no_pr_notify(self):
        """target=processing（流水线继续）→ 不触发待合入通知。"""
        _clear_state()
        cid = _seed_issue_card(issue_number=40, status="pending_review", stage="qa")
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli, \
             patch("src.domains.runtime.issues_center._fire_notify_coro", side_effect=_fire_inline()) as _fire:
            _cli.return_value.list_prs.return_value = []
            r = client.post(f"/api/issues/{cid}/review-pass",
                            headers=_session_headers(agent_type="qa"),
                            json={"target": "processing"})
        assert r.status_code == 200 and r.json()["result"] == "passed_processing"
        count, _ = _issue_conv_count(40)
        assert count == 0


# ===== 切片 D：自动结单挂钩 =====

_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}


def _seed_finalize_issue(issue_number=41, status="processing"):
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND issue_number=?", (REPO, issue_number))
        c.execute("DELETE FROM events WHERE repo_key=? AND issue_id=?", (REPO, f"#{issue_number}"))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, completed_at, completed_reason) "
            "VALUES (?,?,?,?,?,?,NULL,NULL)",
            (_uid["id"], "pr_merge_eric", REPO, issue_number, "插件需求", status))
        db.commit()
    set_issue_stage(REPO, f"#{issue_number}", "sre", "sre")
    with db_lock:
        c = db.cursor()
        for _ag, _st in _ALL_COMPLETED.items():
            c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) VALUES (?,?,?,?,?)",
                      (REPO, f"#{issue_number}", "a2a_event", _ag, _st))
        db.commit()


class TestAutoFinalizeHook:

    def test_auto_done_with_open_pr_notifies(self):
        """末节点自动结单 + 关联 open PR → 通知（qa_summary=完成 agent 结果摘要透传）。"""
        _clear_state()
        _seed_finalize_issue(issue_number=41)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.get_issue_state.return_value = "open"
            _cli.return_value.close_issue.return_value = {"ok": True}
            _cli.return_value.list_prs.return_value = [_pr(60, title="feat: #41 插件需求")]
            _cli.return_value.get_pr_files.return_value = [{"additions": 66, "deletions": 6}]
            _res = ic.auto_finalize_issue_on_last_node(
                REPO, "#41", "sre", source="test", qa_summary="QA 审查通过，可以合入")
            assert _res == "auto_done", _res
        count, conv_id = _issue_conv_count(41)
        assert count == 1, "自动结单路径应写入待合入模板"
        body = next((m.get("content") or "" for m in get_messages(conv_id, limit=50)
                     if "📮 PR #" in (m.get("content") or "")), "")
        assert "📮 PR #60 待合入" in body
        assert "QA 审查通过，可以合入" in body, "qa_summary 须随通知透传"
        counts = ic.get_im_notify_counters()
        assert counts.get("attempted") == 1

    def test_auto_done_no_open_pr_no_template(self):
        _clear_state()
        _seed_finalize_issue(issue_number=42)
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as _cli:
            _cli.return_value.get_issue_state.return_value = "open"
            _cli.return_value.close_issue.return_value = {"ok": True}
            _cli.return_value.list_prs.return_value = []
            _res = ic.auto_finalize_issue_on_last_node(REPO, "#42", "sre", source="test")
            assert _res == "auto_done", _res
        count, _ = _issue_conv_count(42)
        assert count == 0
        assert ic.get_im_notify_counters().get("skipped_no_open_pr") == 1

    def test_noop_path_does_not_scan_prs(self):
        """非末节点 noop → 不扫描 PR、不通知。"""
        _clear_state()
        _seed_finalize_issue(issue_number=43)
        with patch("src.api.pipeline_config.get_pipeline_config",
                   return_value={"nodes": [{"id": "issue", "next": "vod"}, {"id": "vod", "next": "coding"},
                                           {"id": "coding", "next": "qa"}, {"id": "qa", "next": "sre"},
                                           {"id": "sre", "next": "dn"}]}):
            _res = ic.auto_finalize_issue_on_last_node(REPO, "#43", "sre", source="test")
            assert _res == "noop:not_last_node", _res
        count, _ = _issue_conv_count(43)
        assert count == 0
        assert ic.get_im_notify_counters().get("attempted", 0) == 0
