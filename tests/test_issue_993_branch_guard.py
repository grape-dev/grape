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
    from src.session_manager import db
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass
    yield
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
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


def test_list_prs_gitcode_paginates_until_short_page():
    """VcsApiClient.list_prs：>1 页时按 page 翻页，短页即停。"""
    from src.vcs.api_client import VcsApiClient

    calls = []

    def _req(method, path):
        import re as _re
        calls.append(path)
        page = int(_re.search(r"[?&]page=(\d+)", path).group(1))
        if page == 1:
            return [{"number": i} for i in range(100)]
        if page == 2:
            return [{"number": 100}, {"number": 101}]
        return []

    c = VcsApiClient("gitcode", "tok", "owner", "repo")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(c, "_gitcode_request", _req)
        items = c.list_prs(state="all", head=BRANCH)
    finally:
        monkeypatch.undo()
    assert len(items) == 102
    assert len(calls) == 2
    assert "state=all" in calls[0] and "per_page=100" in calls[0]


def test_list_prs_github_uses_head_owner_branch():
    from src.vcs.api_client import VcsApiClient

    calls = []

    def _req(method, path):
        calls.append(path)
        return []

    c = VcsApiClient("github", "tok", "owner", "repo")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(c, "_github_request", _req)
        c.list_prs(state="all", head=BRANCH)
    finally:
        monkeypatch.undo()
    assert calls and f"head=owner:{BRANCH}" in calls[0]


def test_list_prs_gitlab_encodes_project_path():
    from src.vcs.api_client import VcsApiClient

    calls = []

    def _req(method, path):
        calls.append(path)
        return []

    c = VcsApiClient("gitlab", "tok", "owner", "repo")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(c, "_gitlab_request", _req)
        c.list_prs(state="all", head=BRANCH)
    finally:
        monkeypatch.undo()
    assert calls and "/projects/owner%2Frepo/merge_requests" in calls[0]


def test_list_prs_error_passthrough():
    from src.vcs.api_client import VcsApiClient

    c = VcsApiClient("gitcode", "tok", "owner", "repo")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(c, "_gitcode_request",
                            lambda method, path: {"error": "HTTP 403"})
        assert c.list_prs() == {"error": "HTTP 403"}
    finally:
        monkeypatch.undo()


def test_paginate_pages_shared_helper():
    from src.vcs.api_client import paginate_pages

    pages = {1: [1, 2], 2: [3]}
    got = paginate_pages(lambda p: pages.get(p, []), page_size=2, max_pages=5)
    assert got == [1, 2, 3]


def test_consecutive_push_records_pushed_local_sha():
    """AC3：push 成功后记录 local sha，第二次合法推送不误报漂移。"""
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-1")
    assert bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="local-1")["allowed"] is True
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-2")
    assert bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="local-2")["allowed"] is True


def test_hook_records_pending_local_sha_not_stale_remote(monkeypatch):
    """钩子记录 stdin 的 local_sha（parts[1]）为候选，而非 push 前的旧 remote_sha。"""
    monkeypatch.setattr(bg, "acquire_branch_lease",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "open"})
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(f"refs/heads/{BRANCH} local-new refs/heads/{BRANCH} old-remote\n"))
    assert bg.hook_main([REPO_KEY, BRANCH]) == 0
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["pending_expected_sha"] == "local-new"
    assert state["pending_prev_sha"] == "old-remote"


def test_manual_push_success_reconciles_expected():
    """钩子推送落地后，下一次守卫把候选 sha 收敛为 expected。"""
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-1")
    bg.record_pending_push(REPO_KEY, BRANCH, "local-2", prev_remote_sha="local-1")
    expected = bg.reconcile_pending(REPO_KEY, BRANCH, "local-2")
    assert expected == "local-2"
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["expected_remote_sha"] == "local-2"
    assert state["pending_expected_sha"] == "" and state["pending_prev_sha"] == ""


def test_manual_push_failure_does_not_false_drift():
    """钩子记录的候选若未真正传输（远端仍是旧值），不得误判 remote_drift。"""
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-1")
    bg.record_pending_push(REPO_KEY, BRANCH, "local-2", prev_remote_sha="local-1")
    expected = bg.reconcile_pending(REPO_KEY, BRANCH, "local-1")
    assert expected == "local-1"
    verdict = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="local-1",
                                    expected_remote_sha=expected)
    assert verdict["allowed"] is True
    assert bg.load_lease_state(REPO_KEY, BRANCH)["pending_expected_sha"] == ""


