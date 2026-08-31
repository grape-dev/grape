"""`check` command: read-only idempotency / guard verification."""
from __future__ import annotations

import json

from ..config import Config
from ..exitcodes import EXIT_DIFF, EXIT_OK
from ..mapfile import MapFile
from ..sync_engine import hop1_pending, _expected_remote_for_target
from ..worktree import Worktree


def check_command(config: Config, args) -> int:
    wt = Worktree.create(config)
    try:
        mf = MapFile(config.map_file)

        hop1 = hop1_pending(config, wt, mf)
        target_lines = []
        in_sync = not hop1

        for t in config.active_targets:
            try:
                expected, pairs = _expected_remote_for_target(config, wt, mf, t.name)
            except Exception as e:  # noqa: BLE001 - guard errors surface as diff
                target_lines.append({"target": t.name, "status": "guard_failed",
                                     "message": str(e)})
                in_sync = False
                continue

            actual = wt.target_head(t.name) if wt.target_has_branch(t.name) else None
            if len(pairs) == 0 and actual == expected:
                target_lines.append({"target": t.name, "status": "ok"})
            else:
                target_lines.append({"target": t.name, "status": "diff",
                                     "new_commits": len(pairs),
                                     "expected": expected, "actual": actual})
                in_sync = False

        payload = {
            "in_sync": in_sync,
            "hop1_pending": len(hop1),
            "hop1_commits": hop1,
            "targets": target_lines,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for line in target_lines:
                print(line)
            if hop1:
                print(f"hop1: {len(hop1)} master commits pending cherry-pick")
        return EXIT_OK if in_sync else EXIT_DIFF
    finally:
        wt.close()
