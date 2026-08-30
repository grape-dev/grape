"""`bootstrap` command: first-run init with probe-first.

Probes each target to determine its plan (empty / incremental / full-rewrite),
reports it, then — unless --dry-run — runs the actual initialization via the
sync engine (populates empty targets, rewrites identity, builds the map).

--force is required when a target needs a full rewrite (non-fast-forward /
identity mismatch), and is rejected in probe-only mode if required.
"""
from __future__ import annotations

import sys

from ..config import Config
from ..exitcodes import EXIT_GUARD, EXIT_OK
from ..mapfile import MapFile
from ..sync_engine import run_sync
from ..worktree import Worktree


def bootstrap_command(config: Config, args) -> int:
    wt = Worktree.create(config)
    try:
        mf = MapFile(config.map_file)
        force = getattr(args, "force", False)
        dry_run = getattr(args, "dry_run", False)

        print("bootstrap: probing targets (probe-first)")
        plan = _probe_targets(config, wt)
        for name, p in plan.items():
            print(f"  {name}: {p['plan']}" + (f" ({p['detail']})" if p["detail"] else ""))

        needs_force = any(p["needs_force"] for p in plan.values())
        if needs_force and not force:
            print(
                "bootstrap: some targets need a full rewrite; re-run with --force "
                "after review",
                file=sys.stderr,
            )
            return EXIT_GUARD

        if dry_run:
            print("bootstrap: dry-run complete (nothing written)")
            return EXIT_OK

        # real initialization: run the sync engine (populates empty targets,
        # rewrites identity for rewrite targets, builds the map).
        result = run_sync(config, wt, mf, force=force, push=True)
        failed = [n for n, s in result.targets.items()
                  if s["status"] == "guard_failed"]
        if failed:
            for n in failed:
                print(f"bootstrap: target {n} failed: {result.targets[n]['message']}",
                      file=sys.stderr)
            return EXIT_GUARD
        print("bootstrap: done — targets initialized, map built")
        return EXIT_OK
    finally:
        wt.close()


def _probe_targets(config: Config, wt: Worktree) -> dict:
    """Determine per-target plan without writing anything."""
    from .. import git
    from ..pairing import walk_and_pair

    plan = {}
    for t in config.active_targets:
        if not wt.target_has_branch(t.name):
            plan[t.name] = {"plan": "EMPTY -> full push", "needs_force": False,
                            "detail": "no target branch yet"}
            continue

        target_head = wt.target_head(t.name)
        branch_tip = wt.source_branch_head()

        if t.match_strategy == "sha":
            if git.is_ancestor(target_head, branch_tip,
                               str(wt.target_repos[t.name])):
                plan[t.name] = {"plan": "incremental", "needs_force": False,
                                "detail": "sha-ancestor OK"}
            else:
                plan[t.name] = {"plan": "full rewrite", "needs_force": True,
                                "detail": "sha NOT ancestor (structure mismatch)"}
        else:  # content
            matched_left, _ = walk_and_pair(
                branch_tip, target_head,
                str(wt.source_repo), str(wt.target_repos[t.name]),
                strategy="content",
            )
            if matched_left is None:
                plan[t.name] = {"plan": "full rewrite", "needs_force": True,
                                "detail": "content pairing failed at first commit"}
            else:
                plan[t.name] = {"plan": "incremental", "needs_force": False,
                                "detail": f"content pairing OK (deepest pair "
                                          f"{matched_left[:8]})"}
    return plan
