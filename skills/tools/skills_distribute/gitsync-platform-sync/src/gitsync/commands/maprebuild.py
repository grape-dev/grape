"""`map rebuild` command: rebuild the map from scratch.

The map is a cache, not the source of truth. Because the rewrite is
deterministic, it can be reconstructed:

- col1->col2 (branch -> remote): recompute via rewrite_one (deterministic);
- col3 (branch -> master): pair branch commits with master commits by the
  hop1 signature (tree + message + author fields, excluding committer).
"""
from __future__ import annotations

from ..config import Config
from ..exitcodes import EXIT_OK
from ..mapfile import MapFile
from ..pairing import hop1_signature
from ..worktree import Worktree


def map_rebuild_command(config: Config, args) -> int:
    wt = Worktree.create(config)
    try:
        from .. import git
        from ..rewrite import parse_commit_full, rewrite_one

        mf = MapFile(config.map_file)

        # --- col1->col2: recompute rewritten shas for the first rewrite target ---
        target = next((t for t in config.active_targets if t.rewrite_identity), None)
        branch_repo = str(wt.source_repo)
        if target is not None:
            target_repo = str(wt.target_repos[target.name])
            branch_tip = wt.source_branch_head()
            all_commits = git.rev_list(branch_tip, cwd=branch_repo, reverse=True)
            parent_map = {}
            col12 = []
            for sha in all_commits:
                info = parse_commit_full(sha, branch_repo)
                mapped_parents = []
                for p in info.parents:
                    mp = parent_map.get(p) or mf.branch_to_remote(p)
                    mapped_parents.append(mp)
                new_sha = rewrite_one(
                    target_repo, info, mapped_parents,
                    config.identity.name, config.identity.email,
                )
                parent_map[sha] = new_sha
                col12.append((sha, new_sha))
        else:
            col12 = []

        # --- col3: pair branch commits with master commits via hop1 signature ---
        master_tip = wt.master_head()
        master_sigs = {}
        for m in git.rev_list(master_tip, cwd=branch_repo, reverse=True):
            master_sigs.setdefault(hop1_signature(m, branch_repo), m)

        entries = []
        for sha, remote_sha in col12:
            master = master_sigs.get(hop1_signature(sha, branch_repo))
            if master:
                entries.append((sha, remote_sha, master))

        # merge with existing map (preserve any existing col3 already present)
        merged = list(entries)
        seen = {e[0] for e in merged}
        for sha, remote_sha, master in col12:
            if sha in seen:
                continue
            old = mf.branch_to_master(sha)
            if old:
                merged.append((sha, remote_sha, old))

        if merged:
            mf.append(merged)
        print(f"map rebuild: wrote {len(merged)} entries")
        return EXIT_OK
    finally:
        wt.close()
