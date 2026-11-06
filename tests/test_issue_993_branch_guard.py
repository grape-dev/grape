"""Issue #993：分支级写租约 + push 前置守卫测试。

覆盖四轴中的三轴：
- AC1 租约争用：同一 repo+branch 两写入方仅一方获准；
- AC2 PR 已 merged/closed 后 push 阻断并提示新建分支/PR；
- AC3 远端 head 漂移阻断并提示重新 fetch/rebase。
"""
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


# ===================== pre-push 钩子安装 =====================

def test_install_pre_push_hook(tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", "main", str(wt)], capture_output=True, text=True)
    assert bg.install_pre_push_hook(str(wt), REPO_KEY, BRANCH) is True
    hooks = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True).stdout.strip()
    hook_path = os.path.join(str(wt), hooks, "pre-push")
    assert os.path.isfile(hook_path)
    content = open(hook_path, encoding="utf-8").read()
    assert bg.HOOK_MARKER in content
    assert REPO_KEY in content and BRANCH in content
    assert os.access(hook_path, os.X_OK)
    assert bg.remove_pre_push_hook(str(wt)) is True
    assert not os.path.isfile(hook_path)


def test_install_preserves_existing_hook(tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", "main", str(wt)], capture_output=True, text=True)
    hooks = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True).stdout.strip()
    hook_path = os.path.join(str(wt), hooks, "pre-push")
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho existing\n")
    assert bg.install_pre_push_hook(str(wt), REPO_KEY, BRANCH) is True
    preserved = hook_path + ".grape-local"
    assert os.path.isfile(preserved)
    assert bg.remove_pre_push_hook(str(wt)) is True
    # 移除后恢复原钩子
    assert os.path.isfile(hook_path)
    assert "echo existing" in open(hook_path, encoding="utf-8").read()