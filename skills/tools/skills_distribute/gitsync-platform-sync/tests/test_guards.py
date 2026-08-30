"""Tests for guard functions (backup branch, fast-forward, foreign history)."""
import subprocess

import pytest

from gitsync import git
from gitsync.guards import GuardError, create_backup_branch, ensure_fast_forward


@pytest.fixture
def bare_repo(tmp_path):
    repo = tmp_path / "repo.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    return repo


def _seed_commit(repo):
    """Create one commit in a temp non-bare clone and push to the bare repo."""
    work = str(repo) + "-work"
    subprocess.run(["git", "init", "-q", work], check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "dev@e.com"], cwd=work, check=True)
    with open(f"{work}/f.txt", "w") as f:
        f.write("x")
    subprocess.run(["git", "add", "f.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=work, check=True)
    return work


def test_create_backup_branch_existing(bare_repo):
    work = _seed_commit(bare_repo)
    name = create_backup_branch(str(bare_repo), "main", bare_repo, "refs/heads/backup/pre-sync-")
    assert name is not None
    assert name.startswith("refs/heads/backup/pre-sync-")
    # backup ref now exists in the bare repo
    assert git.ref_exists(name, cwd=str(bare_repo))
    # and it points at the same commit as main
    main_sha = git.rev_parse("refs/heads/main", cwd=str(bare_repo))
    backup_sha = git.rev_parse(name, cwd=str(bare_repo))
    assert main_sha == backup_sha


def test_create_backup_branch_missing(bare_repo):
    name = create_backup_branch(str(bare_repo), "nonexistent", bare_repo,
                                "refs/heads/backup/pre-sync-")
    assert name is None


def test_ensure_fast_forward_ff(bare_repo):
    work = _seed_commit(bare_repo)
    head = git.rev_parse("refs/heads/main", cwd=str(bare_repo))
    # head is ancestor of itself -> fast forward, no force needed
    assert ensure_fast_forward(head, head, str(bare_repo), "t", force=False) is True


def test_ensure_fast_forward_non_ff_rejects(bare_repo):
    work = _seed_commit(bare_repo)
    head = git.rev_parse("refs/heads/main", cwd=str(bare_repo))
    # a commit not descending from head -> non-fast-forward
    other = "0" * 40
    with pytest.raises(GuardError):
        ensure_fast_forward(head, other, str(bare_repo), "t", force=False)
    # with force it's allowed
    assert ensure_fast_forward(head, other, str(bare_repo), "t", force=True) is False
