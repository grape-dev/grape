"""Integration tests for the sync engine using local bare repos."""
import subprocess

import pytest

from gitsync import git
from gitsync.config import Config, IdentityConfig, TargetConfig
from gitsync.mapfile import MapFile
from gitsync.sync_engine import run_sync
from gitsync.worktree import Worktree


def _git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=check)


def _seed_gitcode(base, tmp):
    """Build gitcode with master=c3 and github-branch=c2 (lagging)."""
    gitcode = base / "gitcode.git"
    _git("init", "-q", "--bare", str(gitcode))
    seed = tmp / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=str(seed))
    _git("config", "user.name", "Dev A", cwd=str(seed))
    _git("config", "user.email", "dev.a@example.com", cwd=str(seed))
    shas = []
    for i, (content, date) in enumerate([
        ("a", "2024-01-01T10:00:00+08:00"),
        ("ab", "2024-01-02T10:00:00+08:00"),
        ("abc", "2024-01-03T10:00:00+08:00"),
    ]):
        (seed / "f.txt").write_text(content)
        _git("add", "f.txt", cwd=str(seed))
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        p = subprocess.run(["git", "commit", "-q", "-m", f"c{i+1}"], cwd=str(seed),
                           env=env, capture_output=True, text=True, check=True)
        shas.append(_git("rev-parse", "HEAD", cwd=str(seed)).stdout.strip())
    _git("branch", "github", shas[1], cwd=str(seed))  # branch at c2
    _git("remote", "add", "origin", str(gitcode), cwd=str(seed))
    _git("push", "-q", "origin", "master", "github", cwd=str(seed))
    return gitcode, seed, shas


def _make_config(base, gitcode, github):
    config_path = base / "config.yaml"
    config_path.write_text(f"""\
identity:
  name: "GitHub Mirror Bot"
  email: "mirror-bot@example.com"
remotes:
  gitcode: "{gitcode}"
  github: "{github}"
branches:
  source_master: "master"
  source_branch: "github"
targets:
  - name: github_master
    remote: github
    branch: master
    rewrite_identity: true
    match_strategy: content
map_file: "{base / 'map.txt'}"
tmpdir: ""
""")
    from gitsync.config import load_config
    return load_config(str(config_path))


def _add_master_commit(seed, content, date="2024-01-04T10:00:00+08:00"):
    (seed / "f.txt").write_text(content)
    _git("add", "f.txt", cwd=str(seed))
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "commit", "-q", "-m", "c-new"], cwd=str(seed),
                   env=env, check=True, capture_output=True, text=True)
    _git("push", "-q", "origin", "master", cwd=str(seed))
    return _git("rev-parse", "HEAD", cwd=str(seed)).stdout.strip()


