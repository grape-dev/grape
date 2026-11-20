"""Issue #2245：coding worktree 的 .git 只读 → 写型回合无法同步/提交/推送。

覆盖：
- M1：只读 gitdir（预置 worktree 指向只读基线卷）自动迁移到会话可写 gitdir，
  迁移后 config/fetch/commit/push 可用，且对象库通过 alternates 共享（不重复 clone）。
- M2：会话启动自检（探测 .git/index.lock 写入），只读时 env 标注 + prompt 降级提示。
"""
import errno
import os
import subprocess

import pytest

from src.domains.runtime.engine import repogit


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


def _mock_readonly_prefix(monkeypatch, prefixes):
    """让 python 侧 os.open 对 prefixes 下的路径返回 EROFS（模拟只读挂载）。

    仅影响 repogit 的显式 os.open 探测；git 子进程不受影响（迁移目标在可写卷）。
    """
    real_open = os.open
    if isinstance(prefixes, (str, bytes, os.PathLike)):
        prefixes = [prefixes]
    roots = tuple(os.path.abspath(str(p)) for p in prefixes)

    def _fake_open(path, flags, *args, **kwargs):
        target = os.path.abspath(str(path))
        if any(target == r or target.startswith(r + os.sep) for r in roots):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _fake_open)


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    _git(["config", "user.email", "test@example.com"], str(src))
    _git(["config", "user.name", "Test User"], str(src))
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    monkeypatch.setattr("src.kernel.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(src))
    wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "2245")
    assert wt and os.path.isdir(wt)
    _git(["config", "user.email", "agent@example.com"], wt)
    _git(["config", "user.name", "Agent"], wt)
    return wt


class TestProbe:
    def test_writable_gitdir_detected(self, worktree):
        gitdir = repogit.resolve_gitdir(worktree)
        assert gitdir and os.path.isdir(gitdir)
        ok, why = repogit.probe_git_write(gitdir)
        assert ok, why
        assert not os.path.exists(os.path.join(gitdir, "index.lock"))

    def test_readonly_gitdir_detected(self, worktree, monkeypatch):
        gitdir = repogit.resolve_gitdir(worktree)
        _mock_readonly_prefix(monkeypatch, gitdir)
        ok, why = repogit.probe_git_write(gitdir)
        assert not ok
        assert "Read-only" in why

    def test_common_dir_of_linked_worktree(self, worktree):
        gitdir = repogit.resolve_gitdir(worktree)
        common = repogit.git_common_dir(gitdir)
        assert common and os.path.isdir(os.path.join(common, "objects"))
        assert os.path.abspath(common) != os.path.abspath(gitdir)


class TestRelocate:
    def test_readonly_gitdir_relocates_and_enables_git_writes(self, worktree, monkeypatch, tmp_path):
        gitdir = repogit.resolve_gitdir(worktree)
        common = repogit.git_common_dir(gitdir)
        # 迁移前存在未提交改动：迁移不得丢工作区内容（reset --mixed 不碰工作区）
        (tmp_path / "local-change.txt").write_text("keep-me", encoding="utf-8")
        with open(os.path.join(worktree, "local-change.txt"), "w", encoding="utf-8") as f:
            f.write("keep-me")

        _mock_readonly_prefix(monkeypatch, gitdir)
        status = repogit.ensure_worktree_git_writable("gitcode:owner/repo", worktree)

        assert status["writable"] is True
        assert status["mode"] == "relocated"
        session = status["gitdir"]
        assert session == os.path.join(worktree, repogit._SESSION_GITDIR_NAME)
        with open(os.path.join(worktree, ".git"), encoding="utf-8") as f:
            assert session in f.read()
        assert os.path.isfile(os.path.join(worktree, "local-change.txt"))
        # objects 共享：alternates 指向只读基线，不复制整库
        with open(os.path.join(session, "objects", "info", "alternates"), encoding="utf-8") as f:
            assert os.path.join(common, "objects") in f.read()

        # config / add / commit / fetch / push 全部可用
        assert _git(["config", "user.name"], worktree).returncode == 0
        assert _git(["add", "a.txt"], worktree).returncode == 0
        with open(os.path.join(worktree, "a.txt"), "w", encoding="utf-8") as f:
            f.write("v2")
        assert _git(["add", "a.txt"], worktree).returncode == 0
        r = _git(["commit", "-m", "issue2245 write"], worktree)
        assert r.returncode == 0, r.stderr
        r = _git(["fetch", "origin"], worktree)
        assert r.returncode == 0, r.stderr
        r = _git(["push", "origin", "HEAD:refs/heads/feat/issue-2245"], worktree)
        assert r.returncode == 0, r.stderr

    def test_fully_readonly_worktree_degrades_with_hint(self, worktree, monkeypatch):
        _mock_readonly_prefix(monkeypatch, [worktree, repogit.resolve_gitdir(worktree)])
        status = repogit.ensure_worktree_git_writable("gitcode:owner/repo", worktree)
        assert status["writable"] is False
        assert status["mode"] == "readonly"
        assert "READ-ONLY" in status["hint"]
        assert "commit" in status["hint"]

    def test_native_mode_keeps_writable_worktree_unchanged(self, worktree):
        before = repogit.resolve_gitdir(worktree)
        status = repogit.ensure_worktree_git_writable("gitcode:owner/repo", worktree)
        assert status == {"writable": True, "mode": "native", "gitdir": before,
                          "common_dir": repogit.git_common_dir(before),
                          "reason": "", "hint": ""}
        assert repogit.resolve_gitdir(worktree) == before


