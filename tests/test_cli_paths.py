"""Tests for :mod:`src.kernel.cli_paths` — user-local CLI discovery under systemd."""
from __future__ import annotations

import os
import shutil
import stat

from src.kernel.cli_paths import USER_BIN_DIRS, ensure_user_bin_dirs


def _fake_cli(directory: str, name: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_ensure_user_bin_dirs_makes_cli_discoverable(tmp_path, monkeypatch):
    """A CLI in ~/.local/bin becomes visible to shutil.which after the call."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _fake_cli(str(home / ".local" / "bin"), "fake-agent-cli")

    assert shutil.which("fake-agent-cli") is None

    added = ensure_user_bin_dirs()

    assert str(home / ".local" / "bin") in added
    assert shutil.which("fake-agent-cli") is not None


def test_ensure_user_bin_dirs_is_idempotent(tmp_path, monkeypatch):
    """Calling it repeatedly must not duplicate PATH entries."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _fake_cli(str(home / ".local" / "bin"), "fake-agent-cli")

    first = ensure_user_bin_dirs()
    second = ensure_user_bin_dirs()

    assert first
    assert second == []
    parts = os.environ["PATH"].split(os.pathsep)
    assert len(parts) == len(set(parts))


def test_ensure_user_bin_dirs_skips_missing_dirs(tmp_path, monkeypatch):
    """Non-existent user dirs stay out of PATH."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    ensure_user_bin_dirs()

    for raw in USER_BIN_DIRS:
        assert os.path.expanduser(raw) not in os.environ["PATH"].split(os.pathsep)