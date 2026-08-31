"""Safety guards: foreign-history check, non-fast-forward check, backups."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import git


class GuardError(Exception):
    """Raised when a guard refuses an operation (maps to EXIT_GUARD)."""


def check_foreign_history(
    target_head: str,
    expected_head: str,
    cwd: str,
    target_name: str,
    acknowledged: Optional[list] = None,
) -> None:
    """Reject if the target branch has commits not reachable from expected_head.

    This is an approximation: if target_head is not an ancestor of expected_head
    (meaning the sync will not be a pure fast-forward), the target may hold
    foreign commits. For pure fast-forward syncs target_head IS the ancestor.
    """
    acknowledged = acknowledged or []
    if target_head in acknowledged:
        return
    if expected_head and git.is_ancestor(target_head, expected_head, cwd):
        return
    raise GuardError(
        f"target '{target_name}': head {target_head} is not an ancestor of expected "
        f"{expected_head} — possible foreign history. Refusing (pass --force only "
        "after explicit review, or add to acknowledged_commits)."
    )


def ensure_fast_forward(
    target_head: str,
    new_head: str,
    cwd: str,
    target_name: str,
    force: bool,
) -> bool:
    """Return True if the push is a fast-forward (no force needed).

    Raises GuardError when non-fast-forward and --force not given.
    """
    if not target_head or git.is_ancestor(target_head, new_head, cwd):
        return True
    if force:
        return False
    raise GuardError(
        f"target '{target_name}': non-fast-forward update from {target_head} to "
        f"{new_head}. Use --force only when the source history was deliberately "
        "rewritten."
    )


def create_backup_branch(remote: str, branch: str, cwd: str, backup_ref: str) -> Optional[str]:
    """Create a backup ref pointing at the current target head before a force push.

    The ref name includes a pid and microseconds so concurrent invocations
    (event-triggered runs) never collide on the same name.

    Returns the created backup ref name, or None if the branch doesn't exist yet.
    """
    import os

    ref = f"refs/heads/{branch}"
    if not git.ref_exists(ref, cwd=cwd):
        return None
    sha = git.rev_parse(ref, cwd=cwd)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    name = f"{backup_ref}{stamp}-{os.getpid()}"
    git.update_ref(name, sha, cwd=cwd)
    git.push(remote, f"{name}:{name}", cwd=cwd)
    return name
