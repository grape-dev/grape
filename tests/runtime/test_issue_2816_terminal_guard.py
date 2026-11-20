"""Issue #2816：看门狗重复唤醒已终态载体 folded 单——终态短路测试。

覆盖：
- 纯判定：needs-human 标签 / needs-human-terminal 标记行 / intersect / 放行；
- 派发短路：coding 派发被跳过，event_skipped 留痕，issue 置人工态；
- 正常单据：无终态信号时派发行为不变（不短路）；
- 人工处置动作校验。
"""
from src.kernel.session_manager import db, db_lock

from tests.runtime._a2a_guard_support import A2AGuardTestBase, REPO_KEY


class TestTerminalStateEvaluation:

    def test_label_hit_marks_terminal(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(["bug", "needs-human"])
        assert state["terminal"] is True
        assert "issue_label_needs_human" in state["triggers"]
        assert state["manual_required"] is True

    def test_carrier_marker_hit_marks_terminal(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["bug"], carrier_comments={917: [
                {"body": "[PR Sweeper] needs-human-terminal "
                         "CONFLICT_SCOPE=intersect qa-r3-h1-s1-fail"}]})
        assert state["terminal"] is True
        assert state["intersect"] is True
        assert state["carrier_pr"] == 917
        assert "carrier_pr_needs_human_terminal" in state["triggers"]

    def test_folded_issue_text_identifies_carrier(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            [], issue_texts=["#3425 净改动已于 2026-11-14 追加进载体 PR #917"])
        assert state["folded"] is True
        assert state["carrier_prs"] == [917]
        assert state["terminal"] is False  # 仅 folded 不足以短路

    def test_clean_issue_passes(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(["bug", "P1"])
        assert state["terminal"] is False
        assert state["manual_required"] is False


class TestTerminalDispositionValidation:

    def test_actions_validation(self):
        from src.api.terminal_disposition import validate_disposition
        assert validate_disposition("new_carrier", 0) != ""
        assert validate_disposition("new_carrier", 917) == ""
        assert validate_disposition("reassign", target_agent="qa") == ""
        assert validate_disposition("reassign", target_agent="hacker") != ""
        assert validate_disposition("close") == ""
        assert validate_disposition("nope") != ""


class TestTerminalDispatchGuard2816(A2AGuardTestBase):

    def _body(self, agent="coding"):
        return {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": "5",
                "pr_number": "",
                "context": "Issue #2816 terminal guard test",
            },
        }

    def _seed_issue_row(self, status="processing"):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES ('owner', 'grape_bot', ?, 5, 'terminal guard', ?)",
                (REPO_KEY, status))
            db.commit()

    def _issue_status(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, completed_reason FROM issue "
                      "WHERE repo_key=? AND issue_number=5", (REPO_KEY,))
            return c.fetchone()

    def _seed_skip_cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def test_coding_dispatch_skipped_and_issue_marked_manual(self, monkeypatch):
        client = self._setup()
        self._seed_issue_row()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True,
            "labels": ["needs-human", "bug"],
            "issue_texts": ["载体 PR #917"],
            "carrier_comments": {917: [
                {"body": "needs-human-terminal CONFLICT_SCOPE=intersect "
                         "qa-r3-h1-s1-fail+a2a-rounds-exhausted"}]},
            "explicit_pr": 0,
        })
        guard.clear_cache()

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"].startswith("needs_human_terminal"), data
        assert data["carrier_pr"] == 917
        assert data["conflict_scope"] == "intersect"
        assert data["manual_status"] == "awaiting_input"
        assert {a["action"] for a in data["manual_actions"]} == {
            "new_carrier", "reassign", "close"}

        row = self._issue_status()
        assert row and row[0] == "awaiting_input", row
        assert row[1].startswith("needs_human_terminal")

        with db_lock:
            c = db.cursor()
            c.execute("SELECT reason FROM event_skipped WHERE repo_key=? "
                      "ORDER BY rowid DESC LIMIT 1", (REPO_KEY,))
            _skip = c.fetchone()
        assert _skip and _skip[0] == "needs_human_terminal", _skip
        self._seed_skip_cleanup()

    def test_clean_issue_dispatch_not_blocked(self, monkeypatch):
        client = self._setup()
        self._seed_issue_row()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True, "labels": ["bug", "P1"],
            "issue_texts": [], "carrier_comments": {}, "effective_pr": 0,
            "manual_override": {},
        })
        guard.clear_cache()

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") != "needs_human_terminal", data
        row = self._issue_status()
        assert row and row[0] == "processing", row
        self._seed_skip_cleanup()

    def _terminal_fetch(self, *a, **k):
        return {
            "available": True,
            "labels": ["needs-human"],
            "issue_texts": ["载体 PR #917"],
            "carrier_comments": {917: [{"body": "needs-human-terminal"}]},
            "effective_pr": 917,
            "manual_override": {},
        }

    def test_force_bypasses_terminal_guard(self, monkeypatch):
        client = self._setup()
        self._seed_issue_row()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()
        body = self._body(agent="coding")
        body["force"] = True

        resp = client.post("/api/a2a", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") != "needs_human_terminal", data
        row = self._issue_status()
        assert row and row[0] == "processing", row  # 未被置人工态
        self._seed_skip_cleanup()

    def test_non_coding_agent_not_short_circuited(self, monkeypatch):
        client = self._setup()
        self._seed_issue_row()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()

        resp = client.post("/api/a2a", json=self._body(agent="qa"))
        assert resp.status_code == 200, resp.text
        assert resp.json().get("skipped") != "needs_human_terminal", resp.text
        row = self._issue_status()
        assert row and row[0] == "processing", row
        self._seed_skip_cleanup()

    def test_env_switch_disables_guard(self, monkeypatch):
        client = self._setup()
        self._seed_issue_row()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setenv("A2A_TERMINAL_GUARD", "0")
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        assert resp.json().get("skipped") != "needs_human_terminal", resp.text
        row = self._issue_status()
        assert row and row[0] == "processing", row
        self._seed_skip_cleanup()

    def test_pending_review_issue_marked_manual(self, monkeypatch):
        """复审 Spec：pending_review 终态单也要落人工态（status+stage）。"""
        client = self._setup()
        self._seed_issue_row("pending_review")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET stage='pending_review' "
                      "WHERE repo_key=? AND issue_number=5", (REPO_KEY,))
            db.commit()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True,
            "labels": ["needs-human"],
            "issue_texts": ["载体 PR #917"],
            "carrier_comments": {917: [
                {"body": "needs-human-terminal CONFLICT_SCOPE=intersect"}]},
            "effective_pr": 917,
            "manual_override": {},
        })
        guard.clear_cache()

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["skipped"].startswith("needs_human_terminal"), resp.text

        with db_lock:
            c = db.cursor()
            _row = c.execute("SELECT status, stage FROM issue "
                             "WHERE repo_key=? AND issue_number=5",
                             (REPO_KEY,)).fetchone()
        assert _row == ("awaiting_input", "awaiting_input"), _row
        self._seed_skip_cleanup()

