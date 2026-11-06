"""Issue #993：分支级写租约 + push 前置守卫测试。

覆盖四轴中的三轴：
- AC1 租约争用：同一 repo+branch 两写入方仅一方获准；
- AC2 PR 已 merged/closed 后 push 阻断并提示新建分支/PR；
- AC3 远端 head 漂移阻断并提示重新 fetch/rebase。
"""
import io
import os
import subprocess

import pytest

from src.runtime import branch_guard as bg


REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-993"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import src.lock as lock
    from src.session_manager import db
    # 租约状态文件落到临时目录，避免污染工作区
    monkeypatch.setattr(bg, "_state_dir", lambda repo_key: str(tmp_path / "state"))
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.commit()
    except Exception:
        pass
    yield
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.commit()
    except Exception:
        pass


# ===================== AC1 租约争用 =====================

def test_lease_key_is_branch_scoped():
    assert bg.lease_key(REPO_KEY, "feat/a") != bg.lease_key(REPO_KEY, "feat/b")
    assert len(bg.lease_key(REPO_KEY, "x" * 200)) <= 128


def test_lease_contention_only_one_writer(monkeypatch):
    import src.lock as lock
    monkeypatch.setattr(lock, "_holder", "host-task-A")
    first = bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-old")
    assert first["ok"] is True
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["expected_remote_sha"] == "sha-old"

    monkeypatch.setattr(lock, "_holder", "host-task-B")
    second = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert second["ok"] is False
    assert second["reason"] == "branch_locked"
    assert "host-task-A" in second["message"]

    # 原持有者可重入；释放后另一方才可获取
    monkeypatch.setattr(lock, "_holder", "host-task-A")
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH)["ok"] is True
    bg.release_branch_lease(REPO_KEY, BRANCH)
    monkeypatch.setattr(lock, "_holder", "host-task-B")
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH)["ok"] is True


# ===================== AC2 merged/closed 阻断 =====================

def test_merged_pr_blocks_push_with_actionable_message():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="aaa", pr_state="merged",
                              pr_number=42, base_branch="weekly-mys")
    assert r["allowed"] is False
    assert r["reason"] == "pr_merged"
    assert "PR #42" in r["message"] and "已合并" in r["message"]
    assert "新建分支" in r["message"] and "weekly-mys" in r["message"]


def test_closed_pr_blocks_push():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, pr_state="closed", pr_number=7)
    assert r["allowed"] is False
    assert r["reason"] == "pr_closed"


def test_open_pr_allows_push():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="aaa", pr_state="open")
    assert r["allowed"] is True


# ===================== AC3 远端漂移阻断 =====================

def test_remote_drift_blocks_push():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="bbb",
                              expected_remote_sha="aaa")
    assert r["allowed"] is False
    assert r["reason"] == "remote_drift"
    assert "fetch/rebase" in r["message"]
    assert "二次解冲突" in r["message"]


def test_no_drift_allows_push():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="aaa",
                              expected_remote_sha="aaa")
    assert r["allowed"] is True


def test_first_push_without_expected_sha_allows():
    assert bg.load_lease_state(REPO_KEY, BRANCH) == {}
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="new-sha")
    assert r["allowed"] is True
    bg.record_remote_sha(REPO_KEY, BRANCH, "new-sha")
    r2 = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="moved-sha")
    assert r2["allowed"] is False and r2["reason"] == "remote_drift"


def test_pr_state_matching_by_head_branch():
    items = [
        {"number": 1, "state": "closed", "merged": False,
         "head": {"ref": "feat/other"}, "base": {"ref": "main"}},
        {"number": 2, "state": "closed", "merged_at": "2026-01-01",
         "head": {"ref": BRANCH}, "base": {"ref": "weekly-mys"}},
    ]
    got = bg._match_pr(items, BRANCH)
    assert got == {"state": "merged", "number": 2, "base": "weekly-mys"}