def test_manual_push_transfer_window_concurrent_move_blocks_drift():
    """传输窗口内他人写入远端 → 候选与旧值均不匹配，下一次守卫判漂移阻断。"""
    bg.record_remote_sha(REPO_KEY, BRANCH, "local-1")
    bg.record_pending_push(REPO_KEY, BRANCH, "local-2", prev_remote_sha="local-1")
    expected = bg.reconcile_pending(REPO_KEY, BRANCH, "other-sha")
    verdict = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="other-sha",
                                    expected_remote_sha=expected)
    assert expected == "local-2"
    assert verdict["allowed"] is False and verdict["reason"] == "remote_drift"


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


# ===================== 钩子安装 / 共享目录（AC1/M1 回归） =====================

class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git(*args, cwd=None):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True, env=env)


def test_install_pre_push_hook_shared_dir_has_no_fixed_branch(tmp_path):
    """linked worktree 的 hooks 目录是仓库共享目录；钩子不得固化分支。"""
    base = tmp_path / "base"
    base.mkdir()
    _git("init", cwd=base)
    _git("checkout", "-b", "main", cwd=base)
    (base / "a.txt").write_text("a")
    _git("add", "-A", cwd=base)
    _git("commit", "-m", "init", cwd=base)
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    _git("worktree", "add", "-b", "feat/one", str(wt1), cwd=base)
    _git("worktree", "add", "-b", "feat/two", str(wt2), cwd=base)

    assert bg.install_pre_push_hook(str(wt1), REPO_KEY) is True
    assert bg.install_pre_push_hook(str(wt2), REPO_KEY) is True
    hooks1 = bg._hooks_dir(str(wt1))
    hooks2 = bg._hooks_dir(str(wt2))
    assert hooks1 and hooks1 == hooks2, "worktree 共享同一 hooks 目录"
    content = (tmp_path / "base" / ".git" / "hooks" / "pre-push").read_text()
    assert bg.HOOK_MARKER in content
    assert "feat/one" not in content and "feat/two" not in content, \
        "钩子必须运行时从 stdin 取 ref，不能固化安装时分支"


def test_installed_hook_feeds_stdin_to_local_and_guard(tmp_path, monkeypatch):
    """既有 pre-push.grape-local 消费 stdin 后，守卫仍能读到同一份 ref 列表。"""
    from pathlib import Path

    base = tmp_path / "base"
    base.mkdir()
    _git("init", cwd=base)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_py = fake_bin / "python3"
    fake_py.write_text('#!/bin/sh\ncat > "$GUARD_SEEN"\n')
    fake_py.chmod(0o755)
    # 钩子使用 sys.executable（不依赖 PATH 里的 python3），安装时注入假解释器。
    monkeypatch.setattr(bg.sys, "executable", str(fake_py))
    assert bg.install_pre_push_hook(str(base), REPO_KEY) is True
    hooks = Path(bg._hooks_dir(str(base)))

    local_seen = tmp_path / "local-stdin.txt"
    local = hooks / "pre-push.grape-local"
    local.write_text('#!/bin/sh\ncat > "$LOCAL_SEEN"\n')
    local.chmod(0o755)

    guard_seen = tmp_path / "guard-stdin.txt"
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["LOCAL_SEEN"] = str(local_seen)
    env["GUARD_SEEN"] = str(guard_seen)
    payload = "refs/heads/feat/x abc123 refs/heads/feat/x 000\n"
    r = subprocess.run(["sh", str(hooks / "pre-push"), "origin", "url"],
                       input=payload, text=True, capture_output=True, env=env)
    assert r.returncode == 0, r.stderr
    assert local_seen.read_text() == payload
    assert guard_seen.read_text() == payload


