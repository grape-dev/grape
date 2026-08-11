"""M3 git 基建测试（见 multi-issue-isolation-design.md §7.4）：bare mirror 基线 + per-Issue worktree"""
import os, subprocess
import pytest

from src.runtime import repogit

# 原始 clone_url_for（autouse patch 会替换 repogit.clone_url_for，URL 测试用原函数）
_ORIG_CLONE_URL = repogit.clone_url_for


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


@pytest.fixture
def git_src(tmp_path):
    """本地源仓库（含一个 commit），并让 repogit 的 clone_url 指向它。"""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    return src


@pytest.fixture(autouse=True)
def _patch(git_src, tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(git_src))
    yield


class TestCloneUrl:
    def test_gitcode_url(self):
        assert _ORIG_CLONE_URL("gitcode:org/test") == "https://gitcode.com/org/test.git"

    def test_github_url(self):
        assert _ORIG_CLONE_URL("github:org/test") == "https://github.com/org/test.git"


class TestRepoBase:
    def test_ensure_base_creates_bare_mirror(self):
        base = repogit.ensure_repo_base("gitcode:owner/repo")
        assert base and os.path.isdir(base)
        r = _git(["rev-parse", "--is-bare-repository"], base)
        assert r.stdout.strip() == "true"

    def test_fetch_refreshes(self, git_src, tmp_path):
        base = repogit.ensure_repo_base("gitcode:owner/repo")
        # 源仓库新增 commit
        (git_src / "b.txt").write_text("v2", encoding="utf-8")
        _git(["add", "-A"], str(git_src))
        _git(["commit", "-m", "second"], str(git_src))
        repogit.ensure_repo_base("gitcode:owner/repo")
        r = _git(["log", "--oneline", "-1", "origin/main"], base)
        assert "second" in r.stdout


class TestIssueWorktree:
    def test_worktree_branch_and_isolation(self):
        wt5 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        wt6 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "6")
        assert wt5 and wt6 and wt5 != wt6
        br5 = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt5)
        br6 = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt6)
        assert br5.stdout.strip() == "feat/issue-5"
        assert br6.stdout.strip() == "feat/issue-6"
        # 各自 commit 互不干扰
        open(os.path.join(wt5, "a.txt"), "w", encoding="utf-8").write("changed-5")
        _git(["add", "-A"], wt5)
        _git(["commit", "-m", "issue5 change"], wt5)
        r6 = _git(["show", "HEAD:a.txt"], wt6)
        assert r6.stdout.strip() == "v1", "issue6 不应看到 issue5 的改动"

    def test_worktree_reuse_same_issue(self):
        a = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        b = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert a == b

    def test_non_git_agent_skipped(self):
        assert repogit.ensure_issue_worktree("gitcode:owner/repo", "issue", "5") is None

    def test_cleanup_removes_worktree(self):
        wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert os.path.isdir(wt)
        repogit.cleanup_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert not os.path.isdir(wt)
        base = repogit.repo_base_dir("gitcode:owner/repo")
        r = _git(["worktree", "list"], base)
        assert wt not in r.stdout


class TestTokenNoPersist:
    def test_token_not_on_disk(self):
        repogit.ensure_repo_base("gitcode:owner/repo", token="sk_secret_123")
        repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5", token="sk_secret_123")
        base = repogit.repo_base_dir("gitcode:owner/repo")
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", "5")
        # 遍历 git 相关配置/文件，确认无 token
        for p in (os.path.join(base, "config"), os.path.join(base, "config.worktree")):
            if os.path.isfile(p):
                assert "sk_secret_123" not in open(p, encoding="utf-8", errors="ignore").read()
        for root, _d, files in os.walk(wt):
            for f in files:
                if f == ".git-askpass.sh":
                    # 辅助脚本只从环境变量读 token，不落盘
                    content = open(os.path.join(root, f), encoding="utf-8").read()
                    assert "sk_secret_123" not in content
                    assert "BOT_GITCODE_TOKEN" in content
        # 无 credentials 文件
        assert not os.path.exists(os.path.join(wt, ".git-credentials"))