def test_pr_state_prefers_latest_match_across_pages():
    items = [
        {"number": 3, "state": "open", "head": {"ref": BRANCH}, "base": {"ref": "weekly-mys"}},
        {"number": 9, "state": "closed", "merged": True,
         "head": {"ref": BRANCH}, "base": {"ref": "weekly-mys"}},
    ]
    got = bg._match_pr(items, BRANCH)
    assert got["number"] == 9 and got["state"] == "merged"


def test_fetch_pr_state_uses_paginated_client(monkeypatch):
    from src.vcs.api_client import VcsApiClient
    captured = {}

    def _list_prs(self, state="all", head="", max_pages=20):
        captured["state"], captured["head"] = state, head
        return [{"number": 5, "state": "closed", "merged": True,
                 "head": {"ref": BRANCH}, "base": {"ref": "weekly-mys"}}]

    monkeypatch.setattr(VcsApiClient, "list_prs", _list_prs)
    got = bg.fetch_pr_state(REPO_KEY, BRANCH, token="tok")
    assert got["state"] == "merged" and got["number"] == 5
    assert captured == {"state": "all", "head": BRANCH}


def test_consecutive_push_records_pushed_local_sha():
    """AC3：push 成功后记录 local sha，第二次合法推送不误报漂移。"""
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-1")
    assert bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="local-1")["allowed"] is True
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-2")
    assert bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="local-2")["allowed"] is True


def test_hook_records_local_sha_not_stale_remote(monkeypatch):
    """钩子必须记录 stdin 的 local_sha（parts[1]），而非 push 前的旧 remote_sha。"""
    monkeypatch.setattr(bg, "acquire_branch_lease",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "open"})
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(f"refs/heads/{BRANCH} local-new refs/heads/{BRANCH} old-remote\n"))
    assert bg.hook_main([REPO_KEY, BRANCH]) == 0
    assert bg.load_lease_state(REPO_KEY, BRANCH)["expected_remote_sha"] == "local-new"


def test_hook_block_is_dual_channel(monkeypatch, capsys):
    """阻断必须同时写 git push stderr 与任务日志。"""
    monkeypatch.setattr(bg, "acquire_branch_lease",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 42,
                                         "base": "weekly-mys"})
    logged = []
    monkeypatch.setattr(bg.log, "warning", lambda m, *a, **k: logged.append(m))
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(f"refs/heads/{BRANCH} local refs/heads/{BRANCH} remote\n"))
    assert bg.hook_main([REPO_KEY, BRANCH]) == 1
    err = capsys.readouterr().err
    assert "[branch-guard]" in err and "PR #42" in err
    assert logged and "pr_merged" in logged[0]


def test_hook_cross_process_holder_rejects_self_lock(monkeypatch, capsys):
    """AC1/M1 自锁回归：父任务持租约时，钩子进程（不同 holder）必判 branch_locked。

    自动推送路径因此改用 ``--no-verify``（自身已持租约并完成等价 preflight），
    避免 push 触发的新进程钩子把本任务自己锁在门外。
    """
    import src.lock as lock
    monkeypatch.setattr(lock, "_holder", "host-task-A")
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH)["ok"] is True
    try:
        monkeypatch.setattr(lock, "_holder", "host-task-B")
        logged = []
        monkeypatch.setattr(bg.log, "warning", lambda m, *a, **k: logged.append(m))
        monkeypatch.setattr(bg.sys, "stdin",
                            io.StringIO(f"refs/heads/{BRANCH} local refs/heads/{BRANCH} remote\n"))
        assert bg.hook_main([REPO_KEY, BRANCH]) == 1
        err = capsys.readouterr().err
        assert "[branch-guard]" in err and "host-task-A" in err
        assert logged and "branch lease rejected" in logged[0]
    finally:
        monkeypatch.setattr(lock, "_holder", "host-task-A")
        bg.release_branch_lease(REPO_KEY, BRANCH)
