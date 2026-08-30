"""`lookup` command: bidirectional commit traceability across the chain.

Chain: remote (target) --map col1/col2--> github-branch --> col3/col1 --> master
Additionally ai_shell/downstream shas equal branch shas (col1).
"""
from __future__ import annotations

import json

from ..config import Config
from ..exitcodes import EXIT_GUARD, EXIT_OK
from ..mapfile import MapFile
from ..worktree import Worktree


def lookup_command(config: Config, args) -> int:
    sha = args.sha.strip().lower()
    mf = MapFile(config.map_file)

    result = {
        "input": sha,
        "branch": None,
        "remote": None,
        "master": None,
        "targets": [],
        "subject": None,
    }

    # 1. input is a master sha (col3)
    branch = mf.master_to_branch(sha)
    if branch:
        result["branch"] = branch
        result["remote"] = mf.branch_to_remote(branch)
        result["subject"] = _subject(config, branch, "branch")
        _collect_targets(config, result, branch)
        _emit(result, args)
        return EXIT_OK

    # 2. input is a github-branch sha (col1) or a target sha
    remote = mf.branch_to_remote(sha)
    if remote:
        result["remote"] = remote
        result["master"] = mf.branch_to_master(sha)
        result["subject"] = _subject(config, sha, "branch")
        result["branch"] = sha
        _collect_targets(config, result, sha)
        _emit(result, args)
        return EXIT_OK

    # 3. input is a remote sha (col2)
    branch = mf.remote_to_branch(sha)
    if branch:
        result["branch"] = branch
        result["master"] = mf.branch_to_master(branch)
        result["subject"] = _subject(config, branch, "branch")
        result["remote"] = sha
        _collect_targets(config, result, branch)
        _emit(result, args)
        return EXIT_OK

    print(f"lookup: sha {sha} not found in map", file=__import__("sys").stderr)
    return EXIT_GUARD


def _subject(config: Config, branch_sha: str, kind: str):
    try:
        wt = Worktree.create(config)
        try:
            from ..git import cat_tree
            raw = cat_tree(branch_sha, str(wt.source_repo))
            for line in raw.splitlines():
                if line.startswith("author "):
                    return line
            return raw.splitlines()[0] if raw else None
        finally:
            wt.close()
    except Exception:
        return None


def _collect_targets(config: Config, result: dict, branch_sha: str) -> None:
    mf = MapFile(config.map_file)
    for t in config.targets:
        # for rewrite targets, remote sha = map col2; for raw targets, sha == branch sha
        if t.rewrite_identity:
            remote = mf.branch_to_remote(branch_sha)
            if remote:
                result["targets"].append({"name": t.name, "sha": remote})
        else:
            result["targets"].append({"name": t.name, "sha": branch_sha})


def _emit(result: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
