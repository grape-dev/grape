"""`sync` command: incremental multi-target git sync."""
from __future__ import annotations

import sys

from ..config import Config
from ..exitcodes import EXIT_GUARD, EXIT_OK
from ..mapfile import MapFile
from ..sync_engine import run_sync
from ..worktree import Worktree


def sync_command(config: Config, args) -> int:
    wt = Worktree.create(config)
    try:
        mf = MapFile(config.map_file)
        result = run_sync(config, wt, mf, force=getattr(args, "force", False),
                          push=True)

        if getattr(args, "verbose", False):
            print(f"hop1: cherry-picked {len(result.hop1_cherrypicked)} commits")
        worst = EXIT_OK
        for name, summary in result.targets.items():
            if summary["status"] == "ok":
                if getattr(args, "verbose", False):
                    print(
                        f"target {name}: ok ({summary['new_commits']} new commits, "
                        f"head {summary['head']})"
                    )
            elif summary["status"] == "guard_failed":
                print(f"target {name}: GUARD FAILED: {summary['message']}", file=sys.stderr)
                worst = EXIT_GUARD
        return worst
    finally:
        wt.close()
