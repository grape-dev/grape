# -*- coding: utf-8 -*-
"""Issue #1573：PR 合并视同审核通过，自动退出「待审核」状态 — 测试。

覆盖（C1）：
- stage=qa + status=pending_review → PR merged(sre) 后 status=processing、stage=sre，
  且 `_resume_issue_waiting` 被调用一次；
- 幂等：同事件重复投递，第二次不重复恢复，status/stage 不变；
- 回归：awaiting_input 不触发恢复，stage 仍推进 sre；
- 回归：stage 非 qa 时既不恢复也不推进；
- 降级：恢复抛异常不阻断 stage 推进，仅 warning。

C2 回归：
- `auto_finalize_issue_on_last_node` 在 status=pending_review 时仍返回 noop:pending_review；
- status=processing + 全节点 completed → auto_done。
"""
import json
from unittest.mock import patch

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/i1573-repo"
_NUM = 1573
_SCENE = "scene-1573"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped",
                  "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE ?", (f"{_REPO}%",))
        db.commit()


def _seed_repo():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'i1573-repo', 'XOcto/i1573-repo', 0, ?, '', 'wt-1573', 'grape_bot')",
            (_REPO, _SCENE))
        nodes = [{"tag": "sre", "id": "sre", "skill": "sre-cli", "enabled": True,
                  "engine_runtime_ids": [77]}]
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,0,?)",
            (_SCENE, _SCENE, _REPO, json.dumps(nodes),
             json.dumps({"work_branch": "weekly-mys"})))
        db.commit()


