"""Core sync engine shared by bootstrap/sync/check.

Implements the git-sync chain:
  master --hop1(cherry-pick)--> github分支 --hop2--> each target

hop1: cherry-pick new master commits onto the github branch (no -x, author
      preserved). Because the source repos are bare, we do the cherry-pick in
      a throwaway non-bare worktree built from the bare clone, then update the
      bare ref and push.
hop2: for each target, replay new github-branch commits onto the target via
      commit-tree, mapping parents through the map file. Rewrite identity when
      the target requires it, otherwise reuse the same tree/message/dates.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import git
from .config import Config
from .guards import GuardError
from .mapfile import MapFile
from .pairing import content_signature, hop1_signature
from .rewrite import parse_commit_full, rewrite_one
from .worktree import Worktree


class SyncResult:
    def __init__(self):
        self.hop1_cherrypicked: List[str] = []      # master shas picked
        self.hop1_branch_head: Optional[str] = None  # new github-branch head
        self.targets: Dict[str, dict] = {}           # per-target summary

    def target_summary(self, name: str):
        return self.targets.setdefault(name, {
            "status": "pending",
            "new_commits": 0,
            "head": None,
            "force": False,
            "message": "",
        })


def _make_working_clone(url: str) -> str:
    """Create a throwaway non-bare clone of the gitcode remote for cherry-picking.

    Cloning from the real URL ensures push-back targets the real gitcode remote.
    """
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp(prefix="gitsync-work-")
    subprocess.run(
        ["git", "clone", "-q", url, tmp],
        check=True, capture_output=True, text=True,
    )
    return tmp


def _checkout_branch(work: str, branch: str) -> None:
    import subprocess
    subprocess.run(["git", "checkout", "-q", branch], cwd=work, check=True,
                   capture_output=True, text=True)


def _cherry_pick(work: str, sha: str) -> None:
    import subprocess

    args = ["git", "cherry-pick"]
    # merge commits need -m 1 (apply net change vs first parent)
    parent_count = _parent_count(work, sha)
    if parent_count > 1:
        args.append("-m 1")
    args.append(sha)
    proc = subprocess.run(args, cwd=work, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        # "previous cherry-pick is now empty" -> the change is already applied
        # (idempotent re-run of a previously-applied merge/commit). Skip it.
        if "now empty" in err:
            subprocess.run(["git", "cherry-pick", "--skip"], cwd=work,
                           capture_output=True, text=True)
            return
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=work, capture_output=True)
        raise GuardError(f"hop1 cherry-pick {sha} failed: {err}")


def _parent_count(work: str, sha: str) -> int:
    import subprocess

    raw = subprocess.run(["git", "cat-file", "-p", sha], cwd=work,
                         capture_output=True, text=True).stdout
    count = 0
    for line in raw.splitlines():
        if line.startswith("parent "):
            count += 1
        elif line == "":
            break
    return count


def _commit_patch_id(sha: str, cwd: str) -> Optional[str]:
    """Stable patch-id of a commit's change vs its first parent.

    Mirrors `git cherry-pick -m 1` semantics: the net change applied when
    cherry-picking a (merge) commit. This is the correct idempotency check for
    "has this commit's change already been applied" — unlike tree/signature
    matching it is immune to coincidental identical trees (two different
    commits producing the same tree) and to interleaved commits on the source
    branch that shift every subsequent tree.

    Returns None for root commits (no parent to diff against).
    """
    import subprocess

    raw = subprocess.run(["git", "cat-file", "-p", sha], cwd=cwd,
                         capture_output=True, text=True).stdout
    parents = [l.split(" ", 1)[1] for l in raw.splitlines()
               if l.startswith("parent ")]
    if not parents:
        return None
    diff = subprocess.run(["git", "diff", parents[0], sha], cwd=cwd,
                          capture_output=True).stdout
    pid = subprocess.run(["git", "patch-id", "--stable"], input=diff,
                         capture_output=True).stdout.decode()
    return pid.split()[0] if pid.strip() else None


def hop1_pending(config: Config, wt: Worktree, mf: MapFile) -> List[str]:
    """Detect master commits not yet cherry-picked onto the github branch.

    Pure detection, no writes. Used by `check`.

    A master commit is "synced" iff its hop1 signature exists in the github
    branch history, OR its patch-id (net change vs its first parent) exists in
    the branch history, OR the map records a master->branch mapping for it.
    Every master commit that is NOT synced is pending. This handles
    out-of-order arrival: if a newer commit (B) reached the branch before an
    older one (A), A is still detected as pending (it is not skipped merely
    because B is already present).

    The patch-id check is the decisive one: it identifies a commit by the
    change it introduces rather than by the resulting tree, so a commit whose
    tree coincides with an unrelated branch commit (or whose tree was shifted
    by a skipped interleaved commit) is still correctly classified.
    """
    from .pairing import hop1_signature
    from .rewrite import parse_commit_full

    master_head = wt.master_head()
    branch_repo = str(wt.source_repo)
    # first-parent line only: merge side-branch commits are skipped because
    # their net changes are applied once via the merge commit (-m 1). This keeps
    # the github branch code identical to master without duplicating side-branch
    # changes.
    master_commits = git.rev_list(master_head, cwd=branch_repo, reverse=True,
                                  first_parent=True)
    branch_tip = wt.source_branch_head()
    branch_sigs = {}
    branch_patch_ids = set()
    for c in git.rev_list(branch_tip, cwd=branch_repo, reverse=True):
        branch_sigs.setdefault(hop1_signature(c, branch_repo), c)
        pid = _commit_patch_id(c, branch_repo)
        if pid:
            branch_patch_ids.add(pid)

    pending = []
    for c in master_commits:
        sig = hop1_signature(c, branch_repo)
        pid = _commit_patch_id(c, branch_repo)
        # synced if signature matches, patch-id matches (the change itself was
        # already applied, even under a shifted tree), or the map records a
        # master->branch mapping.
        if (sig in branch_sigs
                or (pid and pid in branch_patch_ids)
                or mf.master_to_branch(c)):
            continue
        pending.append(c)
    return pending


def hop1_sync(
    config: Config,
    wt: Worktree,
    mf: MapFile,
    push_back: bool = False,
) -> List[str]:
    """Cherry-pick new master commits onto the github branch.

    New = master commits newer than the last master commit whose content is
    already present in the github branch (matched by hop1 signature).
    Returns list of picked master shas (chronological, oldest first).

    With push_back=True, the cherry-picked result is pushed back to the gitcode
    github branch so subsequent hop2 consumes the updated branch head.
    """
    picked = hop1_pending(config, wt, mf)
    if not picked:
        return []

    # Do the cherry-picks in a working clone on the github branch
    work = _make_working_clone(config.remotes["gitcode"])
    try:
        _checkout_branch(work, config.source_branch)
        for sha in picked:
            _cherry_pick(work, sha)
        if push_back:
            _push_with_retry(work, config.source_branch, "origin")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return picked


def _push_with_retry(work: str, branch: str, remote: str, attempts: int = 5) -> None:
    """Push `branch` to `remote`, retrying on stale-info with fetch+rebase.

    Event-driven runs can fire concurrently; if another instance pushed the
    branch between our cherry-picks and our push, the push is rejected as
    stale. We then fetch the latest, rebase our cherry-picks on top, and retry.
    Because the cherry-picked content is deterministic, concurrent instances
    converge to the same final branch head (idempotent).
    """
    import subprocess

    for attempt in range(attempts):
        proc = subprocess.run(["git", "push", remote, branch], cwd=work,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return
        # stale info (non-fast-forward rejected) -> rebase and retry
        subprocess.run(["git", "fetch", remote, branch], cwd=work,
                       capture_output=True, text=True)
        rb = subprocess.run(
            ["git", "rebase", f"{remote}/{branch}"], cwd=work,
            capture_output=True, text=True,
        )
        if rb.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=work,
                           capture_output=True, text=True)
            raise GuardError(
                f"hop1 push to {branch} failed after rebase conflict: "
                f"{rb.stderr.strip()}"
            )
    raise GuardError(
        f"hop1 push to {branch} failed after {attempts} attempts: "
        f"{proc.stderr.strip()}"
    )


def _expected_remote_for_target(
    config: Config,
    wt: Worktree,
    mf: MapFile,
    target_name: str,
    force: bool = False,
) -> Tuple[Optional[str], List[dict]]:
    """Compute expected target head + list of (branch_sha, new_remote_sha).

    Algorithm: walk the github-branch history from its tip backward. The first
    commit (deepest) that is "already synced" is the sync point; every commit
    newer than it is pending and gets replayed.

    A branch commit is "already synced" when:
      - the map has a col1->col2 entry for it, OR
      - (map empty / fallback) its content signature exists in the target
        history (same content already mirrored).

    This handles both the steady-state (map populated) and first-run
    (map empty, target already has mirrored history) cases uniformly.
    """
    t = next(t for t in config.targets if t.name == target_name)

    # verify_against targets (e.g. downstream) mirror a rewrite target with the
    # same identity, so their expected head must equal the referenced target's
    # remote head (identical rewritten history), not the branch tip. Compute the
    # referenced target's plan and reuse its replayed pairs and expected head.
    if t.verify_against:
        ref = next((x for x in config.targets if x.name == t.verify_against), None)
        if ref is None:
            raise GuardError(
                f"target {target_name}: verify_against {t.verify_against} not found"
            )
        ref_expected, ref_pairs = _expected_remote_for_target(
            config, wt, mf, ref.name, force=force
        )
        # pairs share the branch->remote mapping; downstream gets the same
        # remote shas (identical rewrite identity).
        return ref_expected, ref_pairs

    branch_tip = wt.source_branch_head()
    branch_repo = str(wt.source_repo)
    target_repo = str(wt.target_repos[target_name])

    # Ensure target's existing objects are visible in source_repo so commit-tree
    # (executed in source_repo) can reference target parents (rewritten commits
    # from previous runs live in the target repo). Tolerate a missing branch
    # (empty target repo) — handled later by target_has_branch checks.
    git.fetch(config.remotes[t.remote], f"refs/heads/{t.branch}", cwd=branch_repo,
              check=False)

    # Build set of target content signatures (for fallback matching). For the
    # sha strategy, matching is by exact sha against the branch commit sha.
    target_sigs = None
    if wt.target_has_branch(t.name):
        target_head = wt.target_head(t.name)
        if t.match_strategy == "content":
            target_sigs = {
                content_signature(c, target_repo)
                for c in git.rev_list(target_head, cwd=target_repo, reverse=True)
            }
        else:
            # evaluate in branch_repo, which holds all objects (the target
            # clone may be missing the branch's newest commits when hop1 just
            # advanced it in this same run).
            if not git.is_ancestor(target_head, branch_tip, branch_repo):
                if not force:
                    raise GuardError(
                        f"target {target_name}: non-empty, sha not ancestor of branch — "
                        "structure mismatch; run bootstrap for the plan"
                    )
                # force: allow full overwrite; leave target_sigs None so the whole
                # branch history is replayed
                target_sigs = None
            else:
                target_sigs = set(git.rev_list(target_head, cwd=target_repo, reverse=True))

    def is_synced(branch_sha: str) -> bool:
        if t.match_strategy == "sha":
            # no-rewrite target: synced iff the sha exists in the target history
            if target_sigs is None:
                return False
            return branch_sha in target_sigs
        # rewrite target: synced iff map col1->col2 exists OR content matches
        if mf.branch_to_remote(branch_sha):
            return True
        if target_sigs is None:
            return False
        return content_signature(branch_sha, branch_repo) in target_sigs

    # walk branch history from tip backward to find the shallowest synced commit.
    # Everything strictly older (deeper) than it is either already mirrored or
    # handled; everything newer than it needs replay.
    all_commits = git.rev_list(branch_tip, cwd=branch_repo, reverse=True)  # root..tip

    if target_sigs is None:
        # empty target (no branch yet): nothing is synced, replay the whole
        # branch history (including the root commit).
        sync_point = None
    else:
        sync_point = None
        for c in reversed(all_commits):  # tip..root
            if is_synced(c):
                sync_point = c
                break
    if sync_point is None and not target_sigs is None:
        if not force:
            raise GuardError(
                f"target {target_name}: no synced commit found on the github branch — "
                "run bootstrap for the plan (empty target or structure mismatch)"
            )
        # force: full rewrite. The target history shares nothing with the branch
        # (unrelated root), so treat the target as empty and replay the whole
        # branch history (root included), mirroring the sha-strategy force path.
        target_sigs = None

    if sync_point is not None and sync_point == branch_tip:
        # nothing new; expected head is whatever the target already mirrors
        if t.match_strategy == "sha":
            return branch_tip, []
        return mf.branch_to_remote(branch_tip) or (
            wt.target_head(t.name) if wt.target_has_branch(t.name) else None
        ), []

    if sync_point is None:
        # empty target: pending = the whole branch history (root included)
        pending = list(all_commits)
    else:
        # pending = commits newer than sync_point (i.e., after it in rev-list order)
        idx = all_commits.index(sync_point)
        pending = all_commits[idx + 1:]

    # content-signature -> target sha index, for resolving parents of already
    # mirrored history (fallback when the map lacks the entry)
    target_sig_index = {}
    if wt.target_has_branch(t.name):
        for c in git.rev_list(wt.target_head(t.name), cwd=target_repo, reverse=True):
            sig = c if t.match_strategy == "sha" else content_signature(c, target_repo)
            target_sig_index.setdefault(sig, c)

    def resolve_parent(branch_parent: str) -> str:
        if t.match_strategy == "sha":
            # no-rewrite target: the branch parent sha is used as-is
            return parent_map.get(branch_parent) or branch_parent
        mapped = parent_map.get(branch_parent) or mf.branch_to_remote(branch_parent)
        if mapped:
            return mapped
        # fallback: find the target commit with the same content
        hit = target_sig_index.get(content_signature(branch_parent, branch_repo))
        if hit:
            return hit
        raise GuardError(
            f"target {target_name}: parent {branch_parent} has no mapped remote "
            "commit — history structure mismatch"
        )

    # replay each
    parent_map: Dict[str, str] = {}
    new_pairs: List[dict] = []
    for sha in pending:
        info = parse_commit_full(sha, branch_repo)
        mapped_parents = [resolve_parent(p) for p in info.parents]
        # Create the rewritten commit in branch_repo, which holds all objects
        # (source tree + target parents). The new commit is pushed to the
        # target remote later; git push ships the required objects.
        if t.rewrite_identity:
            new_sha = rewrite_one(
                branch_repo, info, mapped_parents,
                config.identity.name, config.identity.email,
            )
        else:
            # no-rewrite target: reuse the branch commit sha directly
            new_sha = sha
        parent_map[sha] = new_sha
        new_pairs.append({"branch_sha": sha, "remote_sha": new_sha, "info": info})
    if new_pairs:
        return new_pairs[-1]["remote_sha"], new_pairs
    # nothing new to replay: expected head is whatever the target already has
    if wt.target_has_branch(t.name):
        return wt.target_head(t.name), []
    return None, []


def _master_sig_index(config: Config, wt: Worktree):
    """Map hop1-signature -> master commit sha (for col3 lookup)."""
    branch_repo = str(wt.source_repo)
    index = {}
    for c in git.rev_list(wt.master_head(), cwd=branch_repo, reverse=True):
        index.setdefault(hop1_signature(c, branch_repo), c)
    return index


def run_sync(config: Config, wt: Worktree, mf: MapFile, force: bool,
             push: bool = False) -> SyncResult:
    result = SyncResult()

    # --- hop1: master -> github branch (cherry-pick + push back) ---
    picked = hop1_sync(config, wt, mf, push_back=push)
    result.hop1_cherrypicked = picked

    # --- hop2: github branch -> each target ---
    # re-read the branch head (it may have moved after hop1 push-back; the bare
    # source clone is refreshed by refetching from the gitcode remote).
    if picked and push:
        git.fetch(config.remotes["gitcode"], "+refs/heads/*:refs/heads/*",
                  cwd=str(wt.source_repo))
    branch_tip = wt.source_branch_head()
    result.hop1_branch_head = branch_tip

    for t in config.active_targets:
        summary = result.target_summary(t.name)
        try:
            expected, pairs = _expected_remote_for_target(config, wt, mf, t.name,
                                                          force=force)
            summary["new_commits"] = len(pairs)

            repo = wt.target_repos[t.name]
            actual_head = None
            if wt.target_has_branch(t.name):
                actual_head = wt.target_head(t.name)

            # guards (evaluated in the target repo, which holds the actual head;
            # expected new commits are descendants by construction, so fast-forward
            # holds unless the source was rewritten).
            if actual_head and expected and actual_head != expected:
                # ensure expected is reachable for the ancestor check by fetching
                # target refs into the source repo (which holds expected objects)
                git.fetch(
                    config.remotes[t.remote],
                    f"refs/heads/{t.branch}",
                    cwd=str(wt.source_repo),
                )
                is_acknowledged = actual_head in (config.acknowledged_commits or [])
                if not git.is_ancestor(actual_head, expected, str(wt.source_repo)):
                    if not force and not is_acknowledged:
                        raise GuardError(
                            f"target {t.name}: non-fast-forward from {actual_head} to "
                            f"{expected}; use --force only after explicit review "
                            "(or add the head to acknowledged_commits)"
                        )
                    summary["force"] = True
                    # only a genuine non-fast-forward needs a backup branch
                    from .guards import create_backup_branch
                    create_backup_branch(
                        config.remotes[t.remote], t.branch, str(repo), config.backup_ref
                    )
            summary["head"] = expected
            summary["status"] = "ok"

            # push the replayed commits from the source repo (which holds the
            # new objects) to the target remote ref. For verify_against targets
            # pairs may be empty (they reuse the referenced target's plan) but
            # the target can still be behind its expected head — push whenever
            # there is a difference.
            if push and expected and actual_head != expected:
                git.push(
                    config.remotes[t.remote],
                    f"{expected}:refs/heads/{t.branch}",
                    cwd=str(wt.source_repo),
                    force=summary.get("force", False),
                )
                summary["status"] = "pushed"
                # record map entries for the pushed commits
                if t.rewrite_identity and pairs:
                    master_by_sig = _master_sig_index(config, wt)
                    map_entries = []
                    for p in pairs:
                        branch_sha = p["branch_sha"]
                        remote_sha = p["remote_sha"]
                        master_sha = master_by_sig.get(
                            hop1_signature(branch_sha, str(wt.source_repo))
                        ) or mf.branch_to_master(branch_sha) or "-"
                        map_entries.append((branch_sha, remote_sha, master_sha))
                    if map_entries:
                        mf.append(map_entries)

            # cross-target verification: a target with verify_against must land at
            # the same head as the referenced target (downstream history == github
            # history, so their rewritten heads should match). Compare the two
            # expected heads (both computed from the same worktree state) rather
            # than the referenced target's actual head, which may be stale if it
            # was pushed earlier in this same run.
            if t.verify_against and expected:
                ref_expected = None
                # 需求2: 参照目标被禁用(不在 active_targets, 未参与本轮)时,
                # 无法用其 head 校验 —— 视为通过(该目标本就是被禁用状态)。
                for rt in config.active_targets:
                    if rt.name == t.verify_against and rt.name in result.targets:
                        ref_expected = result.targets[rt.name].get("head")
                if ref_expected is None:
                    try:
                        ref_expected, _ = _expected_remote_for_target(
                            config, wt, mf, t.verify_against, force=force
                        )
                    except Exception:  # noqa: BLE001
                        ref_expected = None
                if ref_expected is not None and ref_expected != expected:
                    summary["status"] = "guard_failed"
                    summary["message"] = (
                        f"verify_against {t.verify_against}: head {expected[:8]} "
                        f"!= reference head {ref_expected[:8]}"
                    )
                    result.targets[t.name] = summary
                    continue
            result.targets[t.name] = summary
        except GuardError as e:
            summary["status"] = "guard_failed"
            summary["message"] = str(e)
            result.targets[t.name] = summary
        except Exception as e:  # noqa: BLE001 - isolate per-target failures
            summary["status"] = "guard_failed"
            summary["message"] = f"error: {e}"
            result.targets[t.name] = summary

    return result
