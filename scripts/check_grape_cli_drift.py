#!/usr/bin/env python3
"""Detection guard for grape CLI capability drift (Issue #2227).

Background: `grape git-repo commits --sha <ref>` is required by deployment
pipelines to verify whether a target branch already contains a change. The
command exists in upstream grape-cli, but some worker deployment lines ship an
older/partial build that lacks the `--sha` option (same ``grape --version``,
different capability). This guard probes the *actual* CLI binary instead of
trusting the version string.

Usage:
    python3 scripts/check_grape_cli_drift.py

Environment:
    GRAPE_CLI_BIN  Path to the grape executable under test (default: `grape`
                   resolved via PATH). Useful for checking a worker venv binary
                   such as /opt/grape/.venv/bin/grape.

Exit codes:
    0  PASS (supports `git-repo commits --sha`) or SKIP (grape not installed)
    1  FAIL (grape present but the `--sha` capability / probe is unavailable)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

PREFIX = "[grape-cli-drift]"
REQUIRED_OPTION = "--sha"
PROBE_ARGV = ["git-repo", "commits", "--help"]
# `--sha` as a standalone long option in the help output (avoid matching
# `--sha-xxx` style options if upstream ever adds one).
_SHA_RE = re.compile(r"(?<![\w-])--sha(?![\w-])")


def _resolve_binary(raw: str) -> str | None:
    """Return an executable path for the configured binary, or None."""
    if os.sep in raw or (os.altsep and os.altsep in raw):
        return raw if os.path.isfile(raw) else None
    return shutil.which(raw)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )


def _version(binary: str) -> str:
    try:
        proc = _run([binary, "--version"])
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (proc.stdout or "").strip()
    return text.splitlines()[0] if text else "unknown"


def main() -> int:
    configured = os.environ.get("GRAPE_CLI_BIN", "grape").strip() or "grape"
    binary = _resolve_binary(configured)

    if binary is None:
        print(
            f"{PREFIX} SKIP: grape CLI not found (GRAPE_CLI_BIN={configured}); "
            "install grape-cli (>= 0.3.3) on this worker to enable the guard."
        )
        return 0

    version = _version(binary)

    try:
        proc = _run([binary, *PROBE_ARGV])
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"{PREFIX} FAIL: unable to probe '{configured} git-repo commits --help' "
            f"(version: {version}): {exc}"
        )
        return 1

    output = proc.stdout or ""
    has_sha = bool(_SHA_RE.search(output))
    display_version = (
        version[len("grape ") :] if version.lower().startswith("grape ") else version
    )
    if proc.returncode == 0 and has_sha:
        print(
            f"{PREFIX} PASS: grape {display_version} supports "
            f"'git-repo commits --sha' ({binary})"
        )
        return 0

    if not has_sha:
        reason = "the `--sha` option is missing"
    else:
        reason = f"'git-repo commits --help' exited {proc.returncode}"
    print(
        f"{PREFIX} FAIL: grape {display_version} ({binary}) cannot filter commits "
        f"by branch/SHA: {reason}."
    )
    print(f"{PREFIX} Missing: {REQUIRED_OPTION}")
    print(
        f"{PREFIX} Hint: upgrade grape-cli to a build supporting "
        f"'git-repo commits --sha', or use the documented fallback "
        "(pulls get / contents --ref + local git log)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())