def test_hook_dynamically_uses_pushed_ref(monkeypatch):
    """钩子按 stdin 的 ref 记录状态，不受安装时分支影响。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "open"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(
        f"refs/heads/feat/other local-other refs/heads/feat/other old\n"))
    assert bg.hook_main([REPO_KEY, "feat/installed"]) == 0
    assert bg.load_lease_state(REPO_KEY, "feat/other")["pending_expected_sha"] == "local-other"
    assert bg.load_lease_state(REPO_KEY, "feat/installed") == {}


def test_hook_multi_ref_blocks_before_recording(monkeypatch):
    """多 ref 推送任一被阻断时不得记录任何期望 sha（hook 失败则整次 push 未发生）。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)

    def _pr(_rk, branch):
        return {"state": "merged", "number": 9} if branch == "feat/b" else {"state": "open"}

    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape", _pr)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(
        "refs/heads/feat/a local-a refs/heads/feat/a old\n"
        "refs/heads/feat/b local-b refs/heads/feat/b old\n"))
    assert bg.hook_main([REPO_KEY]) == 1
    assert bg.load_lease_state(REPO_KEY, "feat/a") == {}
    assert bg.load_lease_state(REPO_KEY, "feat/b") == {}


# ===================== preflight_push / resolve_remote_head =====================

def test_resolve_remote_head_parses_ls_remote(monkeypatch):
    from src.runtime import repogit
    monkeypatch.setattr(repogit, "_run_git",
                        lambda *a, **k: _R(0, "abc123\trefs/heads/feat/issue-993\n"))
    assert bg.resolve_remote_head(REPO_KEY, BRANCH) == "abc123"


def test_resolve_remote_head_fail_open(monkeypatch):
    from src.runtime import repogit
    monkeypatch.setattr(repogit, "_run_git", lambda *a, **k: _R(1, "", "boom"))
    assert bg.resolve_remote_head(REPO_KEY, BRANCH) == ""


def test_preflight_push_combines_remote_and_pr_state(monkeypatch):
    monkeypatch.setattr(bg, "resolve_remote_head", lambda *a, **k: "sha-old")
    monkeypatch.setattr(bg, "fetch_pr_state",
                        lambda *a, **k: {"state": "open", "number": 3, "base": "main"})
    got = bg.preflight_push(REPO_KEY, BRANCH, token="tok")
    assert got["allowed"] is True and got["remote_sha"] == "sha-old"
    assert got["pr"]["number"] == 3

    monkeypatch.setattr(bg, "fetch_pr_state",
                        lambda *a, **k: {"state": "merged", "number": 3, "base": "main"})
    got2 = bg.preflight_push(REPO_KEY, BRANCH, token="tok")
    assert got2["allowed"] is False and got2["reason"] == "pr_merged"


# ===================== guarded_push 自动路径 =====================

def test_guarded_push_blocks_before_git(monkeypatch):
    calls = []
    monkeypatch.setattr(bg, "preflight_push", lambda *a, **k: {
        "allowed": False, "reason": "pr_merged", "message": "merged"})
    got = bg.guarded_push(REPO_KEY, BRANCH, "/tmp", token="t",
                          run_git=lambda *a, **k: calls.append(a))
    assert got["ok"] is False and got["stage"] == "preflight"
    assert calls == []


def test_guarded_push_records_pushed_local_sha(monkeypatch):
    monkeypatch.setattr(bg, "preflight_push", lambda *a, **k: {"allowed": True})

    def _run(args, **kw):
        if "rev-parse" in args:
            return _R(0, "local-sha\n")
        return _R(0, "")

    got = bg.guarded_push(REPO_KEY, BRANCH, "/tmp", token="t", run_git=_run)
    assert got["ok"] is True and got["sha"] == "local-sha"
    assert bg.load_lease_state(REPO_KEY, BRANCH)["expected_remote_sha"] == "local-sha"


# ===================== 两条自动推送路径集成 =====================

def _make_worktree_repo(tmp_path, branch="feat/issue-42"):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    wt = tmp_path / "wt"
    wt.mkdir()
    _git("init", cwd=wt)
    _git("checkout", "-b", "main", cwd=wt)
    (wt / "a.txt").write_text("a")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    base_sha = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    _git("remote", "add", "origin", str(remote), cwd=wt)
    _git("update-ref", "refs/remotes/origin/main", base_sha, cwd=wt)
    _git("checkout", "-b", branch, cwd=wt)
    (wt / "b.txt").write_text("b")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "feature", cwd=wt)
    return wt, remote


