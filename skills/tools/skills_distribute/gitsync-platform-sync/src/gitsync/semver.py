"""Semantic versioning helpers for the ClawHub publish flow.

Implements the "two-digit per segment" personal convention:
  1.0.0 -> 1.0.1 -> ... -> 1.0.99 -> 1.1.0 -> ... -> 1.99.99 -> 2.0.0
"""
from __future__ import annotations

import re

MAX_SEGMENT = 99

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(version: str) -> tuple:
    m = _VERSION_RE.match(version.strip())
    if not m:
        raise ValueError(f"invalid semver: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(version: str, part: str = "patch") -> str:
    """Bump a version segment with two-digit-per-segment rollover.

    part: "patch" (x.y.z -> x.y.(z+1) with rollover), "minor", "major".
    """
    major, minor, patch = parse_version(version)
    if part == "patch":
        patch += 1
        if patch > MAX_SEGMENT:
            patch = 0
            minor += 1
            if minor > MAX_SEGMENT:
                minor = 0
                major += 1
    elif part == "minor":
        minor += 1
        if minor > MAX_SEGMENT:
            minor = 0
            major += 1
        patch = 0
    elif part == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"unknown bump part: {part}")
    return f"{major}.{minor}.{patch}"


def next_patch(version: str) -> str:
    return bump(version, "patch")


def highest(versions) -> str | None:
    """Return the highest version among a list of version strings."""
    best = None
    for v in versions:
        try:
            cur = parse_version(v)
        except ValueError:
            continue
        if best is None or cur > best[1]:
            best = (v, cur)
    return best[0] if best else None
