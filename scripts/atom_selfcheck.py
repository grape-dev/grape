#!/usr/bin/env python3
"""Atom self-check runner.

Issue #582: run_case previously anchored *every* ``extra_env`` value that did
not contain ``://`` to the repository root, rewriting scalar env values such
as ``GITCODE_OWNER=stub`` into absolute paths. That caused self-check routing
mismatches and false-green assertions.

Anchoring rule (documented contract, see Issue #582 Spec):
  A value is anchored to the repository root **only** when the env key is a
  known path-type key (``PATH_ENV_KEYS`` whitelist, e.g. ``GRAPE_SRC_DIR``)
  or when the relative value provably exists under the repository root
  (``os.path.exists`` probe fallback covering unknown path keys).
  Everything else — scalar values, URLs (``://``), absolute paths — is passed
  through verbatim.

To extend the whitelist, add the env key to ``PATH_ENV_KEYS`` below.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Path-type env keys whose relative values are always anchored to the repo
# root. Extend this set when a new path-type env key is introduced.
PATH_ENV_KEYS = frozenset({"GRAPE_SRC_DIR"})


def anchor_env(extra_env: dict | None, repo_root: str = REPO_ROOT) -> dict:
    """Return a copy of *extra_env* with path-type values anchored to *repo_root*.

    Rules (Issue #582):
      1. Keys in ``PATH_ENV_KEYS`` (path-type, e.g. ``GRAPE_SRC_DIR``):
         relative values are joined onto *repo_root*; absolute values and
         URLs (``://``) are kept as-is.
      2. Other keys: anchored only as a probe fallback when the value looks
         relative and ``os.path.exists(repo_root / value)`` is true.
      3. Everything else (scalar values such as ``GITCODE_OWNER=stub``,
         URLs, absolute paths, non-string values) is passed through verbatim.
    """
    anchored: dict = {}
    for key, value in (extra_env or {}).items():
        if _should_anchor(key, value, repo_root):
            anchored[key] = os.path.normpath(os.path.join(repo_root, value))
        else:
            anchored[key] = value
    return anchored


def _should_anchor(key: str, value, repo_root: str) -> bool:
    """Decide whether *key*/*value* must be anchored to *repo_root*."""
    if not isinstance(value, str) or not value:
        return False
    if "://" in value:  # URL-like, never a filesystem path
        return False
    if os.path.isabs(value):  # already absolute, nothing to anchor
        return False
    if key in PATH_ENV_KEYS:  # whitelist first
        return True
    # Probe fallback: unknown key, but value exists under the repo root.
    return os.path.exists(os.path.join(repo_root, value))


def run_case(
    name: str,
    command: list[str] | str,
    extra_env: dict | None = None,
    cwd: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Run one self-check case and return a result record.

    ``extra_env`` values follow the anchoring contract documented in
    ``anchor_env`` (Issue #582): only path-type keys (whitelist +
    existence-probe fallback) are anchored to the repo root; scalar env
    values such as ``GITCODE_OWNER=stub`` are passed through verbatim.
    """
    merged_env = dict(os.environ)
    merged_env.update(anchor_env(extra_env))
    proc = subprocess.run(
        command,
        cwd=cwd or REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "name": name,
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "passed": proc.returncode == 0,
        "env": {k: v for k, v in (extra_env or {}).items()},
    }


def run_all(cases: list[dict]) -> int:
    """Run all *cases* (each a kwargs dict for ``run_case``); return exit code."""
    failures = []
    for case in cases:
        result = run_case(**case)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['name']} (rc={result['returncode']})")
        if not result["passed"]:
            failures.append(result["name"])
            if result["stderr"]:
                print(result["stderr"], file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python scripts/atom_selfcheck.py [--env KEY=VALUE ...] -- CMD``.

    Example:
      python scripts/atom_selfcheck.py --env GRAPE_SRC_DIR=src \\
          --env GITCODE_OWNER=stub -- python -c 'print("ok")'
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_env: dict = {}
    command: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--env" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            extra_env[key] = value
            i += 2
        elif argv[i] == "--":
            command = argv[i + 1:]
            break
        else:
            i += 1
    if not command:
        print(__doc__)
        return 2
    result = run_case("cli", command, extra_env=extra_env)
    print(result["stdout"], end="")
    if result["stderr"]:
        print(result["stderr"], file=sys.stderr, end="")
    return result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
