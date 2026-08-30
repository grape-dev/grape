"""`publish` command: ClawHub skill publish (A2: local version + self-healing).

Flow per skill:
  1. read manifest for highest known version
  2. candidate = bump(highest, "patch") or "1.0.0"
  3. clawhub skill publish --version candidate
     - success (or content already published) -> update manifest
     - "Version X already exists" conflict -> bump again and retry (self-heal)
  4. name/dir consistency check before publish
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..clawhub import ClawhubError, skill_publish
from ..config import Config
from ..exitcodes import EXIT_GUARD, EXIT_OK
from ..manifest import Manifest
from ..semver import bump
from ..skills import find_skills, is_excluded


def publish_command(config: Config, args, skills_root: str = "",
                    failed_skills: Optional[list] = None) -> int:
    import tempfile

    cc = config.clawhub
    cleanup = None
    if failed_skills is None:
        failed_skills = []
    if not skills_root:
        skills_root, cleanup = _checkout_skills_dir(config)
    root = Path(skills_root)
    try:
        manifest = Manifest(cc.manifest_file, cc.manifest_backup_file)
        manifest.load()

        skills = find_skills(root)
        if not skills:
            print(f"publish: no skills found under {root}", file=sys.stderr)
            return EXIT_GUARD

        # 需求3: 黑名单过滤(精确名/glob 命中不发布, 不进 manifest)
        excluded = [s.name for s in skills if is_excluded(s.name, cc.exclude_skills)]
        for ex in excluded:
            print(f"publish: EXCLUDED {ex} (in clawhub.exclude_skills)", file=sys.stderr)
        skills = [s for s in skills if not is_excluded(s.name, cc.exclude_skills)]
        if not skills:
            print(f"publish: all {len(excluded)} skills excluded, nothing to publish",
                  file=sys.stderr)
            return EXIT_OK if not excluded else EXIT_GUARD

        failures = []

        def _publish_one(skill):
            """Publish a single skill (independent unit -> can run in parallel)."""
            local_failures = []
            # name/dir consistency check
            if skill.name != skill.folder.name:
                msg = (
                    f"skill {skill.folder.name}: frontmatter name {skill.name!r} != "
                    f"dir name; mismatch policy={cc.name_dir_mismatch}"
                )
                if cc.name_dir_mismatch == "block":
                    local_failures.append(msg)
                    failed_skills.append(skill.name)
                    return skill.name, None, local_failures
                print(f"publish: WARN {msg}", file=sys.stderr)

            try:
                version = _publish_with_self_heal(
                    skill, manifest, cc.config_path,
                    mode=getattr(cc, "publish_mode", "cli"),
                    concurrency=getattr(cc, "publish_concurrency", 6),
                    skill_timeout=getattr(cc, "skill_timeout", 0),
                )
                if version is None:
                    return skill.name, None, local_failures
                print(f"publish: {skill.name} -> v{version}")
                return skill.name, version, local_failures
            except ClawhubError as e:
                msg = str(e)
                # 413 = request body too large: the skill exceeds the ClawHub
                # upload size limit. It is a content/size problem, not a
                # transient failure — surface it clearly with the skill name so
                # the operator can slim the skill or add it to exclude_skills.
                if "413" in msg or "Entity Too Large" in msg:
                    print(
                        f"publish: FAILED {skill.name}: skill exceeds ClawHub "
                        f"upload size limit (413 Request Entity Too Large); "
                        f"slim its assets/reference content or add it to "
                        f"clawhub.exclude_skills",
                        file=sys.stderr,
                    )
                else:
                    print(f"publish: FAILED {skill.name}: {msg}", file=sys.stderr)
                local_failures.append(f"{skill.name}: {msg}")
                failed_skills.append(skill.name)
                return skill.name, None, local_failures

        # 需求4: 各 skill 发布相互独立 -> 并行(线程池), 提速明显;
        # 受 ClawHub 速率限制约束, 默认 workers=3, 可环境变量覆盖
        import concurrent.futures
        import os

        workers = int(os.environ.get("GITSYNC_PUBLISH_WORKERS", "3"))
        if workers > 1 and len(skills) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(workers, len(skills))) as pool:
                futures = [pool.submit(_publish_one, s) for s in skills]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        _, _, f = fut.result()
                        failures.extend(f)
                    except Exception as e:  # noqa: BLE001
                        failures.append(f"parallel publish error: {e}")
        else:
            for s in skills:
                _, _, f = _publish_one(s)
                failures.extend(f)

        manifest.save()
        if failures:
            for f in failures:
                print(f"publish: FAILED: {f}", file=sys.stderr)
            return EXIT_GUARD
        return EXIT_OK
    finally:
        if cleanup:
            cleanup()


def _checkout_skills_dir(config: Config):
    """Clone gitcode, check out the github branch, return (skills_dir, cleanup)."""
    import shutil
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="gitsync-publish-")
    try:
        subprocess.run(
            ["git", "clone", "-q", "-b", config.source_branch, "--single-branch",
             config.remotes["gitcode"], tmp],
            check=True, capture_output=True, text=True,
        )
        skills_path = str(Path(tmp) / config.clawhub.skills_dir)
        return skills_path, (lambda: shutil.rmtree(tmp, ignore_errors=True))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _publish_with_self_heal(skill, manifest, config_path, mode="cli",
                            concurrency=6, skill_timeout=0):
    """Publish skill, bumping on version conflicts until success.

    Idempotency and manifest self-healing:

      1. Compute the local content hash. If the manifest records the same hash,
         content is unchanged -> skip (fast path).
      2. Otherwise, probe ClawHub (--dry-run, or native resolve in mode=native).
         If the content is already published (status "unchanged"), the manifest
         was stale or lost: rebuild its entry from ClawHub's reported version
         and fingerprint, then skip (no new version is published).
      3. Only when the content genuinely differs does a new version get
         published (two-digit-per-segment bump).

    mode="native" uses the built-in concurrent uploader (Plan B) with explicit
    version control (same --version semantics as the CLI); mode="cli" keeps the
    official clawhub CLI.

    Returns the published version, None if skipped (content unchanged), or
    raises ClawhubError on non-conflict failures.
    """
    from ..clawhub import skill_probe, skill_publish
    from ..clawhub_native import publish_native
    from ..skills import content_hash

    fprint = content_hash(skill)
    if manifest.content_hash(skill.name) == fprint:
        # fast path: content unchanged since last publish
        return None

    # slow path: probe ClawHub (authoritative). Rebuilds the manifest entry if
    # the content is already published, so a lost/corrupt manifest self-heals
    # without emitting a duplicate version. The probe always uses the CLI's
    # --dry-run (exact ICU fingerprint); only the upload itself is native.
    probe = skill_probe(str(skill.folder), config_path)
    probe_status = probe.get("status") or (
        "unchanged" if "already published" in probe.get("raw", "") else "unknown"
    )
    if probe_status == "unchanged":
        published_version = probe.get("version")
        if not published_version:
            raise ClawhubError(
                f"skill {skill.name}: probe reported unchanged but no version "
                f"returned: {probe.get('raw', '')[:120]}"
            )
        _record(manifest, skill, published_version, fprint)
        return None  # already published, no new version

    # content genuinely changed -> publish a new version with local version control
    highest = manifest.highest_version(skill.name)
    candidate = bump(highest, "patch") if highest else "1.0.0"
    # per-skill publish budget (2026-08-16): a hung upload must not stall the
    # whole run nor block queued tasks behind the run lock. Defaults by mode
    # (native is concurrent and fast; the CLI is serial per file).
    effective_timeout = skill_timeout or (900 if mode == "native" else 2700)

    for _attempt in range(10):  # safety cap on self-healing bumps
        if mode == "native":
            import concurrent.futures
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex.submit(publish_native, skill.folder, candidate,
                                config_path, concurrency=concurrency)
                try:
                    result = fut.result(timeout=effective_timeout)
                except concurrent.futures.TimeoutError:
                    raise ClawhubError(
                        f"skill {skill.name}: publish timed out after "
                        f"{effective_timeout}s (mode=native)"
                    )
            finally:
                # wait=False: a hung upload must not block the run behind the
                # executor's shutdown(wait=True) — the thread is left to finish
                # on its own (worst case: a late "delayed publish" that the
                # next run's probe treats as already-published, idempotent)
                ex.shutdown(wait=False)
            if "error" not in result:
                _record(manifest, skill, candidate, fprint)
                return candidate
            error = result["error"]
        else:
            result = skill_publish(str(skill.folder), candidate, config_path,
                                   timeout=effective_timeout)
            if "error" not in result:
                _record(manifest, skill, candidate, fprint)
                return candidate
            error = result["error"]
        if "already exists" in error or "Version " in error and "already" in error:
            candidate = bump(candidate, "patch")
            continue
        raise ClawhubError(error)
    raise ClawhubError(f"too many version conflicts for {skill.name}")


def _record(manifest, skill, version: str, fprint: str) -> None:
    """Record a published/known version into the manifest."""
    manifest.add_version(skill.name, version, "in_review", content_hash=fprint)
    manifest.set_skill(skill.name, {
        "dir": skill.relative_path,
        "name": skill.name,
        "content_hash": fprint,
        "versions": manifest.versions(skill.name),
    })