class TestCarrierPriority2816:
    """载体优先级：显式/meta 载体存在时彻底忽略文本派生旧载体。"""

    def _comments(self):
        return {
            500: [{"body": "普通评论，无终态标记"}],
            917: [{"body": "[PR Sweeper] needs-human-terminal CONFLICT_SCOPE=intersect"}],
        }

    def test_explicit_carrier_ignores_old_text_carrier(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["bug"], issue_texts=["净改动已追加进载体 PR #917"],
            carrier_comments=self._comments(), explicit_pr=500)
        assert state["carrier_prs"] == [500]
        assert state["terminal"] is False  # 新载体 500 无标记，旧载体 917 不参与
        assert state["intersect"] is False

    def test_explicit_carrier_exempts_stale_label_without_override(self):
        """复审 Spec：payload 显式载体存在时，旧 needs-human 标签不再拦截。"""
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["needs-human", "bug"], carrier_comments={500: []},
            explicit_pr=500, manual_override={})
        assert state["terminal"] is False, state
        assert state["label_exempted"] is True, state
        assert state["carrier_prs"] == [500], state

    def test_old_text_carrier_replay_does_not_exempt_label(self):
        """复审 Spec: payload 显式号正是文本旧载体时，仅 needs-human 标签仍短路。"""
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["needs-human"], issue_texts=["载体 PR #917"],
            carrier_comments={917: []}, explicit_pr=917, manual_override={})
        assert state["terminal"] is True, state
        assert state["label_exempted"] is False, state
        assert state["triggers"] == ["issue_label_needs_human"], state

    def test_new_explicit_carrier_exempts_label_over_old_text_carrier(self):
        """显式载体不同于文本旧载体时按新载体处理，标签豁免。"""
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["needs-human"], issue_texts=["载体 PR #917"],
            carrier_comments={500: []}, explicit_pr=500, manual_override={})
        assert state["terminal"] is False, state
        assert state["label_exempted"] is True, state
        assert state["carrier_prs"] == [500], state

    def test_override_carrier_matching_explicit_exempts_label(self):
        """显式载体与人工越过载体一致时豁免标签，即使它出现在旧文本载体里。"""
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["needs-human"], issue_texts=["载体 PR #917"],
            carrier_comments={917: []}, explicit_pr=917,
            manual_override={"action": "new_carrier", "carrier_pr": 917})
        assert state["terminal"] is False, state
        assert state["label_exempted"] is True, state

    def test_text_carrier_used_without_explicit(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["bug"], issue_texts=["净改动已追加进载体 PR #917"],
            carrier_comments=self._comments(), explicit_pr=0)
        assert state["carrier_prs"] == [917]
        assert state["terminal"] is True
        assert state["intersect"] is True

    def test_resolve_payload_old_carrier_with_label_only_still_terminal(self, monkeypatch):
        """派发入口回归: payload 带回旧载体号 + 纯 needs-human 标签仍被短路。"""
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True,
            "labels": ["needs-human"],
            "issue_texts": ["载体 PR #917"],
            "carrier_comments": {917: []},
            "effective_pr": 917,
            "manual_override": {},
        })
        guard.clear_cache()
        state = guard.resolve_terminal_state(REPO_KEY, 7, 917)
        guard.clear_cache()
        assert state["terminal"] is True, state
        assert state["label_exempted"] is False, state

    def test_resolve_uses_effective_meta_carrier(self, monkeypatch):
        """复审硬违规回归：meta 载体评论被读取后必须在判定层生效（假阴性修复）。"""
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True,
            "labels": ["bug"],
            "issue_texts": ["净改动已追加进载体 PR #917"],
            "carrier_comments": {
                500: [{"body": "needs-human-terminal CONFLICT_SCOPE=intersect"}],
            },
            "effective_pr": 500,
            "manual_override": {},
        })
        guard.clear_cache()
        state = guard.resolve_terminal_state(REPO_KEY, 6, 0)
        guard.clear_cache()
        assert state["terminal"] is True, state
        assert state["carrier_pr"] == 500, state
        assert state["intersect"] is True, state

    def test_resolve_override_ignores_stale_label_and_old_carrier(self, monkeypatch):
        """resolve+evaluate 联合：new_carrier 后旧载体终态 + 残留标签都不再短路。"""
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", lambda *a, **k: {
            "available": True,
            "labels": ["needs-human", "bug"],
            "issue_texts": ["净改动已追加进载体 PR #917"],
            "carrier_comments": {500: [{"body": "普通评论"}],
                                 917: [{"body": "needs-human-terminal"}]},
            "effective_pr": 500,
            "manual_override": {"action": "new_carrier", "carrier_pr": 500},
        })
        guard.clear_cache()
        state = guard.resolve_terminal_state(REPO_KEY, 6, 0)
        guard.clear_cache()
        assert state["terminal"] is False, state
        assert state["carrier_prs"] == [500], state
        assert state["label_exempted"] is True, state

    def test_fetch_vcs_state_prefers_override(self, monkeypatch):
        import src.api.a2a_terminal_guard as guard

        class _FakeClient:
            def __init__(self):
                self.pr_calls = []

            def get_issue(self, num, timeout=30):
                return {"labels": ["bug"], "title": "t", "body": "载体 PR #917"}

            def list_issue_comments(self, num, timeout=30, **kwargs):
                return [{"body": "载体 PR #917"}]

            def list_pr_comments(self, pr, timeout=30, **kwargs):
                self.pr_calls.append(pr)
                return [{"body": "needs-human-terminal"}]

        fake = _FakeClient()
        monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_token_with_refresh",
                            lambda rk: "tok")
        monkeypatch.setattr("src.domains.runtime.vcs.api_client.create_client",
                            lambda rk, tok: fake)
        monkeypatch.setattr("src.api.terminal_disposition.get_carrier_override",
                            lambda rk, num: 500)
        raw = guard._fetch_vcs_state(REPO_KEY, 6, explicit_pr=0)
        assert raw["available"] is True
        assert raw["effective_pr"] == 500  # 生效载体回传给判定层
        assert list(raw["carrier_comments"]) == [500]
        assert fake.pr_calls == [500]  # 旧载体 917 未被读取

    def test_fetch_vcs_state_keeps_label_when_comments_fail(self, monkeypatch):
        """Spec 复审：评论读取异常不得丢弃已命中的权威 needs-human 标签。"""
        import src.api.a2a_terminal_guard as guard

        class _FakeClient:
            def get_issue(self, num, timeout=30):
                return {"labels": ["needs-human"], "title": "t", "body": ""}

            def list_issue_comments(self, num, timeout=30, **kwargs):
                raise RuntimeError("comments endpoint down")

            def list_pr_comments(self, pr, timeout=30, **kwargs):
                raise RuntimeError("pr comments endpoint down")

        monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_token_with_refresh",
                            lambda rk: "tok")
        monkeypatch.setattr("src.domains.runtime.vcs.api_client.create_client",
                            lambda rk, tok: _FakeClient())
        monkeypatch.setattr("src.api.terminal_disposition.get_carrier_override",
                            lambda rk, num: 0)
        raw = guard._fetch_vcs_state(REPO_KEY, 6, explicit_pr=0)
        assert raw["available"] is True
        assert raw["labels"] == ["needs-human"]

    def test_resolve_caches_negative_result(self, monkeypatch):
        import src.api.a2a_terminal_guard as guard
        calls = []

        def _unavailable(*a, **k):
            calls.append(1)
            return {"available": False}

        monkeypatch.setattr(guard, "_fetch_vcs_state", _unavailable)
        guard.clear_cache()
        assert guard.resolve_terminal_state(REPO_KEY, 6, 0) == {}
        assert guard.resolve_terminal_state(REPO_KEY, 6, 0) == {}
        assert len(calls) == 1  # 负结果同样命中短 TTL 缓存
        guard.clear_cache()

    def test_list_pr_comments_paginates(self, monkeypatch):
        from src.domains.runtime.vcs.api_client import VcsApiClient
        client = VcsApiClient("github", "tok", "owner", "repo")
        calls = []
        pages = {1: [{"body": "a"}, {"body": "b"}], 2: [{"body": "c"}]}

        def _fake(method, path, data=None, form=False, timeout=30):
            calls.append(path)
            page = int(path.rsplit("page=", 1)[1])
            return pages.get(page, [])

        monkeypatch.setattr(client, "_request", _fake)
        out = client.list_pr_comments(7, per_page=2, max_pages=5)
        assert [c["body"] for c in out] == ["a", "b", "c"]
        assert len(calls) == 2
        assert all("/issues/7/comments" in p for p in calls)

    def _collect_paths(self, monkeypatch, client):
        calls = []

        def _fake(method, path, data=None, form=False, timeout=30):
            calls.append(path)
            return []

        monkeypatch.setattr(client, "_request", _fake)
        client.list_pr_comments(9)
        return calls

    def test_list_pr_comments_pulls_branch(self, monkeypatch):
        from src.domains.runtime.vcs.api_client import VcsApiClient
        client = VcsApiClient("gitcode", "tok", "owner", "repo")
        calls = self._collect_paths(monkeypatch, client)
        assert calls and "/pulls/9/comments" in calls[0], calls

    def test_list_pr_comments_gitlab_branch(self, monkeypatch):
        from src.domains.runtime.vcs.api_client import VcsApiClient
        client = VcsApiClient("gitlab", "tok", "owner", "repo")
        calls = self._collect_paths(monkeypatch, client)
        assert calls and "/merge_requests/9/notes" in calls[0], calls


