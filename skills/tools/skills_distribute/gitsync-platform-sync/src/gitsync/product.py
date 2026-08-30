"""Product JSON: written by sync/publish, consumed by db-update.

A single fixed-path file (latest.json) is overwritten each run so db-update
reads the most recent result.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional


def write_product(product_dir: str, data: dict) -> str:
    """Atomically write the product file; returns its path."""
    os.makedirs(product_dir, exist_ok=True)
    path = os.path.join(product_dir, "latest.json")
    fd, tmp = tempfile.mkstemp(dir=product_dir, prefix=".product.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_product(product_dir: str) -> Optional[dict]:
    path = os.path.join(product_dir, "latest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def skill_url_for_target(target: str, repo_url: str, relative_path: str, branch: str) -> str:
    """Build a skill-level URL for a target platform.

    Format: <repo_base>/tree/<branch>/<relative_path>
    """
    base = repo_url.rstrip("/")
    # convert SSH form git@host:org/repo.git -> https://host/org/repo
    if "@" in base and ":" in base and "://" not in base:
        userhost, _, path = base.partition(":")
        host = userhost.rsplit("@", 1)[-1]
        base = f"https://{host}/{path.lstrip('/')}"
    if base.endswith(".git"):
        base = base[:-4]
    return f"{base}/tree/{branch}/{relative_path}"