class TestStartupSelfCheck:
    def test_writable_session_marks_env_only(self, worktree):
        env = {}
        prompt = "base prompt"
        new_prompt, status = repogit.inject_git_write_context(
            {"repo_key": "gitcode:owner/repo", "agent_type": "coding", "issue_id": "2245"},
            env, prompt)
        assert status["writable"] is True
        assert env["GRAPE_GIT_WRITABLE"] == "true"
        assert "GRAPE_GIT_READONLY" not in env
        assert new_prompt == prompt

    def test_readonly_session_marks_env_and_prompt(self, worktree, monkeypatch):
        _mock_readonly_prefix(monkeypatch, [worktree, repogit.resolve_gitdir(worktree)])
        env = {}
        new_prompt, status = repogit.inject_git_write_context(
            {"repo_key": "gitcode:owner/repo", "agent_type": "coding", "issue_id": "2245"},
            env, "base prompt")
        assert status["writable"] is False
        assert env["GRAPE_GIT_WRITABLE"] == "false"
        assert env["GRAPE_GIT_READONLY"] == "true"
        assert env["GRAPE_GIT_READONLY_REASON"]
        assert "READ-ONLY git session" in new_prompt
        assert "do NOT retry git writes" in new_prompt

    def test_non_git_agent_is_noop(self):
        env, prompt = {}, "p"
        new_prompt, status = repogit.inject_git_write_context(
            {"repo_key": "gitcode:owner/repo", "agent_type": "issue", "issue_id": "2245"},
            env, prompt)
        assert status == {} and new_prompt == prompt and env == {}


class TestEnsureRepairsPresetWorktree:
    def test_reuse_readonly_preset_worktree_repairs(self, worktree, monkeypatch):
        gitdir = repogit.resolve_gitdir(worktree)
        _mock_readonly_prefix(monkeypatch, gitdir)
        wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "2245")
        assert wt == worktree
        new_gitdir = repogit.resolve_gitdir(wt)
        assert new_gitdir == os.path.join(wt, repogit._SESSION_GITDIR_NAME)
        assert repogit.probe_git_write(new_gitdir)[0] is True
        with open(os.path.join(wt, "b.txt"), "w", encoding="utf-8") as f:
            f.write("ok")
        assert _git(["add", "b.txt"], wt).returncode == 0
        assert _git(["commit", "-m", "after repair"], wt).returncode == 0


class TestCommonDirReadOnly:
    def test_common_readonly_gitdir_writable_relocates(self, worktree, monkeypatch):
        gitdir = repogit.resolve_gitdir(worktree)
        common = repogit.git_common_dir(gitdir)
        assert os.path.abspath(common) != os.path.abspath(gitdir)
        # admin gitdir 可写，但共享 common（objects/refs）只读 → commit 仍会失败
        _mock_readonly_prefix(monkeypatch, common)
        status = repogit.ensure_worktree_git_writable("gitcode:owner/repo", worktree)
        assert status["writable"] is True, status
        assert status["mode"] == "relocated"
        session = status["gitdir"]
        assert session == os.path.join(worktree, repogit._SESSION_GITDIR_NAME)

        with open(os.path.join(worktree, "c.txt"), "w", encoding="utf-8") as f:
            f.write("common-readonly")
        assert _git(["add", "c.txt"], worktree).returncode == 0
        r = _git(["commit", "-m", "write with readonly common"], worktree)
        assert r.returncode == 0, r.stderr


class TestHeadPreservation:
    def test_detached_head_preserved_after_relocate(self, worktree, monkeypatch):
        # 构造 detached HEAD：新 commit 不属于任何分支，clone 的默认分支会指向旧基线
        assert _git(["checkout", "--detach", "HEAD"], worktree).returncode == 0
        with open(os.path.join(worktree, "a.txt"), "w", encoding="utf-8") as f:
            f.write("detached-v2")
        assert _git(["add", "a.txt"], worktree).returncode == 0
        r = _git(["commit", "-m", "detached work"], worktree)
        assert r.returncode == 0, r.stderr
        head_sha = _git(["rev-parse", "HEAD"], worktree).stdout.strip()

        gitdir = repogit.resolve_gitdir(worktree)
        _mock_readonly_prefix(monkeypatch, gitdir)
        status = repogit.ensure_worktree_git_writable("gitcode:owner/repo", worktree)
        assert status["writable"] is True, status
        assert status["mode"] == "relocated"
        assert _git(["rev-parse", "HEAD"], worktree).stdout.strip() == head_sha
        assert _git(["diff", "--name-only"], worktree).stdout.strip() == ""


class TestWorktreeAddFailureGate:
    def test_non_readonly_add_failure_does_not_degrade(self, worktree, monkeypatch):
        import shutil
        shutil.rmtree(worktree)
        real_run = repogit._run_git
        degrade_calls = []

        def fake_run(args, *a, **kw):
            if "worktree" in args and "add" in args:
                return subprocess.CompletedProcess(
                    args, 128, "",
                    "fatal: 'feat/issue-2245' is already checked out at '/other/worktree'")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(repogit, "_run_git", fake_run)
        monkeypatch.setattr(
            repogit, "_create_worktree_with_session_gitdir",
            lambda *a, **kw: degrade_calls.append(1) or True)

        assert repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "2245") is None
        assert degrade_calls == []

    def test_readonly_failure_marker_detected(self):
        assert repogit._is_readonly_git_failure("fatal: Read-only file system")
        assert repogit._is_readonly_git_failure("error: Permission denied")
        assert not repogit._is_readonly_git_failure(
            "fatal: 'feat/issue-2245' is already checked out at '/x'")
