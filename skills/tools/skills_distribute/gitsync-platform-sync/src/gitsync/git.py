"""Thin wrapper around the git CLI."""
from __future__ import annotations

import subprocess
from typing import List, Optional, Sequence


class GitError(Exception):
    """Raised when a git command fails."""


def run_git(
    args: Sequence[str],
    cwd: Optional[str] = None,
    check: bool = True,
    capture: bool = True,
    env: Optional[dict] = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        env=env,
        timeout=timeout,  # git/SSH hang guard (2026-08-16): no more infinite waits
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def clone_bare(url: str, path: str) -> None:
    run_git(["clone", "--bare", url, path])


def clone(url: str, path: str) -> None:
    run_git(["clone", url, path])


def fetch(remote: str, *refspec: str, cwd: str, check: bool = True) -> None:
    run_git(["fetch", remote, *refspec], cwd=cwd, check=check)


def ls_remote(url: str, pattern: Optional[str] = None) -> List[str]:
    args = ["ls-remote", url]
    if pattern:
        args.append(pattern)
    proc = run_git(args, check=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def rev_parse(ref: str, cwd: str) -> str:
    return run_git(["rev-parse", ref], cwd=cwd).stdout.strip()


def symbolic_ref(name: str, cwd: str) -> str:
    return run_git(["symbolic-ref", "--short", name], cwd=cwd).stdout.strip()


def current_branch(cwd: str) -> str:
    return symbolic_ref("HEAD", cwd)


def is_ancestor(a: str, b: str, cwd: str) -> bool:
    proc = run_git(["merge-base", "--is-ancestor", a, b], cwd=cwd, check=False)
    return proc.returncode == 0


def log_oneline(ref_range: str, cwd: str) -> List[str]:
    proc = run_git(["log", "--oneline", ref_range], cwd=cwd, check=False)
    if proc.returncode != 0:
        return []
    return [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]


def rev_list(ref: str, cwd: str, reverse: bool = False,
             first_parent: bool = False) -> List[str]:
    args = ["rev-list", "--topo-order"]
    if reverse:
        args.append("--reverse")
    if first_parent:
        args.append("--first-parent")
    args.append(ref)
    return run_git(args, cwd=cwd).stdout.split()


def show_commit(sha: str, cwd: str) -> str:
    return run_git(["show", "-s", "--format=%H%n%an%n%ae%n%cn%n%ce%n%ad%n%cd%n%B", sha], cwd=cwd).stdout


def cat_tree(sha: str, cwd: str) -> str:
    return run_git(["cat-file", "-p", sha], cwd=cwd).stdout


def update_ref(ref: str, sha: str, cwd: str) -> None:
    run_git(["update-ref", ref, sha], cwd=cwd)


def ref_exists(ref: str, cwd: str) -> bool:
    """Return True if the given ref exists in the repository."""
    proc = run_git(["show-ref", "--verify", "--quiet", ref], cwd=cwd, check=False)
    return proc.returncode == 0


def push(remote: str, refspec: str, cwd: str, force: bool = False) -> None:
    """Push a ref to a remote.

    force=True uses plain --force. The callers (sync engine) already perform a
    non-fast-forward check and create a backup branch before force-pushing, so
    --force-with-lease adds nothing here and is unreliable because the push
    happens from a fresh bare clone with no remote-tracking refs.
    """
    args = ["push"]
    if force:
        args.append("--force")
    args += [remote, refspec]
    run_git(args, cwd=cwd)
