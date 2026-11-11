"""Expose user-local CLI install directories to systemd-managed processes.

Agent CLIs such as ``opencode`` and ``hermes`` are usually installed under
``~/.local/bin`` or ``~/.local/npm-global/bin``. The systemd units only export
``venv/bin`` plus the standard system paths, so ``shutil.which("hermes")``
returns ``None`` inside the service and web chat fails with
``hermes ACP CLI not found in PATH`` while the same binary works in an
interactive shell.

``ensure_user_bin_dirs()`` is called once during app construction: it prepends
every existing user-local bin directory to ``os.environ["PATH"]`` so that both
``shutil.which()`` lookups and child processes spawned by the agent executor
see the same CLIs a login shell would see. The operation is idempotent.
"""
from __future__ import annotations

import os

# Order matters: first match wins, so the user's own installs come first.
USER_BIN_DIRS = (
    "~/.local/bin",
    "~/.local/npm-global/bin",
    "~/.opencode/bin",
)


def ensure_user_bin_dirs() -> list[str]:
    """Prepend existing user-local bin directories to ``PATH``.

    Returns the directories that were newly added (empty on repeat calls).
    """
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []

    added: list[str] = []
    for raw in USER_BIN_DIRS:
        directory = os.path.expanduser(raw)
        if os.path.isdir(directory) and directory not in parts:
            parts.insert(0, directory)
            added.append(directory)

    if added:
        os.environ["PATH"] = os.pathsep.join(parts)
    return added