"""Issue #3375: note_hook_pr 评论体夹带其他 PR 评审片段——批次 1（本仓 P0）回归。

覆盖：
1. note_hook_pr prompt 专属分支：PR 号 + 本次 diff 文件清单 + 显式作用域声明
2. PR 类事件不注入其他 Issue/PR 的「最新结论」（issue-less PR 跳过该段）
3. get_issue_summary 实体维度过滤（issue 实体行为不变、PR 评审不混入 issue 摘要）
4. get_agent_workdir PR 维度隔离（issue-less PR / 同 Issue 多 PR）
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.kernel.session_manager import db, db_lock

REPO = "gitcode:org/issue3375-scope"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
            c.execute("DELETE FROM issue_agent_session WHERE repo_key=?", (REPO,))
            db.commit()
    _wipe()
    yield
    _wipe()


def _seed_event(seq, issue_id, title, result, entity_type, event_type="issue_opened"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, title, "
            "status, result, created_at, entity_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (seq, REPO, issue_id, event_type, "qa", title, "completed", result,
             f"2026-11-19 0{seq % 10}:00:00", entity_type),
        )
        db.commit()


def _note_evt(pr=452, issue_id="", diff_files=None, truncated=False):
    evt = {
        "type": "note_hook_pr",
        "source": "gitcode",
        "repository": "org/issue3375-scope",
        "repo_key": REPO,
        "issue_id": issue_id,
        "payload": {
            "object_attributes": {
                "noteable_iid": pr,
                "note": "gate PASS",
                "title": "feat: sample",
                "target_branch": "weekly-mys",
                "source_branch": "feat/sample",
                "url": f"https://gitcode.com/org/issue3375-scope/merge_requests/{pr}",
            },
        },
        "_pr_number": str(pr),
    }
    if diff_files is not None:
        evt["_pr_diff_files"] = list(diff_files)
        evt["_pr_diff_truncated"] = bool(truncated)
    return evt


def _prompt(evt):
    from src.kernel.prompt_builder import build_devops_agent_prompt
    return build_devops_agent_prompt(evt, [], "", ["qa"], agent_type="qa")


class TestNoteHookPrPromptScope:
    def test_outputs_pr_number_diff_files_and_scope(self):
        p = _prompt(_note_evt(diff_files=["src/a.py", "src/gate/runner.py"]))
        assert "PR #452" in p
        assert "src/a.py" in p and "src/gate/runner.py" in p
        assert "## PR Change Scope" in p
        assert "Scope constraint" in p
        assert "other Issues or PRs" in p
        # 无关联 Issue 的 PR 事件不得注入仓库内其他 Issue 的结论段
        assert "Issue Pipeline:" not in p

    def test_scope_declared_even_without_diff_list(self):
        p = _prompt(_note_evt())
        assert "PR #452" in p
        assert "unavailable" in p
        assert "## PR Change Scope" in p

    def test_pr_number_falls_back_to_payload(self):
        evt = _note_evt()
        evt.pop("_pr_number")
        assert "PR #452" in _prompt(evt)

    def test_no_cross_issue_summary_for_issue_less_pr(self):
        _seed_event(1, "#25", "other issue 25", "SECRET-REVIEW-FROM-25", "issue")
        _seed_event(2, "#31", "other issue 31", "SECRET-REVIEW-FROM-31", "issue")
        p = _prompt(_note_evt())
        assert "SECRET-REVIEW-FROM-25" not in p
        assert "SECRET-REVIEW-FROM-31" not in p


class TestGetIssueSummaryEntityFilter:
    def test_issue_entity_excludes_pr_review(self):
        from src.domains.runtime.event_store import get_issue_summary
        _seed_event(1, "#9", "pr 9 gate", "SECRET-PR-REVIEW", "pull_request",
                    event_type="note_hook_pr")
        _seed_event(2, "#10", "issue 10", "issue ten result", "issue")
        # 默认（无 entity_type）保持既有行为：PR 事件仍参与聚类
        assert "SECRET-PR-REVIEW" in get_issue_summary(REPO)
        # issue 实体过滤：PR 评审结论不得混入 issue 摘要
        s = get_issue_summary(REPO, entity_type="issue")
        assert "SECRET-PR-REVIEW" not in s
        assert "issue ten result" in s

    def test_exclude_issue_still_works(self):
        from src.domains.runtime.event_store import get_issue_summary
        _seed_event(1, "#1", "issue one", "r1", "issue")
        _seed_event(2, "#2", "issue two", "r2", "issue")
        s = get_issue_summary(REPO, exclude_issue="2", entity_type="issue")
        assert "issue one" in s and "issue two" not in s

    def test_linked_issue_pr_excludes_current_issue(self):
        _seed_event(1, "#9", "current issue", "CURRENT-ISSUE-9", "issue")
        _seed_event(2, "#10", "other issue", "other issue ten", "issue")
        p = _prompt(_note_evt(pr=452, issue_id="#9"))
        assert "CURRENT-ISSUE-9" not in p
        assert "other issue ten" in p


class TestPrWorkdirIsolation:
    def test_issue_only_unchanged(self):
        from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
        ws = get_agent_workdir(REPO, "qa", "#5")
        assert ws.endswith(os.path.join("org", "issue3375-scope", "issues", "5", "qa")), ws

    def test_issue_less_pr_isolated(self):
        from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
        ws = get_agent_workdir(REPO, "qa", "", "452")
        assert ws.endswith(os.path.join("org", "issue3375-scope", "prs", "452", "qa")), ws
        ws2 = get_agent_workdir(REPO, "qa", "", "453")
        assert ws != ws2

    def test_same_issue_multi_pr_isolated(self):
        from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
        a = get_agent_workdir(REPO, "coding", "#5", "12")
        b = get_agent_workdir(REPO, "coding", "#5", "13")
        assert a.endswith(os.path.join("issues", "5", "pr-12", "coding")), a
        assert b.endswith(os.path.join("issues", "5", "pr-13", "coding")), b
        assert a != b

    def test_no_issue_no_pr_fallback(self):
        from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
        ws = get_agent_workdir(REPO, "issue", "#0", "")
        assert ws.endswith("issue-agent"), ws

class TestPrefetchPrScope:
    """Issue #3375：事件构建期预取 PR 作用域（_prefetch_pr_scope）。"""

    def test_non_note_event_is_noop(self):
        from src.domains.runtime.pipeline import event_pipeline as ep
        evt = {"type": "issue_opened", "payload": {}}
        ep._prefetch_pr_scope(evt, REPO)
        assert "_pr_number" not in evt
        assert "_pr_diff_files" not in evt

    def test_reuses_payload_diff_files(self):
        from src.domains.runtime.pipeline import event_pipeline as ep
        evt = _note_evt(pr=452)
        evt["payload"]["added"] = [{"path": "src/a.py"}]
        evt["payload"]["modified"] = [{"path": "src/b.py"}]
        ep._prefetch_pr_scope(evt, REPO)
        assert evt["_pr_number"] == "452"
        assert evt["_pr_diff_files"] == ["src/a.py", "src/b.py"]
        assert evt["_pr_diff_truncated"] is False

    def test_fetches_diff_files_via_api_when_payload_empty(self, monkeypatch):
        from src.domains.runtime.pipeline import event_pipeline as ep
        import src.domains.runtime.vcs.files_enrich as fe
        evt = _note_evt(pr=452)
        monkeypatch.setattr(ep, "_get_repo_api_token", lambda repo_key: "")
        monkeypatch.setattr(fe, "fetch_files_via_api",
                            lambda *a, **k: {"added": [{"path": "x.py"}]})
        ep._prefetch_pr_scope(evt, REPO)
        assert evt["_pr_diff_files"] == ["x.py"]

    def test_fail_open_when_api_raises(self, monkeypatch):
        from src.domains.runtime.pipeline import event_pipeline as ep
        import src.domains.runtime.vcs.files_enrich as fe
        evt = _note_evt(pr=452)

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(ep, "_get_repo_api_token", lambda repo_key: "")
        monkeypatch.setattr(fe, "fetch_files_via_api", _boom)
        ep._prefetch_pr_scope(evt, REPO)
        assert evt["_pr_diff_files"] == []


