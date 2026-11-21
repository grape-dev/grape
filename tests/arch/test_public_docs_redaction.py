"""脱敏红线棘轮（R10 / S17）。

只扫**本分支变更的文件**：基线历史里的真实账号/库名不归本 PR 负责（登记为既有），
但本分支新增或修改的文件里不允许再出现个人路径、真实账号、真实库名、备份实路径。

没有 git 或找不到基线 ref 时跳过（CI 的 shallow clone 场景），不制造假红。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASES = ("grape-dev/arch-weekly-11", "arch-weekly-11", "origin/arch-weekly-11")

FORBIDDEN = {
    "个人账号名": "yang-aiwu-new",
    "个人署名": "liufei_cloud",
    "平台账号": "gcw_3FZApeLT",
    "真实库名(mgmt)": "grape_dev_mgmt",
    "真实库名(insight)": "grape_dev_insight",
    "个人目录": "/home/liufei",
    "备份实路径": "/root/backup-rc",
}
SCOPES = ("docs/", "scripts/", "tests/", "src/")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=60)


def _changed_files() -> list[str]:
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git work tree")
    base = ""
    for cand in BASES:
        if _git("rev-parse", "--verify", "--quiet", cand).returncode == 0:
            base = cand
            break
    if not base:
        pytest.skip("baseline ref not available (shallow clone)")
    mb = _git("merge-base", base, "HEAD")
    if mb.returncode != 0:
        pytest.skip("merge-base unavailable")
    out = _git("diff", "--name-only", f"{mb.stdout.strip()}...HEAD")
    if out.returncode != 0:
        pytest.skip("diff unavailable")
    return [l for l in out.stdout.splitlines() if l.strip()]


def test_s17_changed_files_carry_no_real_identifiers():
    files = _changed_files()
    assert files, "diff 为空——扫描口径坏了"
    hits = []
    for rel in files:
        if not rel.startswith(SCOPES):
            continue
        f = ROOT / rel
        if not f.exists() or f.suffix in {".pyc"}:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, needle in FORBIDDEN.items():
            if needle in text:
                hits.append(f"{rel}: {label} ({needle})")
    assert not hits, "本分支变更文件仍含真实标识：\n" + "\n".join(hits[:20])
