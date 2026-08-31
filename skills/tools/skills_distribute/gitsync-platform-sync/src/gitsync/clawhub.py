"""Wrapper around the official `clawhub` CLI for publishing skills."""
from __future__ import annotations

import subprocess


class ClawhubError(Exception):
    """Raised when the clawhub CLI fails."""


def run_clawhub(args: list, env=None, check=True,
                timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["clawhub", *args],
        capture_output=True, text=True, env=env,
        timeout=timeout,  # hang guard (2026-08-16); publish calls pass skill_timeout
    )
    if check and proc.returncode != 0:
        raise ClawhubError(
            f"clawhub {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def skill_probe(folder: str, config_path: str = "",
                timeout: int = 120) -> dict:
    """Probe ClawHub for the skill's publish state without uploading.

    Runs `clawhub skill publish <folder> --dry-run --json`. The CLI computes
    the bundle fingerprint and reports whether the content is already
    published. This is the authoritative content-idempotency check and lets the
    manifest be rebuilt from ClawHub (the source of truth) when it is lost.

    Returns a dict with keys: status ("unchanged"|"new"|"update"), version
    (published version if unchanged), fingerprint, raw.
    """
    env = None
    if config_path:
        import os
        env = dict(os.environ)
        env["CLAWHUB_CONFIG_PATH"] = config_path
    args = ["skill", "publish", folder, "--dry-run", "--json"]
    proc = run_clawhub(args, env=env, check=False, timeout=timeout)
    data = _parse_output(proc.stdout)
    data["raw"] = proc.stdout.strip() or proc.stderr.strip()
    data["returncode"] = proc.returncode
    return data


def skill_publish(folder: str, version: str, config_path: str = "",
                  timeout: int = 2700) -> dict:
    """Publish a skill with an explicit version.

    The CLI compares the local bundle fingerprint with ClawHub and exits
    successfully (no upload) when the exact content is already published.
    Returns parsed CLI output (JSON when available). `timeout` bounds the whole
    CLI invocation (single-skill publish budget; hang guard).
    """
    env = None
    if config_path:
        import os
        env = dict(os.environ)
        env["CLAWHUB_CONFIG_PATH"] = config_path
    args = ["skill", "publish", folder, "--version", version, "--json"]
    proc = run_clawhub(args, env=env, check=False, timeout=timeout)
    if proc.returncode == 0:
        return _parse_output(proc.stdout)
    # Treat "Version X already exists" as a conflict signal for self-healing.
    return {"error": proc.stdout.strip() or proc.stderr.strip(),
            "returncode": proc.returncode}


def _parse_output(text: str) -> dict:
    import json as _json

    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        return {"raw": text}