class TestMarkIssueManual2816(A2AGuardTestBase):

    def _seed_issue_row(self, status="processing"):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES ('owner', 'grape_bot', ?, 6, 'manual status', ?)",
                (REPO_KEY, status))
            db.commit()

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def test_mark_issue_manual_returns_actual_status(self):
        self._seed_issue_row("processing")
        from src.api.a2a_terminal_guard import mark_issue_manual
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET stage='coding', owner_agent='coding' "
                      "WHERE repo_key=? AND issue_number=6", (REPO_KEY,))
            db.commit()
        assert mark_issue_manual(REPO_KEY, 6) == "awaiting_input"
        with db_lock:
            c = db.cursor()
            _row = c.execute("SELECT status, stage FROM issue "
                             "WHERE repo_key=? AND issue_number=6", (REPO_KEY,)).fetchone()
        assert _row[0] == "awaiting_input" and _row[1] == "awaiting_input", _row
        # 已处于暂停态：无状态变更，但返回实际状态，供响应如实呈现。
        assert mark_issue_manual(REPO_KEY, 6) == "awaiting_input"
        self._cleanup()


class TestManualOverride2816:
    """人工越过持久化：处置后旧 needs-human 标签不再反噬下一轮派发。"""

    def _cleanup(self):
        from src.api.terminal_disposition import clear_manual_override
        clear_manual_override(REPO_KEY, 8)

    def test_override_roundtrip(self):
        from src.api.terminal_disposition import (
            get_manual_override, set_manual_override)
        self._cleanup()
        set_manual_override(REPO_KEY, 8, "new_carrier", 500, "tester")
        data = get_manual_override(REPO_KEY, 8)
        assert data["action"] == "new_carrier"
        assert data["carrier_pr"] == 500
        assert data["by"] == "tester"
        self._cleanup()
        assert get_manual_override(REPO_KEY, 8) == {}

    def test_override_exempts_stale_label_only_for_same_carrier(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        override = {"action": "new_carrier", "carrier_pr": 500}
        # 同一新载体：旧 needs-human 标签被豁免。
        same = evaluate_terminal_state(
            ["needs-human"], carrier_comments={500: []},
            explicit_pr=500, manual_override=override)
        assert same["terminal"] is False and same["label_exempted"] is True, same
        # 显式指向其它载体：豁免不成立，标签仍按权威信号短路。
        other = evaluate_terminal_state(
            ["needs-human"], carrier_comments={901: []},
            explicit_pr=901, manual_override=override)
        assert other["terminal"] is True and other["label_exempted"] is False, other

    def test_override_does_not_mask_new_carrier_terminal(self):
        from src.api.a2a_terminal_guard import evaluate_terminal_state
        state = evaluate_terminal_state(
            ["needs-human"],
            carrier_comments={500: [{"body": "needs-human-terminal"}]},
            explicit_pr=500,
            manual_override={"action": "new_carrier", "carrier_pr": 500})
        assert state["terminal"] is True, state
        assert state["carrier_pr"] == 500, state


class _DispositionRouteFixture(A2AGuardTestBase):
    """处置路由用例的公共夹具（非 Test 前缀，避免被 pytest 直接收集）。"""

    def teardown_method(self):
        from src.api.deps import require_no_session_token
        from src.api.main import app
        app.dependency_overrides.pop(require_no_session_token, None)
        super().teardown_method()

    def _client_with_override(self):
        client = self._setup()
        from src.api.deps import require_no_session_token
        from src.api.main import app
        app.dependency_overrides[require_no_session_token] = lambda: {
            "id": "u-2816", "username": "tester", "auth_type": "jwt"}
        return client

    def _seed(self, status="awaiting_input", stage="done"):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=7", (REPO_KEY,))
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "status, stage) VALUES ('owner', 'grape_bot', ?, 7, 'terminal route', ?, ?)",
                (REPO_KEY, status, stage))
            iid = c.lastrowid
            db.commit()
        return iid

    def _row(self):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=7",
                (REPO_KEY,)).fetchone()

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def _patch_common(self, monkeypatch):
        monkeypatch.setattr("src.api.deps.check_repo_access", lambda user, repo_key: None)
        monkeypatch.setattr("src.api.terminal_disposition.record_disposition",
                            lambda *a, **k: None)
        monkeypatch.setattr("src.api.terminal_disposition.set_manual_override",
                            lambda *a, **k: None)


