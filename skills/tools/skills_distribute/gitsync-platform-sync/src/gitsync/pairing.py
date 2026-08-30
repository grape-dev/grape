"""Pairing logic: match commits between histories.

Two strategies:
- "content": signature = tree + message + author date + committer date
  (identity excluded). Used for the rewrite hop (branch -> remote) where SHAs
  differ but content is identical apart from identity.
- "sha": ancestor check / direct sha identity. Used for non-rewrite hops
  (ai_shell, downstream) where SHAs are expected to match exactly.
"""
from __future__ import annotations

from typing import Optional, Tuple

from .rewrite import parse_commit_full


def content_signature(sha: str, cwd: str) -> str:
    """Deterministic content signature excluding identity fields."""
    info = parse_commit_full(sha, cwd)
    return "|".join([
        info.tree,
        info.message,
        info.author_date,
        info.committer_date,
    ])


def hop1_signature(sha: str, cwd: str) -> str:
    """Signature for master <-> github-branch pairing.

    cherry-pick preserves tree, message, author name/email/date, and changes
    committer identity+date. So the hop1 signature includes author fields but
    excludes committer fields.
    """
    info = parse_commit_full(sha, cwd)
    return "|".join([
        info.tree,
        info.message,
        info.author_name,
        info.author_email,
        info.author_date,
    ])


def walk_and_pair(
    left_tip: str,
    right_tip: str,
    left_cwd: str,
    right_cwd: str,
    strategy: str,
    limit: int = 100000,
) -> Tuple[Optional[str], Optional[str]]:
    """Walk both histories backwards from tips, pairing commits.

    Returns (last_left_sha, last_right_sha) that pair up, or (None, None) if
    the very first commits don't pair (structure mismatch).
    """
    left_cur = left_tip
    right_cur = right_tip
    for _ in range(limit):
        if strategy == "content":
            s_left = content_signature(left_cur, left_cwd)
            s_right = content_signature(right_cur, right_cwd)
        else:
            s_left = left_cur
            s_right = right_cur
        if s_left != s_right:
            # If we haven't matched even the first pair, structure mismatch.
            return (None, None) if left_cur == left_tip and right_cur == right_tip \
                else (left_cur, right_cur)
        # advance to parents (single-parent linear assumption; first parent)
        left_parents = parse_commit_full(left_cur, left_cwd).parents
        right_parents = parse_commit_full(right_cur, right_cwd).parents
        if not left_parents or not right_parents:
            if not left_parents and not right_parents:
                return (left_cur, right_cur)  # both reached root, fully paired
            return (left_cur, right_cur)  # asymmetric depth
        left_cur = left_parents[0]
        right_cur = right_parents[0]
    return (left_cur, right_cur)


def find_sync_point_from_map(
    branch_tip: str,
    cwd: str,
    is_mapped,
    limit: int = 100000,
) -> Optional[str]:
    """Walk from branch_tip backwards until hitting a commit already in the map.

    Returns the last (deepest) commit that is NOT yet mapped — i.e., the
    starting point for replay. If tip is already mapped, returns None (in sync).
    """
    cur = branch_tip
    for _ in range(limit):
        if is_mapped(cur):
            return None if cur == branch_tip else cur
        parents = parse_commit_full(cur, cwd).parents
        if not parents:
            return cur
        cur = parents[0]
    return cur