class TestSessionPrIsolation:
    """Issue #3375：同 Issue 多 PR 不共享 oc_session_id。"""

    def test_same_issue_multi_pr_distinct(self):
        from src.api.session_store import get_or_create_issue_session
        a = get_or_create_issue_session(REPO, "coding", "#5", "12")
        b = get_or_create_issue_session(REPO, "coding", "#5", "13")
        assert a["id"] != b["id"]

    def test_same_issue_same_pr_reuses(self):
        from src.api.session_store import get_or_create_issue_session
        a = get_or_create_issue_session(REPO, "coding", "#5", "12")
        b = get_or_create_issue_session(REPO, "coding", "#5", "12")
        assert a["id"] == b["id"]

    def test_issue_level_unchanged_and_isolated_from_pr(self):
        from src.api.session_store import get_or_create_issue_session, get_issue_session
        plain = get_or_create_issue_session(REPO, "coding", "#5")
        pr = get_or_create_issue_session(REPO, "coding", "#5", "12")
        assert plain["id"] != pr["id"]
        assert get_issue_session(REPO, "coding", "#5")["id"] == plain["id"]
        assert get_issue_session(REPO, "coding", "#5", "12")["id"] == pr["id"]

    def test_zero_pr_falls_back_to_issue_key(self):
        from src.api.session_store import get_or_create_issue_session
        a = get_or_create_issue_session(REPO, "coding", "#5")
        b = get_or_create_issue_session(REPO, "coding", "#5", "0")
        assert a["id"] == b["id"]


