"""Shared pytest fixtures: a real temp git repo for rewrite tests."""
import subprocess

import pytest


@pytest.fixture
def gitrepo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dev A"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "dev.a@example.com"], cwd=repo, check=True)
    return repo


def commit(repo, message, author_date="2024-01-01T10:00:00+08:00",
           committer_date="2024-01-01T10:00:00+08:00", content=None, filename="a.txt"):
    repo = str(repo)
    if content is not None:
        (subprocess.run(["git", "config", "--local", "user.name", "Dev A"],
                        cwd=repo, check=True, capture_output=True))
        p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                           check=True, capture_output=True, text=True)
        import os
        top = p.stdout.strip()
        with open(os.path.join(top, filename), "w") as f:
            f.write(content)
        subprocess.run(["git", "add", filename], cwd=repo, check=True)
    env = {
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_DATE": committer_date,
    }
    proc = subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo,
                          check=True, capture_output=True, text=True, env=env)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()