def test_sync_incremental_single_target(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    gitcode, seed, shas = _seed_gitcode(base, tmp_path)
    github = base / "github.git"
    _git("init", "-q", "--bare", str(github))
    # seed github master with rewritten c1/c2 (rewrite in source_repo which has
    # all objects)
    cfg = _make_config(base, gitcode, github)
    wt = Worktree.create(cfg)
    try:
        from gitsync.rewrite import parse_commit_full, rewrite_one
        source = str(wt.source_repo)
        c1 = parse_commit_full(shas[0], source)
        c2 = parse_commit_full(shas[1], source)
        r1 = rewrite_one(source, c1, [], "Bot", "bot@example.com")
        r2 = rewrite_one(source, c2, [r1], "Bot", "bot@example.com")
        # push rewritten chain to github as master (ships objects + sets ref)
        _git("--git-dir", str(source), "push", str(github),
             f"{r2}:refs/heads/master")
    finally:
        wt.close()

    # master now has c3 (seed) + c4 (new_sha); github branch at c2.
    # sync should cherry-pick c3+c4 into the branch, then rewrite+push to github.
    new_sha = _add_master_commit(seed, "abcd")
    wt = Worktree.create(cfg)
    try:
        from gitsync.sync_engine import hop1_pending
        mf = MapFile(cfg.map_file)
        pending = hop1_pending(cfg, wt, mf)
        assert len(pending) == 2, f"expected 2 pending, got {[p[:8] for p in pending]}"
        assert pending[1] == new_sha

        result = run_sync(cfg, wt, mf, force=False, push=True)
        assert result.targets["github_master"]["status"] == "pushed"
        assert [p[:8] for p in result.hop1_cherrypicked] == [p[:8] for p in pending]
    finally:
        wt.close()

    # github master should now have rewritten commits (Bot identity)
    head = _git("--git-dir", str(github), "rev-parse", "refs/heads/master").stdout.strip()
    author = _git("--git-dir", str(github), "show", "-s",
                  "--format=%an", head).stdout.strip()
    assert author == "GitHub Mirror Bot"


def _make_sha_target_config(base, gitcode, aishell):
    """Config with a single no-rewrite (sha) target against an empty aishell."""
    cfg_path = base / "config.yaml"
    cfg_path.write_text(f"""\
identity:
  name: "Bot"
  email: "bot@example.com"
remotes:
  gitcode: "{gitcode}"
  gitcode_ai_shell: "{aishell}"
branches:
  source_master: "github"
  source_branch: "github"
targets:
  - name: ai_shell
    remote: gitcode_ai_shell
    branch: ai_shell
    rewrite_identity: false
    match_strategy: sha
map_file: "{base / 'map.txt'}"
tmpdir: ""
""")
    from gitsync.config import load_config
    return load_config(str(cfg_path))


def _seed_linear_branch(base, tmp, n):
    """Seed gitcode with a linear github branch of n commits. Returns cfg."""
    gitcode = base / "gitcode.git"
    aishell = base / "aishell.git"
    _git("init", "-q", "--bare", str(gitcode))
    _git("init", "-q", "--bare", str(aishell))
    seed = tmp / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=str(seed))
    _git("config", "user.name", "Dev", cwd=str(seed))
    _git("config", "user.email", "dev@e.com", cwd=str(seed))
    for i in range(n):
        (seed / "f.txt").write_text(f"x{i}")
        _git("add", "f.txt", cwd=str(seed))
        env = {"GIT_AUTHOR_DATE": f"2024-01-0{i+1}T10:00:00+08:00",
               "GIT_COMMITTER_DATE": f"2024-01-0{i+1}T10:00:00+08:00"}
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=str(seed),
                       env=env, check=True, capture_output=True, text=True)
    _git("branch", "-M", "github", cwd=str(seed))
    _git("remote", "add", "origin", str(gitcode), cwd=str(seed))
    _git("push", "-q", "origin", "github", cwd=str(seed))
    return _make_sha_target_config(base, gitcode, aishell)


def test_empty_target_multi_commit_syncs_full_history(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    cfg = _seed_linear_branch(base, tmp_path, 3)
    aishell = base / "aishell.git"
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=False, push=True)
        assert result.targets["ai_shell"]["status"] == "pushed"
        assert result.targets["ai_shell"]["new_commits"] == 3
    finally:
        wt.close()
    # aishell ai_shell branch now has all 3 commits
    log = _git("--git-dir", str(aishell), "log", "--oneline", "refs/heads/ai_shell"
               ).stdout.splitlines()
    assert len(log) == 3, f"expected 3 commits, got {log}"


def test_empty_target_single_commit_syncs(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    cfg = _seed_linear_branch(base, tmp_path, 1)
    aishell = base / "aishell.git"
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=False, push=True)
        assert result.targets["ai_shell"]["status"] == "pushed"
        assert result.targets["ai_shell"]["new_commits"] == 1
    finally:
        wt.close()
    log = _git("--git-dir", str(aishell), "log", "--oneline", "refs/heads/ai_shell"
               ).stdout.splitlines()
    assert len(log) == 1


