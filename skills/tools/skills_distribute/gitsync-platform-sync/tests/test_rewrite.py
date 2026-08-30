"""Tests for the deterministic rewrite engine."""
import subprocess

from gitsync import rewrite
from tests.conftest import commit


def test_rewrite_deterministic(gitrepo):
    sha = commit(gitrepo, "feat: add a.txt", content="hello")
    info = rewrite.parse_commit_full(sha, str(gitrepo))

    new1 = rewrite.rewrite_one(str(gitrepo), info, info.parents, "Bot", "bot@example.com")
    new2 = rewrite.rewrite_one(str(gitrepo), info, info.parents, "Bot", "bot@example.com")
    assert new1 == new2


def test_rewrite_preserves_content_and_dates(gitrepo):
    sha = commit(gitrepo, "feat: add a.txt", content="hello",
                 author_date="2024-05-05T09:30:00+00:00",
                 committer_date="2024-05-06T11:45:00+00:00")
    info = rewrite.parse_commit_full(sha, str(gitrepo))
    assert info.author_date == "1714901400 +0000"
    assert info.committer_date == "1714995900 +0000"

    new = rewrite.rewrite_one(str(gitrepo), info, info.parents, "Bot", "bot@example.com")
    raw = subprocess.run(["git", "cat-file", "-p", new], cwd=str(gitrepo),
                         check=True, capture_output=True, text=True).stdout
    assert f"author Bot <bot@example.com> 1714901400 +0000" in raw
    assert f"committer Bot <bot@example.com> 1714995900 +0000" in raw
    assert info.tree in raw
    assert "feat: add a.txt" in raw


def test_rewrite_preserves_merge_structure(gitrepo):
    # main line
    base = commit(gitrepo, "base", content="base", filename="base.txt")
    # branch
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=str(gitrepo), check=True)
    feat = commit(gitrepo, "feat", content="feat", filename="feat.txt")
    subprocess.run(["git", "checkout", "-q", "master"], cwd=str(gitrepo), check=True)
    main2 = commit(gitrepo, "main2", content="main2", filename="main2.txt")
    # merge
    subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "merge feature", "feature"],
                   cwd=str(gitrepo), check=True)
    merge_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(gitrepo),
                               check=True, capture_output=True, text=True).stdout.strip()

    info = rewrite.parse_commit_full(merge_sha, str(gitrepo))
    assert len(info.parents) == 2, "merge commit must have 2 parents"

    # map parents (here trivially identity for the single-commit chain)
    new = rewrite.rewrite_one(str(gitrepo), info, info.parents, "Bot", "bot@example.com")
    new_raw = subprocess.run(["git", "cat-file", "-p", new], cwd=str(gitrepo),
                             check=True, capture_output=True, text=True).stdout
    parent_count = sum(1 for line in new_raw.splitlines() if line.startswith("parent "))
    assert parent_count == 2, "rewritten merge must keep 2 parents"


def test_parse_commit_full_fields(gitrepo):
    sha = commit(gitrepo, "msg", content="x")
    info = rewrite.parse_commit_full(sha, str(gitrepo))
    assert info.author_name == "Dev A"
    assert info.author_email == "dev.a@example.com"
    assert info.message == "msg\n"
    assert info.parents == []