class TestTerminalDispositionRoute2816(_DispositionRouteFixture):
    """POST /api/issues/{id}/terminal-disposition 三个动作。"""

    def test_close_on_awaiting_input_converges(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        monkeypatch.setattr("src.domains.runtime.issues_center._notify_done",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "src.domains.runtime.issues_center._cleanup_auto_transferred_attachments",
            lambda *a, **k: None)
        iid = self._seed(status="awaiting_input", stage="done")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "close"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok" and data["closed"] is True, data
        row = self._row()
        assert row[0] == "completed" and row[1] == "done", row
        self._cleanup()

    def test_new_carrier_resumes_and_dispatches(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        overrides = {}
        monkeypatch.setattr("src.api.terminal_disposition.set_carrier_override",
                            lambda *a, **k: None)
        monkeypatch.setattr("src.api.terminal_disposition.set_manual_override",
                            lambda rk, num, action, carrier_pr=0, by="": overrides.update(
                                {"action": action, "carrier_pr": carrier_pr, "by": by}))
        dispatched = {}

        async def _fake_a2a(request, body, user):
            dispatched["agent"] = body.agent
            dispatched["carrier_pr"] = body.payload.get("carrier_pr")
            return {"status": "ok", "seq": 11}

        monkeypatch.setattr(
            "src.domains.runtime.routes.webhook.a2a_endpoint", _fake_a2a)
        iid = self._seed(status="awaiting_input")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "new_carrier", "carrier_pr": 500,
                                 "reason": "新载体可用"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["target_agent"] == "coding" and data["seq"] == 11, data
        assert dispatched == {"agent": "coding", "carrier_pr": 500}
        assert overrides == {"action": "new_carrier", "carrier_pr": 500, "by": "tester"}
        assert self._row()[0] == "processing"
        self._cleanup()

    def test_reassign_dispatches_target_agent(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        dispatched = {}

        async def _fake_a2a(request, body, user):
            dispatched["agent"] = body.agent
            return {"status": "ok", "seq": 12}

        monkeypatch.setattr(
            "src.domains.runtime.routes.webhook.a2a_endpoint", _fake_a2a)
        iid = self._seed(status="awaiting_input")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "reassign", "target_agent": "qa"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_agent"] == "qa"
        assert dispatched["agent"] == "qa"
        assert self._row()[0] == "processing"
        self._cleanup()

    def test_invalid_action_returns_400(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        iid = self._seed()
        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "nope"})
        assert resp.status_code == 400, resp.text
        self._cleanup()


class TestResumeFromManual2816(A2AGuardTestBase):
    """复审硬违规回归：已收敛单据不得被恢复/改写 stage。"""

    def _seed(self, status, stage="done"):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=9", (REPO_KEY,))
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "status, stage) VALUES ('owner', 'grape_bot', ?, 9, 'resume guard', ?, ?)",
                (REPO_KEY, status, stage))
            db.commit()

    def _row(self):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=9",
                (REPO_KEY,)).fetchone()

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def test_completed_status_not_resumed_and_stage_untouched(self):
        self._seed("completed", "done")
        from src.domains.runtime.issues_center import resume_issue_from_manual
        assert resume_issue_from_manual(REPO_KEY, 9, "coding", "coding") is False
        row = self._row()
        assert row == ("completed", "done"), row
        self._cleanup()

    def test_cancelled_status_not_resumed_and_stage_untouched(self):
        self._seed("cancelled", "done")
        from src.domains.runtime.issues_center import resume_issue_from_manual
        assert resume_issue_from_manual(REPO_KEY, 9, "qa", "qa") is False
        row = self._row()
        assert row == ("cancelled", "done"), row
        self._cleanup()

    def test_resumable_status_resumes_and_advances_stage(self):
        self._seed("awaiting_input", "awaiting_input")
        from src.domains.runtime.issues_center import resume_issue_from_manual
        assert resume_issue_from_manual(REPO_KEY, 9, "coding", "coding") is True
        row = self._row()
        assert row == ("processing", "coding"), row
        self._cleanup()

    def test_processing_status_is_idempotent_resumable(self):
        """processing 属于 RESUMABLE：重复 new_carrier/reassign 不应被拒。"""
        self._seed("processing", "coding")
        from src.domains.runtime.issues_center import resume_issue_from_manual
        assert resume_issue_from_manual(REPO_KEY, 9, "coding", "coding") is True
        row = self._row()
        assert row == ("processing", "coding"), row
        self._cleanup()

    def test_mysql_zero_rowcount_processing_still_resumed(self, monkeypatch):
        """复审硬违规：MySQL 未启用 CLIENT_FOUND_ROWS，同值 UPDATE rowcount=0。"""
        self._seed("processing", "coding")

        class _ZeroUpdateCursor:
            def __init__(self, cur):
                self._cur = cur

            def execute(self, sql, params=()):
                res = self._cur.execute(sql, params)
                if str(sql).strip().upper().startswith("UPDATE"):
                    self.rowcount = 0
                return res

            def __getattr__(self, name):
                return getattr(self._cur, name)

        class _ZeroUpdateDb:
            def __init__(self, real):
                self._real = real

            def cursor(self):
                return _ZeroUpdateCursor(self._real.cursor())

            def commit(self):
                self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        import src.domains.runtime.issues_center as issues_center
        monkeypatch.setattr(issues_center, "_rt_db", _ZeroUpdateDb(issues_center._rt_db))
        assert issues_center.resume_issue_from_manual(
            REPO_KEY, 9, "coding", "coding") is True
        row = self._row()
        assert row == ("processing", "coding"), row
        self._cleanup()