def test_force_push_overwrites_diverged_target(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    cfg = _seed_linear_branch(base, tmp_path, 2)
    aishell = base / "aishell.git"

    # diverge the aishell ai_shell branch with an unrelated root commit
    seed = base / "diverged"
    seed.mkdir()
    _git("init", "-q", cwd=str(seed))
    _git("config", "user.name", "Other", cwd=str(seed))
    _git("config", "user.email", "other@e.com", cwd=str(seed))
    (seed / "g.txt").write_text("y")
    _git("add", "g.txt", cwd=str(seed))
    subprocess.run(["git", "commit", "-q", "-m", "other"], cwd=str(seed),
                   check=True, capture_output=True, text=True)
    _git("branch", "-M", "ai_shell", cwd=str(seed))
    _git("remote", "add", "origin", str(aishell), cwd=str(seed))
    _git("push", "-q", "origin", "ai_shell", cwd=str(seed))

    # without --force, a non-fast-forward should be refused
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=False, push=True)
        assert result.targets["ai_shell"]["status"] == "guard_failed"
    finally:
        wt.close()

    # with --force, it should overwrite (backup branch created, new head pushed)
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=True, push=True)
        assert result.targets["ai_shell"]["status"] == "pushed"
    finally:
        wt.close()
    log = _git("--git-dir", str(aishell), "log", "--oneline", "refs/heads/ai_shell"
               ).stdout.splitlines()
    assert len(log) == 2
    # a backup branch should exist
    backup = _git("--git-dir", str(aishell), "for-each-ref",
                  "refs/heads/backup/").stdout.strip()
    assert backup, "expected a backup branch after force push"


def test_force_content_full_rewrite_when_no_common_ancestor(tmp_path):
    """Content target with unrelated root history: force replays the whole branch.

    Mirrors the sha-strategy force path: when the target shares no content with
    the source branch (no synced commit), --force treats it as empty and does a
    full rewrite instead of refusing with "no synced commit found".
    """
    base = tmp_path / "base"
    base.mkdir()
    gitcode, seed, _ = _seed_gitcode(base, tmp_path)  # branch github at c2
    github = base / "github.git"
    _git("init", "-q", "--bare", str(github))

    # seed github master with an UNRELATED root commit (no common ancestor)
    seed_gh = tmp_path / "seed-gh"
    seed_gh.mkdir()
    _git("init", "-q", cwd=str(seed_gh))
    _git("config", "user.name", "Other", cwd=str(seed_gh))
    _git("config", "user.email", "other@e.com", cwd=str(seed_gh))
    (seed_gh / "g.txt").write_text("unrelated")
    _git("add", "g.txt", cwd=str(seed_gh))
    subprocess.run(["git", "commit", "-q", "-m", "foreign"], cwd=str(seed_gh),
                   check=True, capture_output=True, text=True)
    _git("branch", "-M", "master", cwd=str(seed_gh))
    _git("remote", "add", "origin", str(github), cwd=str(seed_gh))
    _git("push", "-q", "origin", "master", cwd=str(seed_gh))

    cfg = _make_config(base, gitcode, github)

    # without --force -> refused with "no synced commit"
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=False, push=True)
        assert result.targets["github_master"]["status"] == "guard_failed"
        assert "no synced commit" in result.targets["github_master"]["message"]
    finally:
        wt.close()

    # with --force -> full rewrite pushed, backup branch created
    wt = Worktree.create(cfg)
    try:
        result = run_sync(cfg, wt, MapFile(cfg.map_file), force=True, push=True)
        assert result.targets["github_master"]["status"] == "pushed"
    finally:
        wt.close()
    log = _git("--git-dir", str(github), "log", "--oneline",
               "refs/heads/master").stdout.splitlines()
    assert len(log) == 3, f"expected 3 rewritten commits, got {log}"
    backup = _git("--git-dir", str(github), "for-each-ref",
                  "refs/heads/backup/").stdout.strip()
    assert backup, "expected a backup branch after force push"


