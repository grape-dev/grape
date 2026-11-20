"""Issue #2227: grape CLI capability drift guard.

The guard must probe the *actual* CLI capability (`git-repo commits --sha`)
instead of trusting `grape --version`, because deployment lines may ship
different builds under the same version string.

Covered scenarios:
- grape exists and supports `--sha`      -> PASS (exit 0)
- grape exists but lacks `--sha`         -> FAIL (exit 1, version + missing item)
- grape not installed                    -> SKIP (exit 0, no false alarm)
- probe command errors                   -> FAIL (exit 1)
- install-worker.sh / check-all.sh wiring and doc fallback commands
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = ROOT / "scripts" / "check_grape_cli_drift.py"
INSTALL_SH = ROOT / "scripts" / "install-worker.sh"
CHECK_ALL = ROOT / "scripts" / "check-all.sh"
REPO_DOC = ROOT / "help" / "api" / "10-git-repos.md"


def _fake_grape(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "grape"
    binary.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def _run_guard(binary: Path | str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GRAPE_CLI_BIN"] = str(binary)
    return subprocess.run(
        [sys.executable, str(GUARD)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def test_pass_when_sha_supported(tmp_path):
    binary = _fake_grape(
        tmp_path,
        'if [ "$1" = "--version" ]; then echo "grape 0.3.3"; exit 0; fi\n'
        'echo "Options:"\n'
        'echo "  --sha TEXT     parameter sha"\n',
    )
    proc = _run_guard(binary)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout
    assert "0.3.3" in proc.stdout


def test_fail_when_sha_missing_reports_version_and_missing_item(tmp_path):
    binary = _fake_grape(
        tmp_path,
        'if [ "$1" = "--version" ]; then echo "grape 0.2.9"; exit 0; fi\n'
        'echo "Options:"\n'
        'echo "  --owner TEXT"\n',
    )
    proc = _run_guard(binary)
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
    assert "0.2.9" in proc.stdout
    assert "Missing: --sha" in proc.stdout


def test_skip_when_grape_missing(tmp_path):
    proc = _run_guard(tmp_path / "does-not-exist")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SKIP" in proc.stdout


def test_fail_when_probe_errors(tmp_path):
    binary = _fake_grape(
        tmp_path,
        'if [ "$1" = "--version" ]; then echo "grape 0.3.3"; exit 0; fi\n'
        'echo "boom" >&2\nexit 2\n',
    )
    proc = _run_guard(binary)
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout


def test_guard_matches_standalone_sha_option_only(tmp_path):
    binary = _fake_grape(
        tmp_path,
        'if [ "$1" = "--version" ]; then echo "grape 0.3.3"; exit 0; fi\n'
        'echo "  --sha-extra TEXT"\n',
    )
    proc = _run_guard(binary)
    assert proc.returncode == 1
    assert "Missing: --sha" in proc.stdout


def test_install_worker_probes_sha_capability():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "git-repo commits --help" in content
    assert "*--sha*" in content
    assert "lacks 'git-repo commits --sha'" in content


def test_check_all_runs_drift_guard():
    content = CHECK_ALL.read_text(encoding="utf-8")
    assert "scripts/check_grape_cli_drift.py" in content


def test_doc_notes_version_and_fallback_commands():
    content = REPO_DOC.read_text(encoding="utf-8")
    assert "grape-cli >= 0.3.3" in content
    assert "check_grape_cli_drift.py" in content
    assert "pulls get" in content
    assert "contents" in content and "--ref" in content
    assert "git log" in content