def _seed_issue(status, stage):
    """直接落库构造「真实形态」的 (status, stage)。

    修复（2026-11-09）：旧实现先插 status 再调 set_issue_stage 写入 stage，而 #1599 之后
    set_issue_stage 在阶段变化时会同步 status（pending_review→processing），导致种子态与
    断言前提不符（6 条用例全红）。真实系统里审核/等答态由 _set_issue_pending_review /
    awaiting_input 直接写 status，stage 早已就位，故此处按真实形态一次写入。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, status, stage, owner_agent) "
            "VALUES (0, '', ?, ?, ?, ?, ?)",
            (_REPO, _NUM, status, stage, stage))
        db.commit()


def _issue_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
                  (_REPO, _NUM))
        return c.fetchone()


def _merged_payload():
    return json.dumps({
        "object_attributes": {
            "iid": 9001,
            "number": 9001,
            "title": f"fix: #{_NUM} pr merged",
            "source_branch": f"feat/issue-{_NUM}",
            "target_branch": "weekly-mys",
            "action": "merge",
            "state": "merged",
        },
        "project": {"path_with_namespace": "XOcto/i1573-repo"},
        "user": {"username": "human_user"},
    }).encode()


def _run_pipeline(monkeypatch):
    import src.api.event_pipeline as ep
    import src.runtime.engine_select as es
    import src.api.pr_merged_sre_retry as retry_mod
    monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
    monkeypatch.setattr(es, "get_scene_pool_engine_ids", lambda *a, **kw: None)
    monkeypatch.setattr(retry_mod, "schedule_pr_merged_sre_retry", lambda *a, **kw: True)
    with patch("src.user.db.get_agent_bot_token", return_value=""), \
         patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
         patch("src.user.db.get_repo_token_with_refresh", return_value="tok-1573"):
        ep.run_pipeline(_merged_payload(), {"X-GitCode-Event": "Merge Request Hook"},
                        account="XOcto/i1573-repo")


class TestPRMergedReviewResume:
    def setup_method(self):
        _clean()
        _seed_repo()

    def teardown_method(self):
        _clean()

    def test_pending_review_resumed_then_stage_sre(self, monkeypatch):
        """C1-1：pending_review + qa → 恢复 processing，stage 推进 sre，resume 调 1 次。"""
        _seed_issue("pending_review", "qa")
        import src.api.issues_center as ic
        original = ic._resume_issue_waiting
        calls = []

        def _spy(repo_key, issue_number):
            calls.append((repo_key, issue_number))
            return original(repo_key, issue_number)

        monkeypatch.setattr(ic, "_resume_issue_waiting", _spy)
        _run_pipeline(monkeypatch)
        assert _issue_row() == ("processing", "sre"), _issue_row()
        assert calls == [(_REPO, _NUM)], calls

    def test_idempotent_second_delivery_no_change(self, monkeypatch):
        """C1-2：重复投递第二次不再恢复，status/stage 不变。"""
        _seed_issue("pending_review", "qa")
        import src.api.issues_center as ic
        original = ic._resume_issue_waiting
        calls = []

        def _spy(repo_key, issue_number):
            calls.append((repo_key, issue_number))
            return original(repo_key, issue_number)

        monkeypatch.setattr(ic, "_resume_issue_waiting", _spy)
        _run_pipeline(monkeypatch)
        _run_pipeline(monkeypatch)
        assert _issue_row() == ("processing", "sre"), _issue_row()
        assert len(calls) == 1, calls

    def test_awaiting_input_not_resumed_stage_advances(self, monkeypatch):
        """C1-3：awaiting_input 不恢复，stage 仍推进 sre。"""
        _seed_issue("awaiting_input", "qa")
        import src.api.issues_center as ic
        calls = []
        monkeypatch.setattr(ic, "_resume_issue_waiting",
                            lambda rk, n: calls.append((rk, n)) or True)
        _run_pipeline(monkeypatch)
        assert _issue_row() == ("awaiting_input", "sre"), _issue_row()
        assert calls == [], calls

    def test_non_qa_stage_no_resume_no_advance(self, monkeypatch):
        """C1-4：stage 非 qa → 既不恢复也不推进。"""
        _seed_issue("pending_review", "coding")
        import src.api.issues_center as ic
        calls = []
        monkeypatch.setattr(ic, "_resume_issue_waiting",
                            lambda rk, n: calls.append((rk, n)) or True)
        _run_pipeline(monkeypatch)
        assert _issue_row() == ("pending_review", "coding"), _issue_row()
        assert calls == [], calls

    def test_resume_exception_does_not_block_stage(self, monkeypatch):
        """C1-5：恢复抛异常 → 仅 warning，stage 仍推进 sre。"""
        _seed_issue("pending_review", "qa")
        import src.api.issues_center as ic

        def _boom(rk, n):
            raise RuntimeError("boom-1573")

        monkeypatch.setattr(ic, "_resume_issue_waiting", _boom)
        _run_pipeline(monkeypatch)
        status, stage = _issue_row()
        assert stage == "sre", (status, stage)
        # 修复（2026-11-09）：resume 抛异常时阶段仍推进，stage 变化的 status 同步照常生效，
        # 否则会留下 status=pending_review / stage=sre 的幽灵审核点（#1599 收敛目标）。
        assert status == "processing", (status, stage)


class TestAutoFinalizeRegression:
    def setup_method(self):
        _clean()
        _seed_repo()

    def teardown_method(self):
        _clean()

    def _seed_events(self, statuses):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
            for _ag, _st in statuses.items():
                c.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status) VALUES (?,?,?,?,?)",
                    (_REPO, f"#{_NUM}", "a2a_event", _ag, _st))
            db.commit()

    _ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                      "qa": "completed", "sre": "completed"}

    def test_pending_review_still_noop(self):
        """C2：pending_review 仍返回 noop:pending_review。"""
        from src.api.issues_center import auto_finalize_issue_on_last_node
        _seed_issue("pending_review", "sre")
        self._seed_events(self._ALL_COMPLETED)
        with patch("src.vcs.api_client.VcsApiClient") as _cli:
            _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
            assert _res == "noop:pending_review", _res
            _cli.return_value.close_issue.assert_not_called()
        assert _issue_row()[0] == "pending_review"

    def test_processing_all_completed_auto_done(self):
        """C2：processing + 全节点 completed → auto_done。"""
        from src.api.issues_center import auto_finalize_issue_on_last_node
        _seed_issue("processing", "sre")
        self._seed_events(self._ALL_COMPLETED)
        with patch("src.vcs.api_client.VcsApiClient") as _cli:
            _inst = _cli.return_value
            _inst.get_issue_state.return_value = "open"
            _inst.close_issue.return_value = {"ok": True}
            _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
            assert _res == "auto_done", _res
        assert _issue_row() == ("completed", "done"), _issue_row()