def _seed_multi_target(base, tmp, n):
    """gitcode: master with n commits, github-branch lagging behind.

    Returns (cfg, gitcode, github) with github target only.
    """
    gitcode = base / "gitcode.git"
    github = base / "github.git"
    _git("init", "-q", "--bare", str(gitcode))
    _git("init", "-q", "--bare", str(github))
    seed = tmp / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=str(seed))
    _git("config", "user.name", "Dev", cwd=str(seed))
    _git("config", "user.email", "dev@e.com", cwd=str(seed))
    for i in range(n):
        (seed / "f.txt").write_text(f"x{i}")
        _git("add", "f.txt", cwd=str(seed))
        env = {"GIT_AUTHOR_DATE": f"2024-01-0{i+1}T10:00:00+08:00",
               "GIT_COMMITTER_DATE": f"2024-01-0{i+1}T10:00:00+08:00"}
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=str(seed),
                       env=env, check=True, capture_output=True, text=True)
    _git("branch", "github", cwd=str(seed))
    _git("remote", "add", "origin", str(gitcode), cwd=str(seed))
    _git("push", "-q", "origin", "master", "github", cwd=str(seed))
    # seed github with the rewritten history of the github branch (so it's a
    # fast-forward target that concurrency can race on)
    from gitsync.rewrite import parse_commit_full, rewrite_one
    cfg_path = base / "config.yaml"
    cfg_path.write_text(f"""\
identity:
  name: "Bot"
  email: "bot@example.com"
remotes:
  gitcode: "{gitcode}"
  github: "{github}"
branches:
  source_master: "master"
  source_branch: "github"
targets:
  - name: github_master
    remote: github
    branch: master
    rewrite_identity: true
    match_strategy: content
map_file: "{base / 'map.txt'}"
tmpdir: ""
""")
    from gitsync.config import load_config
    from gitsync.worktree import Worktree
    cfg = load_config(str(cfg_path))
    wt = Worktree.create(cfg)
    try:
        branch_repo = str(wt.source_repo)
        branch_tip = wt.source_branch_head()
        commits = git.rev_list(branch_tip, cwd=branch_repo, reverse=True)
        parent = None
        head = None
        for c in commits:
            info = parse_commit_full(c, branch_repo)
            new = rewrite_one(branch_repo, info, [parent] if parent else [],
                              "Bot", "bot@example.com")
            parent = new
            head = new
        # push the rewritten chain to github as master (ships objects + sets ref)
        _git("--git-dir", str(branch_repo), "push", str(github),
             f"{head}:refs/heads/master")
    finally:
        wt.close()
    return cfg, gitcode, github


