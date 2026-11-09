# -*- coding: utf-8 -*-
"""Issue #1070：跨技能前缀引用物化校验误判修复回归。"""
import logging
import os

import pytest

from src.runtime import skill_materialize as sm


SKILL_MD_CROSS = """---
name: grape-daily-failure-analysis
---
# Daily Failure Analysis

Read grape-platform-ops/references/ops-runbook.md for platform ops.
Local guide: references/local-runbook.md
Also see docs/references/architecture.md (path-segment prefix, not a skill ref).
"""

SKILL_MD_OWN = """---
name: demo-skill
---
# Demo

See references/runbook.md.
"""


def _write_skill(root, name, files):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return d


@pytest.fixture
def skills_root(tmp_path):
    d = tmp_path / ".skills"
    d.mkdir()
    return str(d)


# ── 解析层：跨技能前缀引用被排除 ──────────────────────────

def test_parse_excludes_cross_skill_prefix_refs(tmp_path):
    d = _write_skill(str(tmp_path), "grape-daily-failure-analysis",
                     {"SKILL.md": SKILL_MD_CROSS})
    refs = sm.parse_skill_md_references(os.path.join(d, "SKILL.md"))
    # 本技能自有引用保留
    assert refs == ["references/local-runbook.md"]


def test_parse_keeps_own_prefix_refs(tmp_path):
    # 前缀 == 本技能自身目录名 → 视为自有引用（repo 视角写法）
    d = _write_skill(str(tmp_path), "demo-skill", {
        "SKILL.md": "See demo-skill/references/runbook.md\n",
        "references/runbook.md": "# r",
    })
    refs = sm.parse_skill_md_references(os.path.join(d, "SKILL.md"))
    assert refs == ["references/runbook.md"]


def test_parse_cross_skill_skip_logs_info_once(tmp_path, caplog):
    d = _write_skill(str(tmp_path), "demo-skill", {"SKILL.md": SKILL_MD_CROSS})
    md = os.path.join(d, "SKILL.md")
    sm._CROSS_SKILL_LOGGED.clear()
    with caplog.at_level(logging.INFO):
        sm.parse_skill_md_references(md)
        sm.parse_skill_md_references(md)
    # 一次性 INFO：同进程同技能只记一次，且含 skipped N cross-skill refs 口径
    hits = [r for r in caplog.records if "cross-skill refs" in r.getMessage()]
    assert len(hits) == 1
    assert "skipped 2 cross-skill refs for skill demo-skill" in hits[0].getMessage()


# ── 校验层：跨技能引用物化成功，缺自有文件仍判败 ─────────

def test_materialize_zip_with_cross_skill_refs_ok(skills_root):
    # 验收①：zip 只含本技能自有内容 + SKILL.md 引用其他技能 references → 物化成功
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("grape-daily-failure-analysis/SKILL.md", SKILL_MD_CROSS)
        zf.writestr("grape-daily-failure-analysis/references/local-runbook.md", "# local")
    ok, skills, reason = sm.materialize_skill_zip_atomic(skills_root, buf.getvalue())
    assert ok, reason
    assert "grape-daily-failure-analysis" in skills
    dst = os.path.join(skills_root, "grape-daily-failure-analysis")
    assert os.path.isfile(os.path.join(dst, "references", "local-runbook.md"))


def test_materialize_missing_own_reference_still_fails(skills_root):
    # 回归保障：缺自有 references 文件仍判失败、走降级链路
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("grape-daily-failure-analysis/SKILL.md", SKILL_MD_CROSS)
        # 故意缺 references/local-runbook.md
    ok, _skills, reason = sm.materialize_skill_zip_atomic(skills_root, buf.getvalue())
    assert not ok
    assert "references/local-runbook.md" in reason


def test_materialize_missing_skill_md_still_fails(skills_root):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo-skill/references/runbook.md", "# r")
    ok, _skills, reason = sm.materialize_skill_zip_atomic(skills_root, buf.getvalue())
    assert not ok
    assert "SKILL.md" in reason


def test_materialize_files_with_cross_skill_refs_ok(skills_root):
    files = {
        "SKILL.md": SKILL_MD_CROSS,
        "references/local-runbook.md": "# local",
    }
    ok, reason = sm.materialize_skill_files_atomic(
        skills_root, "grape-daily-failure-analysis", files)
    assert ok, reason


def test_validate_skill_dir_cross_skill_refs_not_missing(skills_root):
    d = _write_skill(skills_root, "grape-daily-failure-analysis", {
        "SKILL.md": SKILL_MD_CROSS,
        "references/local-runbook.md": "# local",
    })
    ok, missing, _extra, _vd = sm.validate_skill_dir(d)
    assert ok, missing
    assert missing == []


def test_cross_skill_prefix_extractor():
    assert sm._extract_cross_skill_prefix(
        "grape-platform-ops/references/x.md", "demo") == "grape-platform-ops"
    assert sm._extract_cross_skill_prefix(
        "demo/references/x.md", "demo") == ""
    assert sm._extract_cross_skill_prefix("references/x.md", "demo") == ""
    assert sm._extract_cross_skill_prefix(
        "a/b/references/x.md", "demo") == "b"