class TestCwdDerivation:
    """Issue #3375：runtime / takeover 的 cwd 推导带 PR 维度。"""

    def test_runtime_cwd_or_derived_with_pr(self):
        from src.domains.runtime.routes.runtime import _cwd_or_derived
        p = _cwd_or_derived("", REPO, "qa", "#5", "t1", "12")
        assert p.endswith(os.path.join("issues", "5", "pr-12", "qa")), p
        p2 = _cwd_or_derived("", REPO, "qa", "", "t1", "452")
        assert p2.endswith(os.path.join("prs", "452", "qa")), p2

    def test_takeover_derives_cwd_from_json_env_extra(self):
        from src.domains.runtime.engine.takeover import _derive_task_cwd
        p = _derive_task_cwd(REPO, "qa", {"issue_id": "#5",
                                          "env_extra": json.dumps({"PR_NUMBER": "12"})})
        assert p.endswith(os.path.join("issues", "5", "pr-12", "qa")), p


class TestHistoricalEntityBackfill:
    """Issue #3375：历史行 entity_type 为空时按 event_type 反推实体，防 PR 评审混入。"""

    def test_historical_pr_row_excluded_from_issue_summary(self):
        from src.domains.runtime.event_store import get_issue_summary
        _seed_event(1, "#77", "hist pr", "HIST-PR-REVIEW", "",
                    event_type="note_hook_pr")
        _seed_event(2, "#78", "issue 78", "issue-78-result", "",
                    event_type="issue_opened")
        s = get_issue_summary(REPO, entity_type="issue")
        assert "HIST-PR-REVIEW" not in s
        assert "issue-78-result" in s
        s_pr = get_issue_summary(REPO, entity_type="pull_request")
        assert "HIST-PR-REVIEW" in s_pr


class TestNumericValue:
    def test_excludes_zero(self):
        from src.domains.runtime.pipeline.event_pipeline import _numeric_value
        assert _numeric_value("0") == ""
        assert _numeric_value("#0") == ""
        assert _numeric_value("#12") == "12"
        assert _numeric_value("abc") == ""
