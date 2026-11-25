"""Issue #3375 复审补充：PR 维度清扫 / 会话键还原 / 真实 Note Hook 预取 / 常量一致性。

覆盖审查指出的测试缺口：
1. `cleanup._zombie_issue_dirs` 扫描 `prs/<p>/<agent>` 与 `issues/<n>/pr-<p>/<agent>`；
2. `cleanup._active_issue_sessions` 把 `::pr<n>` 会话键还原真实 Issue 号；
3. done-issue 清扫按 `issues/<n>/pr-<p>` 逐个传 pr_number（`_issue_pr_numbers`）；
4. 真实 GitCode Note Hook（仅 `noteable_iid`/`url`）下 diff 清单预取走 API；
5. `prompt_builder._PR_EVENT_TYPES` 与 `event_types.ALL_PR_EVENTS` 一致；
6. `agent._cleanup_task_workdir` 对缺 issue_session_id 的关联 Issue PR 任务按 PR 清理。
"""
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.kernel.session_manager import db, db_lock

REPO = "gitcode:org/issue3375-cleanup"


@pytest.fixture(autouse=True)
def _clean_db():
    def _wipe():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
            c.execute("DELETE FROM issue_agent_session WHERE repo_key=?", (REPO,))
            db.commit()
    _wipe()
    yield
    _wipe()


def _touch_dir(path, age_seconds=0):
    os.makedirs(path, exist_ok=True)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


class TestZombieScanNewLayouts:
    def test_scans_issue_less_and_multi_pr_layouts(self, tmp_path, monkeypatch):
        from src.kernel import config
        from src.domains.runtime.engine import cleanup
        ws = tmp_path / "ws"
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(ws))
        repo_dir = ws / "gitcode" / "org" / "issue3375-cleanup"
        old = 4 * 86400
        prs_dir = _touch_dir(str(repo_dir / "prs" / "452" / "qa"), old)
        pr_sub = _touch_dir(str(repo_dir / "issues" / "5" / "pr-12" / "coding"), old)
        fresh = _touch_dir(str(repo_dir / "prs" / "453" / "qa"), 0)

        found = set(cleanup._zombie_issue_dirs(3 * 86400))
        assert prs_dir in found
        assert pr_sub in found
        assert fresh not in found


class TestActiveIssueSessionsPrKey:
    def test_pr_suffix_restores_real_issue_number(self):
        from src.domains.runtime.engine import cleanup
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue_agent_session (id, repo_key, agent_type, issue_id, status) "
                "VALUES (?,?,?,?,?)",
                ("s-pr-12", REPO, "coding", "#5::pr12", "running"),
            )
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_session_id, "
                "repo_key, agent_type, prompt, status) VALUES (?,?,?,?,?,?,?,?)",
                ("t-1", "as-1", 1, "s-pr-12", REPO, "coding", "p", "dispatched"),
            )
            db.commit()
        active = cleanup._active_issue_sessions()
        assert (REPO, "coding", "5") in active


class TestDoneIssuePrSweep:
    def test_lists_pr_subdirs_for_done_issue(self, tmp_path, monkeypatch):
        from src.kernel import config
        from src.domains.runtime.engine import cleanup
        ws = tmp_path / "ws"
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(ws))
        repo_dir = ws / "gitcode" / "org" / "issue3375-cleanup"
        _touch_dir(str(repo_dir / "issues" / "5" / "pr-12" / "coding"))
        _touch_dir(str(repo_dir / "issues" / "5" / "pr-13" / "qa"))
        _touch_dir(str(repo_dir / "issues" / "5" / "coding"))
        assert sorted(cleanup._issue_pr_numbers(REPO, "5")) == ["12", "13"]

    def test_no_pr_subdirs_returns_empty(self, tmp_path, monkeypatch):
        from src.kernel import config
        from src.domains.runtime.engine import cleanup
        ws = tmp_path / "ws"
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(ws))
        repo_dir = ws / "gitcode" / "org" / "issue3375-cleanup"
        _touch_dir(str(repo_dir / "issues" / "5" / "coding"))
        assert cleanup._issue_pr_numbers(REPO, "5") == []


class TestPrEventTypesParity:
    def test_kernel_mirror_matches_domain_truth(self):
        from src.kernel.prompt_builder import _PR_EVENT_TYPES
        from src.domains.runtime.vcs.event_types import ALL_PR_EVENTS
        assert _PR_EVENT_TYPES == frozenset(ALL_PR_EVENTS)


class TestRealNoteHookPrefetch:
    def test_fetches_by_noteable_iid_without_pr_object(self, monkeypatch):
        from src.domains.runtime.pipeline import event_pipeline as ep
        import src.domains.runtime.vcs.files_enrich as fe
        import src.api.git_repo_api as api

        evt = {
            "type": "note_hook_pr",
            "source": "gitcode",
            "repo_key": REPO,
            "payload": {
                "object_attributes": {
                    "noteable_iid": 452,
                    "note": "gate PASS",
                    "url": "https://gitcode.com/org/repo/merge_requests/452",
                },
            },
            "_pr_number": "452",
        }
        calls = []

        def _fake_req(provider, method, url, token=None):
            calls.append(url)
            return [{"new_path": "src/a.py", "state": "added"}]

        monkeypatch.setattr(ep, "_get_repo_api_token", lambda repo_key: "")
        monkeypatch.setattr(fe, "get_platform_card", lambda p: object())
        monkeypatch.setattr(api, "_vcs_req", _fake_req)
        ep._prefetch_pr_scope(evt, REPO)
        assert evt["_pr_diff_files"] == ["src/a.py"]
        assert calls and "/pulls/452/files" in calls[0]


class TestCleanupTaskWorkdirLinkedPr:
    def test_pr_issue_without_issue_session_id_uses_issue_pr_path(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine import agent, repogit
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        calls = []
        monkeypatch.setattr(
            repogit, "remove_issue_worktree_if_pushed",
            lambda *a, **k: calls.append(("issue", a, k)) or True)
        monkeypatch.setattr(
            repogit, "cleanup_pr_worktree",
            lambda *a, **k: calls.append(("pr", a, k)) or True)
        monkeypatch.setattr(agent, "_get_task_token", lambda task: "")
        task = {
            "id": "t-1",
            "repo_key": REPO,
            "agent_type": "qa",
            "issue_id": "#5",
            "cwd": str(cwd),
            "issue_session_id": "",
        }
        agent._cleanup_task_workdir(task, {"PR_NUMBER": "12"})
        assert calls and calls[0][0] == "issue"
        assert calls[0][1][2] == "5"
        assert calls[0][2].get("pr_number") == "12"
        assert not any(c[0] == "pr" for c in calls)