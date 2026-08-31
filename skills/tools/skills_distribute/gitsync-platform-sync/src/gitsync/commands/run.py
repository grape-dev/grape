"""`run` command: one-shot for the multica agent.

Executes: sync -> publish -> db-update. Each step runs independently;
a failure in one does not abort the others. Outputs a structured JSON summary
(with --verbose for detailed logs) and returns the worst exit code.
"""
from __future__ import annotations

import json
import sys

from ..config import Config
from ..exitcodes import EXIT_GUARD, EXIT_OK
from ..mapfile import MapFile
from ..product import skill_url_for_target, write_product
from ..sync_engine import run_sync
from ..worktree import Worktree


def run_command(config: Config, args) -> int:
    steps = {"sync": {}, "publish": {}, "db_update": {}}
    worst = EXIT_OK
    synced_platforms = set()
    failed_skills: list = []      # publish 级: 发布失败/超时的 skill 名(2026-08-16)
    publish_completed = False     # publish_command 是否正常返回(非异常中断)

    # ---- step 1: git sync ----
    wt = Worktree.create(config)
    result = None
    mf = MapFile(config.map_file)
    try:
        result = run_sync(config, wt, mf, force=getattr(args, "force", False),
                          push=True)
        target_status = {}
        for name, s in result.targets.items():
            target_status[name] = {
                "status": s["status"],
                "new_commits": s.get("new_commits", 0),
                "message": s.get("message", ""),
            }
            if s["status"] in ("guard_failed",):
                worst = max(worst, EXIT_GUARD)
        steps["sync"] = {
            "status": "ok" if not any(
                s["status"] == "guard_failed" for s in result.targets.values()
            ) else "failed",
            "hop1_cherrypicked": [c[:8] for c in result.hop1_cherrypicked],
            "targets": target_status,
        }
    except Exception as e:  # noqa: BLE001
        steps["sync"] = {"status": "failed", "error": str(e)}
        worst = max(worst, EXIT_GUARD)
    finally:
        wt.close()

    # platform success from git sync: hop1 = gitcode intermediate branch;
    # github = the target that pushes to the github remote.
    if result is not None:
        synced_platforms.add("gitcode")
        github_target = next((t for t in config.active_targets if t.remote == "github"),
                             None)
        if github_target is not None:
            ts = result.targets.get(github_target.name, {})
            if ts.get("status") in ("ok", "pushed"):
                synced_platforms.add("github")

    # ---- step 2: ClawHub publish + collect product ----
    skills_products = []
    cc = config.clawhub
    if not (cc and cc.organization):
        msg = "clawhub.organization not configured; cannot build clawhub url"
        print(f"run: {msg}", file=sys.stderr)
        steps["publish"] = {"status": "failed", "error": msg}
        worst = max(worst, EXIT_GUARD)
        print(json.dumps(steps, ensure_ascii=False,
                         indent=2 if getattr(args, "verbose", False) else None))
        return worst
    try:
        from .publish import publish_command

        publish_rc = publish_command(config, args, failed_skills=failed_skills)
        publish_completed = True
        if publish_rc != EXIT_OK:
            worst = max(worst, publish_rc)
            steps["publish"] = {"status": "failed", "exit": publish_rc}
        else:
            steps["publish"] = {"status": "ok"}
            synced_platforms.add("clawhub")
    except Exception as e:  # noqa: BLE001
        publish_completed = False
        steps["publish"] = {"status": "failed", "error": str(e)}
        worst = max(worst, EXIT_GUARD)
    steps["publish"].setdefault("failed_skills", failed_skills)

    # build product entries from the manifest — independent of publish success.
    # platforms that DID sync this round are recorded per-skill (gitcode/github
    # are repo-wide; clawhub is per-skill: only skills that actually published
    # get clawhub marked, so one failed skill doesn't un-mark the others).
    try:
        from ..manifest import Manifest

        manifest = Manifest(cc.manifest_file, cc.manifest_backup_file)
        manifest.load()
        github_branch = config.source_branch
        gt = next((t for t in config.active_targets if t.remote == "github"), None)
        if gt is not None:
            github_branch = gt.branch
        for name, entry in manifest.data.items():
            ver = manifest.highest_version(name)
            if ver is None:
                continue
            rel = entry.get("dir", name)
            repo_rel = f"{cc.skills_dir}/{rel}" if cc.skills_dir else rel
            gh_url = skill_url_for_target(
                "github", config.remotes.get("github", ""), repo_rel, github_branch,
            )
            gc_url = skill_url_for_target(
                "gitcode", config.remotes.get("gitcode", ""), repo_rel,
                config.source_branch,
            )
            ch_url = f"{cc.base_url}/{cc.organization}/skills/{name}"
            per_skill_platforms = set(synced_platforms)
            if publish_completed and name not in failed_skills:
                per_skill_platforms.add("clawhub")
            skills_products.append({
                "name": name,
                "relative_path": repo_rel,
                "version": ver,
                "url": gh_url,
                "urls": {"github": gh_url, "gitcode": gc_url, "clawhub": ch_url},
                "status": "published",
                "platforms": sorted(per_skill_platforms),
            })
    except Exception as e:  # noqa: BLE001
        steps.setdefault("publish", {})["product_build_error"] = str(e)
        worst = max(worst, EXIT_GUARD)

    # ---- write product file (before db-update reads it) ----
    if config.db.product_dir:
        try:
            write_product(config.db.product_dir, {
                "generated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "skills": skills_products,
            })
        except OSError as e:
            steps.setdefault("db_update", {})["product_write_error"] = str(e)
            worst = max(worst, EXIT_GUARD)

    # ---- step 3: db-update ----
    try:
        from .dbupdate import db_update_command

        db_rc = db_update_command(config, args)
        if db_rc != EXIT_OK:
            worst = max(worst, db_rc)
            steps["db_update"] = {"status": "failed", "exit": db_rc}
        else:
            steps["db_update"] = {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        steps["db_update"] = {"status": "failed", "error": str(e)}
        worst = max(worst, EXIT_GUARD)

    if getattr(args, "verbose", False):
        print(json.dumps(steps, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(steps, ensure_ascii=False))
    return worst
