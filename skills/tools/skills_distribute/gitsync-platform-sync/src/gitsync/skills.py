"""Skill discovery + SKILL.md frontmatter parsing."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional

import yaml


def content_hash(skill: "Skill") -> str:
    """SHA-256 fingerprint of all files in a skill folder.

    Used for idempotency: if the fingerprint is unchanged since the last
    publish, the skill's content is identical and should NOT be re-published
    as a new version.
    """
    h = hashlib.sha256()
    files = sorted(p for p in skill.folder.rglob("*") if p.is_file())
    for p in files:
        rel = p.relative_to(skill.folder).as_posix()
        h.update(rel.encode("utf-8"))
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return h.hexdigest()


class Skill:
    def __init__(self, folder: Path, name: str, description: str,
                 relative_path: str):
        self.folder = folder
        self.name = name
        self.description = description
        self._relative_path = relative_path

    @property
    def relative_path(self) -> str:
        return self._relative_path


def _parse_frontmatter(text: str):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    raw = text[3:end]
    try:
        return yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}


def find_skills(root: Path) -> List[Skill]:
    """Recursively scan root for skill folders (dirs containing SKILL.md).

    Supports nested categories: skills/ai/maas/<skill>/SKILL.md.
    A directory containing SKILL.md is a skill; parent category dirs are not.
    """
    if not root.is_dir():
        return []
    skills = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for child in entries:
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                name = _skill_name_from_md(skill_md) or child.name
                rel = child.relative_to(root).as_posix()
                skills.append(Skill(folder=child, name=name,
                                    description=_description_from_md(skill_md),
                                    relative_path=rel))
            else:
                stack.append(child)
    return skills


def _skill_name_from_md(path: Path) -> Optional[str]:
    fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    name = fm.get("name")
    return str(name) if name else None


def is_excluded(skill_name: str, patterns: List[str]) -> bool:
    """需求3: skill 是否命中发布黑名单(精确名或 glob 通配)。

    patterns 来自 config.clawhub.exclude_skills, 支持:
      - 精确名: "huawei-cloud-terraform-generator"
      - glob:   "terraform-*" / "huawei-cloud-*-generator"
    命中任一 pattern 即视为排除。空列表恒返回 False。
    """
    import fnmatch

    if not patterns or not skill_name:
        return False
    name = skill_name.strip()
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if p == name or fnmatch.fnmatch(name, p):
            return True
    return False


def _description_from_md(path: Path) -> str:
    fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    desc = fm.get("description")
    return str(desc) if desc else ""