def test_concurrent_event_runs_converge(tmp_path):
    """Multiple event-triggered runs racing on the same source converge.

    Each run picks up the full master history (idempotent), pushes to the
    github branch (with fetch+rebase retry), and pushes the rewritten head to
    github. Final state is complete and consistent.
    """
    import threading

    base = tmp_path / "base"
    base.mkdir()
    cfg, gitcode, github = _seed_multi_target(base, tmp_path, 3)

    # add a new commit so there is work to do
    seed = tmp_path / "seed"
    (seed / "f.txt").write_text("new")
    _git("add", "f.txt", cwd=str(seed))
    env = {"GIT_AUTHOR_DATE": "2024-01-09T10:00:00+08:00",
           "GIT_COMMITTER_DATE": "2024-01-09T10:00:00+08:00"}
    subprocess.run(["git", "commit", "-q", "-m", "c-new"], cwd=str(seed),
                   env=env, check=True, capture_output=True, text=True)
    _git("push", "-q", "origin", "master", cwd=str(seed))

    results = {}
    def worker(name):
        wt = Worktree.create(cfg)
        try:
            r = run_sync(cfg, wt, MapFile(cfg.map_file), force=False, push=True)
            results[name] = r.targets["github_master"]
        finally:
            wt.close()

    threads = [threading.Thread(target=worker, args=(f"i{n}",))
               for n in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # at least one instance pushed; none crashed the whole run
    statuses = [results[t]["status"] for t in results]
    assert any(s in ("pushed", "ok") for s in statuses), results

    # github master is complete: rewritten c1..c-new (5 commits)
    log = _git("--git-dir", str(github), "log", "--oneline",
               "refs/heads/master").stdout.splitlines()
    assert len(log) == 4, f"expected 4 commits, got {log}"

    # branch on gitcode has all master commits
    bhead = _git("--git-dir", str(gitcode), "rev-parse",
                 "refs/heads/master").stdout.strip()
    ghhead = _git("--git-dir", str(gitcode), "rev-parse",
                  "refs/heads/github").stdout.strip()
    # github branch head content == master head content (hop1 signature excludes
    # committer, which cherry-pick rewrites)
    from gitsync.pairing import hop1_signature
    sig_branch = hop1_signature(ghhead, str(gitcode))
    sig_master = hop1_signature(bhead, str(gitcode))
    assert sig_branch == sig_master, "github branch should match master content"


def test_hop1_handles_merge_commit(tmp_path):
    """A merge commit on master is cherry-picked with -m 1.

    Side-branch commits are skipped (their net change is applied once via the
    merge), and the github branch ends with the same tree as master.
    """
    from gitsync.sync_engine import hop1_pending, hop1_sync

    base = tmp_path / "base"
    base.mkdir()
    gitcode = base / "gitcode.git"
    github = base / "github.git"
    _git("init", "-q", "--bare", str(gitcode))
    _git("init", "-q", "--bare", str(github))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=str(seed))
    _git("config", "user.name", "Dev", cwd=str(seed))
    _git("config", "user.email", "dev@e.com", cwd=str(seed))

    # mainline: c1 (base), c2 (side branch diverges from c1)
    (seed / "f.txt").write_text("base")
    _git("add", "f.txt", cwd=str(seed))
    env = {"GIT_AUTHOR_DATE": "2024-01-01T10:00:00+08:00",
           "GIT_COMMITTER_DATE": "2024-01-01T10:00:00+08:00"}
    subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=str(seed),
                   env=env, check=True, capture_output=True, text=True)

    # side branch: c2 side commit
    _git("checkout", "-q", "-b", "side", cwd=str(seed))
    (seed / "side.txt").write_text("side")
    _git("add", "side.txt", cwd=str(seed))
    env = {"GIT_AUTHOR_DATE": "2024-01-02T10:00:00+08:00",
           "GIT_COMMITTER_DATE": "2024-01-02T10:00:00+08:00"}
    subprocess.run(["git", "commit", "-q", "-m", "c2-side"], cwd=str(seed),
                   env=env, check=True, capture_output=True, text=True)

    # merge side into mainline
    _git("checkout", "-q", "master", cwd=str(seed))
    _git("merge", "--no-ff", "-q", "-m", "merge side", "side", cwd=str(seed))
    merge_sha = _git("rev-parse", "HEAD", cwd=str(seed)).stdout.strip()

    _git("remote", "add", "origin", str(gitcode), cwd=str(seed))
    _git("push", "-q", "origin", "master", cwd=str(seed))
    # github branch starts at c1 (before the merge)
    _git("branch", "github", "HEAD~1", cwd=str(seed))
    _git("push", "-q", "origin", "github", cwd=str(seed))

    cfg_path = base / "config.yaml"
    cfg_path.write_text(f"""\
identity:
  name: "Bot"
  email: "bot@example.com"
remotes:
  gitcode: "{gitcode}"
  github: "{github}"
branches:
  source_master: "master"
  source_branch: "github"
targets:
  - name: github_master
    remote: github
    branch: master
    rewrite_identity: true
    match_strategy: content
map_file: "{base / 'map.txt'}"
tmpdir: ""
""")
    from gitsync.config import load_config
    cfg = load_config(str(cfg_path))

    wt = Worktree.create(cfg)
    try:
        pending = hop1_pending(cfg, wt, MapFile(cfg.map_file))
        # only the merge commit is pending (side commit is covered by -m 1)
        assert pending == [merge_sha], pending

        picked = hop1_sync(cfg, wt, MapFile(cfg.map_file), push_back=True)
        assert picked == [merge_sha], picked
    finally:
        wt.close()

    # github branch tree should now match master tree
    bhead = _git("--git-dir", str(gitcode), "rev-parse",
                 "refs/heads/master").stdout.strip()
    ghhead = _git("--git-dir", str(gitcode), "rev-parse",
                  "refs/heads/github").stdout.strip()
    master_tree = _git("--git-dir", str(gitcode), "rev-parse",
                       f"{bhead}^{{tree}}").stdout.strip()
    branch_tree = _git("--git-dir", str(gitcode), "rev-parse",
                       f"{ghhead}^{{tree}}").stdout.strip()
    assert master_tree == branch_tree, "github branch tree should match master tree"