def _patch_guard(monkeypatch):
    from src.runtime import branch_guard
    seen = {"acquire": [], "release": [], "pre": [], "record": []}
    monkeypatch.setattr(branch_guard, "acquire_branch_lease",
                        lambda rk, br, *a, **k: (seen["acquire"].append((rk, br))
                                                 or {"ok": True}))
    monkeypatch.setattr(branch_guard, "release_branch_lease",
                        lambda rk, br: seen["release"].append((rk, br)))
    monkeypatch.setattr(branch_guard, "preflight_push",
                        lambda rk, br, token="": (seen["pre"].append((rk, br))
                                                  or {"allowed": True}))
    monkeypatch.setattr(branch_guard, "record_remote_sha",
                        lambda rk, br, sha: seen["record"].append((rk, br, sha)))
    return seen


def test_agent_linked_push_path_uses_guard(tmp_path, monkeypatch):
    import json as _json

    from src.runtime import agent, repogit
    from src.vcs import api_client

    wt, remote = _make_worktree_repo(tmp_path)
    seen = _patch_guard(monkeypatch)
    monkeypatch.setattr(agent, "_get_task_token", lambda task: "tok")
    monkeypatch.setattr(repogit, "ensure_feature_branch", lambda *a, **k: True)
    monkeypatch.setattr(api_client, "create_pr_for_repo",
                        lambda **k: {"number": 7, "html_url": "u"})

    task = {"agent_type": "coding", "issue_id": "#42"}
    env = {"BOT_USERNAME": "bot", "GRAPE_LINKED_WORKTREES": _json.dumps(
        [{"repo_key": REPO_KEY, "branch": "main", "dir": str(wt)}])}
    agent._submit_linked_repo_prs(task, env)

    assert task["_linked_pr_results"][0]["status"] == "created"
    assert (REPO_KEY, "feat/issue-42") in seen["acquire"]
    assert (REPO_KEY, "feat/issue-42") in seen["pre"]
    assert any(r[0] == REPO_KEY and r[1] == "feat/issue-42" for r in seen["record"])
    assert (REPO_KEY, "feat/issue-42") in seen["release"]
    heads = _git("ls-remote", "--heads", str(remote), cwd=wt).stdout
    assert "refs/heads/feat/issue-42" in heads


def test_direct_linked_push_path_uses_guard(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    import src.repo as repo_mod
    from src.api import event_pipeline
    from src.runtime import repogit
    from src.vcs import api_client

    wt, remote = _make_worktree_repo(tmp_path)
    seen = _patch_guard(monkeypatch)
    monkeypatch.setattr(repogit, "ensure_feature_branch", lambda *a, **k: True)
    monkeypatch.setattr(repo_mod, "Repo", lambda rk: SimpleNamespace(bot_token="tok"))
    created = []
    monkeypatch.setattr(api_client, "create_pr_for_repo",
                        lambda **k: (created.append(k) or {"number": 8, "html_url": "u"}))

    event_pipeline._submit_direct_linked_prs(
        {"BOT_USERNAME": "bot", "GRAPE_LINKED_WORKTREES": _json.dumps(
            [{"repo_key": REPO_KEY, "branch": "main", "dir": str(wt)}])},
        "gitcode:org/main", "#42", "tok")

    assert (REPO_KEY, "feat/issue-42") in seen["acquire"]
    assert (REPO_KEY, "feat/issue-42") in seen["pre"]
    assert any(r[0] == REPO_KEY and r[1] == "feat/issue-42" for r in seen["record"])
    assert (REPO_KEY, "feat/issue-42") in seen["release"]
    assert created and created[0]["head"] == "feat/issue-42"
    heads = _git("ls-remote", "--heads", str(remote), cwd=wt).stdout
    assert "refs/heads/feat/issue-42" in heads


def test_lease_contention_message_contains_task_id(monkeypatch):
    """AC1：租约 holder 使用任务 ID 时，争用提示可定位到具体任务。"""
    import src.lock as lock
    monkeypatch.setattr(lock, "_holder", "host-task-A")
    first = bg.acquire_branch_lease(REPO_KEY, BRANCH, task_id="t_taskA")
    assert first["ok"] is True
    assert first["holder"] == "t_taskA"

    monkeypatch.setattr(lock, "_holder", "host-task-B")
    second = bg.acquire_branch_lease(REPO_KEY, BRANCH, task_id="t_taskB")
    assert second["ok"] is False
    assert second["reason"] == "branch_locked"
    assert second["holder"] == "t_taskA"
    assert "t_taskA" in second["message"]

    bg.release_branch_lease(REPO_KEY, BRANCH, task_id="t_taskA")
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH, task_id="t_taskB")["ok"] is True