class TestTerminalDispositionStatusFloor2816(_DispositionRouteFixture):
    """复审硬违规回归：处置入口对已收敛单据返回 409 且不派发、不写 stage。"""

    def _no_dispatch(self, monkeypatch):
        dispatched = []

        async def _fake_a2a(request, body, user):
            dispatched.append(body.agent)
            return {"status": "ok", "seq": 99}

        monkeypatch.setattr(
            "src.domains.runtime.routes.webhook.a2a_endpoint", _fake_a2a)
        return dispatched

    def test_new_carrier_on_completed_returns_409(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        monkeypatch.setattr("src.api.terminal_disposition.set_carrier_override",
                            lambda *a, **k: None)
        dispatched = self._no_dispatch(monkeypatch)
        iid = self._seed(status="completed", stage="done")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "new_carrier", "carrier_pr": 500})
        assert resp.status_code == 409, resp.text
        assert self._row() == ("completed", "done")
        assert dispatched == []
        self._cleanup()

    def test_reassign_on_cancelled_returns_409(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        dispatched = self._no_dispatch(monkeypatch)
        iid = self._seed(status="cancelled", stage="done")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "reassign", "target_agent": "qa"})
        assert resp.status_code == 409, resp.text
        assert self._row() == ("cancelled", "done")
        assert dispatched == []
        self._cleanup()

    def test_close_pending_review_converges_and_clears_carrier(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        monkeypatch.setattr("src.domains.runtime.issues_center._notify_done",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "src.domains.runtime.issues_center._cleanup_auto_transferred_attachments",
            lambda *a, **k: None)
        from src.api.terminal_disposition import (
            get_carrier_override, set_carrier_override)
        set_carrier_override(REPO_KEY, 7, 500)
        assert get_carrier_override(REPO_KEY, 7) == 500
        iid = self._seed(status="pending_review", stage="pending_review")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "close"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["closed"] is True
        assert self._row() == ("completed", "done")
        assert get_carrier_override(REPO_KEY, 7) == 0
        self._cleanup()

    def test_close_on_completed_returns_blocked_and_keeps_state(self, monkeypatch):
        """复审 Spec：close 作用于已收敛单：无变更、不改 stage、不复活。"""
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        monkeypatch.setattr("src.domains.runtime.issues_center._notify_done",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "src.domains.runtime.issues_center._cleanup_auto_transferred_attachments",
            lambda *a, **k: None)
        iid = self._seed(status="completed", stage="done")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "close"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "blocked" and data["closed"] is False, data
        assert self._row() == ("completed", "done")
        self._cleanup()

    def test_close_on_cancelled_returns_blocked(self, monkeypatch):
        client = self._client_with_override()
        self._patch_common(monkeypatch)
        monkeypatch.setattr("src.domains.runtime.issues_center._notify_done",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "src.domains.runtime.issues_center._cleanup_auto_transferred_attachments",
            lambda *a, **k: None)
        iid = self._seed(status="cancelled", stage="done")

        resp = client.post(f"/api/issues/{iid}/terminal-disposition",
                           json={"action": "close"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["closed"] is False, resp.text
        assert self._row() == ("cancelled", "done")
        self._cleanup()


class TestDispatchEntryTerminalGuard2816(A2AGuardTestBase):
    """复审 Spec 回归：排队 drain 直投的 coding 事件在派发入口复查终态。"""

    def _seed_issue(self, status="processing", stage="coding"):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=5", (REPO_KEY,))
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "status, stage) VALUES ('owner', 'grape_bot', ?, 5, 'drain guard', ?, ?)",
                (REPO_KEY, status, stage))
            db.commit()

    def _issue_status(self):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT status FROM issue WHERE repo_key=? AND issue_number=5",
                (REPO_KEY,)).fetchone()[0]

    def _event_status(self, seq):
        with db_lock:
            c = db.cursor()
            return c.execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()[0]

    def _cleanup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
            c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
            c.execute("DELETE FROM event_skipped WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def _terminal_fetch(self, *a, **k):
        return {
            "available": True,
            "labels": ["needs-human"],
            "issue_texts": ["载体 PR #917"],
            "carrier_comments": {917: [{"body": "needs-human-terminal "
                                        "CONFLICT_SCOPE=intersect"}]},
            "effective_pr": 917,
            "manual_override": {},
        }

    def test_dispatch_entry_reevaluates_terminal_state(self, monkeypatch):
        self._setup()
        self._seed_issue()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()
        from src.api.a2a_dispatch import run_a2a_dispatch
        from src.api.event_store import append_event
        seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="A2A: coding",
            issue_id="#5", source="a2a", agent_type="coding",
            payload={"issue_number": "5"}, status="running")

        res = run_a2a_dispatch(
            REPO_KEY, seq, "coding", {"payload": {"issue_number": "5"}},
            issue_number="5", repository="owner/repo")

        assert res["status"] == "skipped", res
        assert res["skipped"].startswith("needs_human_terminal"), res
        assert res["carrier_pr"] == 917
        assert self._event_status(seq) == "cancelled"  # 不留下 running 僵尸
        assert self._issue_status() == "awaiting_input"
        assert any(r.startswith("needs_human_terminal")
                   for r, _ in self._skips_for()), self._skips_for()
        guard.clear_cache()
        self._cleanup()

    def test_dispatch_entry_force_bypasses_guard(self, monkeypatch):
        self._setup()
        self._seed_issue()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()
        from src.api.a2a_dispatch import run_a2a_dispatch
        res = run_a2a_dispatch(
            REPO_KEY, 0, "coding", {"payload": {"issue_number": "5"}},
            issue_number="5", repository="owner/repo", force=True)
        assert res.get("skipped") != "needs_human_terminal", res
        assert self._issue_status() == "processing"
        guard.clear_cache()
        self._cleanup()

    def test_precomputed_guard_state_avoids_vcs_reread(self, monkeypatch):
        """复审判断项：async 入口已判定的状态透传后不得再同步读 VCS。"""
        self._setup()
        self._seed_issue(status="awaiting_input", stage="awaiting_input")
        import src.api.a2a_terminal_guard as guard

        def _must_not_read(*a, **k):
            raise AssertionError("guard must not re-read VCS when precomputed")

        monkeypatch.setattr(guard, "guard_terminal_dispatch", _must_not_read)
        from src.api.a2a_dispatch import run_a2a_dispatch
        res = run_a2a_dispatch(
            REPO_KEY, 0, "coding", {"payload": {"pr_number": "500"}},
            issue_number="5", repository="owner/repo",
            guard_state={
                "reason": "needs_human_terminal",
                "detail": "precomputed", "triggers": ["issue_label_needs_human"],
                "carrier_pr": 500, "folded": True, "conflict_scope": "",
                "manual_status": "awaiting_input", "manual_applied": True,
            })
        assert res["status"] == "skipped", res
        assert res["handoff_result"] == "rejected", res
        self._cleanup()

    def test_queue_drain_marks_skipped_event_cancelled(self, monkeypatch):
        """复审 Spec：排队 drain 直投路径命中终态 → cancelled + 不算已派发。"""
        self._setup()
        self._seed_issue()
        import src.api.a2a_terminal_guard as guard
        monkeypatch.setattr(guard, "_fetch_vcs_state", self._terminal_fetch)
        guard.clear_cache()
        from src.domains.runtime.pipeline.event_pipeline import _claim_and_dispatch_queued
        from src.api.event_store import append_event
        seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="A2A: coding",
            issue_id="#5", source="a2a", agent_type="coding",
            payload={"issue_number": "5"}, status="queued")
        qinfo = {"dispatch": {"evt": {"payload": {"issue_number": "5"}},
                              "skill_name": "coding"}}
        ok = _claim_and_dispatch_queued(
            seq, REPO_KEY, "coding", "#5",
            {"issue_number": "5", "repository": "owner/repo"}, qinfo, 0)
        assert ok is True
        assert self._event_status(seq) == "cancelled"
        assert self._issue_status() == "awaiting_input"
        guard.clear_cache()
        self._cleanup()

    def test_queue_payload_carrier_hint_passed_to_dispatch(self, monkeypatch):
        """复审 Spec：队列行缺 dispatch evt.payload 时透传 pr_number 载体提示。"""
        self._setup()
        from src.domains.runtime.pipeline.event_pipeline import _claim_and_dispatch_queued
        from src.api.event_store import append_event
        captured = {}

        def _fake_dispatch(repo_key, seq, agent, evt, **kwargs):
            captured["evt"] = evt
            return {"status": "ok", "seq": seq}

        monkeypatch.setattr("src.api.a2a_dispatch.run_a2a_dispatch", _fake_dispatch)
        seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="A2A: coding",
            issue_id="#5", source="a2a", agent_type="coding",
            payload={"issue_number": "5"}, status="queued")
        qinfo = {"dispatch": {"evt": {"payload": {}}, "skill_name": "coding"}}
        ok = _claim_and_dispatch_queued(
            seq, REPO_KEY, "coding", "#5",
            {"issue_number": "5", "repository": "owner/repo", "pr_number": "500"},
            qinfo, 0)
        assert ok is True
        assert captured["evt"]["payload"]["pr_number"] == "500", captured
        self._cleanup()


class TestCrossPipelineHandoffRejected2816:
    def test_skipped_dispatch_returns_rejected(self, monkeypatch):
        """复审判断项：handoff 派发 skipped 不能被当作 ok。"""
        from src.domains.runtime.routes import webhook

        def _fake_dispatch(*a, **k):
            return {"status": "skipped", "seq": 5, "skipped": "needs_human_terminal"}

        monkeypatch.setattr(webhook, "run_a2a_dispatch", _fake_dispatch)
        res = webhook._dispatch_handoff_a2a(
            REPO_KEY, 5, "coding", {"payload": {"issue_number": "5"}},
            issue_number="5", repository="owner/repo")
        assert res["status"] == "rejected", res
        assert res["dispatch"]["skipped"] == "needs_human_terminal", res
