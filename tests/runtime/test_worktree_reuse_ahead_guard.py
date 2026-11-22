"""sre/qa 复用「既 ahead 又 behind」的本地 feat 分支导致基线闸门 diverged 的回归。

现场（#92017）：qa 完成后 sre 被派发，sre 工作区复用共享 base 里的
`refs/heads/feat/issue-N`（coding 建树时 -b 建的），该分支带着未合并提交；
基线 `lidongsheng` 同时前进了 1 个提交 → ahead>0 且 behind>0 → 闸门判
diverged，任务 2 秒 failed，agent 从未启动。

约定：
- 检测/发布类（qa/sre）与「提交已推远端」的场景：复用前先把旧 tip 存进
  refs/grape-preserved/issue-N-<sha12>，再把分支对齐 origin/<base>，用干净树建树；
- 作者类 coding 且提交未推：仍阻断（不允许静默丢工作），错误六要素保持原样。
"""
import os
import subprocess

import pytest

from src.domains.runtime.engine import repogit
from src.domains.runtime.engine.repogit import WorkspaceBaselineError

REPO = "gitcode:owner/repo"


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


@pytest.fixture
def git_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    _git(["config", "user.email", "test@example.com"], str(src))
    _git(["config", "user.name", "Test User"], str(src))
    _git(["config", "receive.denyCurrentBranch", "ignore"], str(src))
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    return src


@pytest.fixture(autouse=True)
def _patch(git_src, tmp_path, monkeypatch):
    monkeypatch.setattr("src.kernel.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(git_src))
    monkeypatch.delenv("GRAPE_WORKSPACE_STRICT", raising=False)
    monkeypatch.delenv("GRAPE_WORKSPACE_TTL_HOURS", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    yield


def _wt(agent, num="92017", **kw):
    wt = repogit.ensure_issue_worktree(REPO, agent, num, **kw)
    assert wt, f"ensure_issue_worktree({agent}) 应成功预置 worktree"
    return wt


def _commit(wt, name, text):
    with open(os.path.join(wt, name), "w", encoding="utf-8") as f:
        f.write(text)
    _git(["add", "-A"], wt)
    r = _git(["commit", "-m", f"add {name}"], wt)
    assert r.returncode == 0, r.stderr
    return _sha(wt)


def _advance_base(git_src, text):
    (git_src / "a.txt").write_text(text, encoding="utf-8")
    _git(["add", "-A"], str(git_src))
    _git(["commit", "-m", f"base -> {text}"], str(git_src))
    return _sha(str(git_src))


def _sha(path, rev="HEAD"):
    return _git(["rev-parse", rev], path).stdout.strip()


def _counts(path, ref="feat/issue-92017", base="origin/main"):
    out = _git(["rev-list", "--left-right", "--count", f"{ref}...{base}"], path).stdout
    a, b = out.split()[:2]
    return int(a), int(b)


def _preserved_refs(repo_base):
    r = _git(["for-each-ref", "--format=%(refname)", "refs/grape-preserved/"], repo_base)
    return [x for x in (r.stdout or "").splitlines() if x.strip()]


def _repo_base():
    from src.kernel.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(REPO), "codebase")


def test_sre_rebuilds_clean_when_local_branch_diverged(git_src):
    """coding 本地提交未推 + 基线前进 → sre 建树不再被 diverged 拦死。"""
    cwt = _wt("coding")
    csha = _commit(cwt, "coding-work.txt", "wip")
    base_tip = _advance_base(git_src, "v2")

    swt = _wt("sre")
    assert _sha(swt) == base_tip, "sre 工作区应对齐 origin/main，而不是继承 coding 提交"
    assert not os.path.exists(os.path.join(swt, "coding-work.txt"))
    assert _counts(swt) == (0, 0)
    refs = _preserved_refs(_repo_base())
    assert any(csha[:12] in r for r in refs), f"旧 tip 必须落 preservation ref，实得 {refs}"


def test_existing_publisher_worktree_diverged_is_refused_and_rebuilt(git_src):
    """已存在的 sre 工作区停在分叉分支上 → 拒绝复用并按基线重建。"""
    swt = _wt("sre")
    ssha = _commit(swt, "sre-wip.txt", "wip")
    base_tip = _advance_base(git_src, "v3")

    swt2 = _wt("sre")
    assert _sha(swt2) == base_tip, "重建后应对齐基线 tip"
    assert not os.path.exists(os.path.join(swt2, "sre-wip.txt"))
    assert _counts(swt2) == (0, 0)
    assert any(ssha[:12] in r for r in _preserved_refs(_repo_base()))


def test_coding_diverged_with_unpushed_commits_still_blocked(git_src):
    """作者类 coding 且有未推提交 → 保持阻断（不静默丢工作）。"""
    cwt = _wt("coding")
    _commit(cwt, "coding-work.txt", "wip")
    _advance_base(git_src, "v4")
    with pytest.raises(WorkspaceBaselineError) as ei:
        _wt("coding")
    err = ei.value
    assert err.reason == "diverged"
    assert err.ahead == 1 and err.behind == 1
    assert "feat/issue-92017" in (err.actual_head or "")


def test_coding_diverged_but_pushed_aligned_clean(git_src):
    """提交已推远端（origin/feat/issue-N 含 tip）→ 允许对齐重建，不丢东西。"""
    cwt = _wt("coding")
    csha = _commit(cwt, "coding-work.txt", "wip")
    push = _git(["push", "origin", "feat/issue-92017:feat/issue-92017"], cwt)
    assert push.returncode == 0, push.stderr
    base_tip = _advance_base(git_src, "v5")

    cwt2 = _wt("coding")
    assert _sha(cwt2) == base_tip
    assert _counts(cwt2) == (0, 0)
    assert any(csha[:12] in r for r in _preserved_refs(_repo_base()))