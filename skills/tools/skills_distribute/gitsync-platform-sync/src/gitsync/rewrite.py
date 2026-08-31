"""Deterministic identity-rewrite engine built on git commit-tree.

A rewritten commit preserves everything byte-for-byte (tree, message, author
date, committer date, parent structure) except author/committer name+email.
Because the rewrite is a pure function of the input commit and its mapped
parents, the same input always yields the same output SHA. This determinism is
what powers idempotency, self-healing and cross-target consistency.
"""
from __future__ import annotations

import os
import subprocess
from typing import List

from . import git


class CommitInfo:
    """Raw commit object fields needed for rewriting."""

    __slots__ = (
        "sha", "tree", "parents", "author_name", "author_email", "author_date",
        "committer_name", "committer_email", "committer_date", "message",
    )

    def __init__(self, sha, tree, parents, author_name, author_email, author_date,
                 committer_name, committer_email, committer_date, message):
        self.sha = sha
        self.tree = tree
        self.parents = parents
        self.author_name = author_name
        self.author_email = author_email
        self.author_date = author_date
        self.committer_name = committer_name
        self.committer_email = committer_email
        self.committer_date = committer_date
        self.message = message


def _split_ident_line(line: str):
    """Split 'Name <email> <epoch> <tz>' -> (name, email, "<epoch> <tz>")."""
    rest, _, tz = line.rpartition(" ")
    rest, _, epoch = rest.rpartition(" ")
    name = None
    email = None
    if "<" in rest and rest.strip().endswith(">"):
        name, email = rest.rsplit("<", 1)
        name = name.rstrip()
        email = email.rstrip(">")
    return name, email, f"{epoch} {tz}"


def parse_commit_full(sha: str, cwd: str) -> CommitInfo:
    """Parse a commit with full header fields (author/committer + dates).

    Message bytes are preserved exactly (everything after the first blank line
    that terminates the header block).
    """
    import subprocess

    proc = subprocess.run(
        ["git", "cat-file", "-p", sha], cwd=cwd, capture_output=True
    )
    if proc.returncode != 0:
        raise git.GitError(
            f"git cat-file -p {sha} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    data = proc.stdout
    # header terminates at first b"\n\n"; message is everything after
    sep = data.find(b"\n\n")
    if sep == -1:
        raw_header = data
        message_bytes = b""
    else:
        raw_header = data[:sep]
        message_bytes = data[sep + 2:]
    raw_header = raw_header.decode(errors="replace")
    headers = raw_header.split("\n")
    message = message_bytes.decode("utf-8", errors="replace")

    parents = [h.split(" ", 1)[1] for h in headers if h.startswith("parent ")]
    tree = next((h.split(" ", 1)[1] for h in headers if h.startswith("tree ")), "")

    author = next((h[len("author "):] for h in headers if h.startswith("author ")), " < >")
    committer = next(
        (h[len("committer "):] for h in headers if h.startswith("committer ")), " < >"
    )
    an, ae, ad = _split_ident_line(author)
    cn, ce, cd = _split_ident_line(committer)

    return CommitInfo(
        sha=sha,
        tree=tree,
        parents=parents,
        author_name=an or "",
        author_email=ae or "",
        author_date=ad or "",
        committer_name=cn or "",
        committer_email=ce or "",
        committer_date=cd or "",
        message=message,
    )


def rewrite_one(
    cwd: str,
    info: CommitInfo,
    parents: List[str],
    identity_name: str,
    identity_email: str,
) -> str:
    """Create a rewritten commit. `parents` are the already-mapped parent SHAs.

    All fields except author/committer identity are preserved verbatim.
    """
    return commit_tree(
        cwd=cwd,
        tree=info.tree,
        message=info.message,
        parents=parents,
        author_name=identity_name,
        author_email=identity_email,
        author_date=info.author_date,
        committer_name=identity_name,
        committer_email=identity_email,
        committer_date=info.committer_date,
    )


def commit_tree(
    cwd: str,
    tree: str,
    message: str,
    parents: List[str],
    author_name: str,
    author_email: str,
    author_date: str,
    committer_name: str,
    committer_email: str,
    committer_date: str,
) -> str:
    """Create a commit object via git commit-tree with full control of all
    ident fields. Returns the new commit sha."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_DATE": committer_date,
    })
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=message,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if proc.returncode != 0:
        raise git.GitError(
            f"git commit-tree failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()
