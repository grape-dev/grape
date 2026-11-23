# -*- coding: utf-8 -*-
"""Issue #2118: 组织技能随仓库分发在 .grape/skills/org/，官方内置包解析必须命中。

复审 Critical：三个组织技能位于按 agent_type 分组的 org 子目录，若
`_OFFICIAL_AGENT_TYPES` 未登记 org，`resolve_official_skill_path` 在 sid=0
时返回空串 → 物化降级并要求 `grape skills detail`（未注册则 404）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ORG_SKILLS = ("org-intake-cli", "org-dispatch-cli", "org-aggregate-cli")
ROOT = Path(__file__).resolve().parents[2]


def test_official_agent_types_covers_org_directory():
    """org 必须登记在官方 agent_type 目录清单内，否则扫描阶段跳过该目录。"""
    import src.kernel.config as cfg

    assert "org" in cfg._OFFICIAL_AGENT_TYPES


@pytest.mark.parametrize("skill", ORG_SKILLS)
def test_resolve_org_skill_hits_repo_bundle(skill):
    """真实仓库布局：解析结果必须指向 .grape/skills/org/<skill>/SKILL.md。"""
    from src.kernel.config import resolve_official_skill_path

    resolved = resolve_official_skill_path(skill)
    assert resolved, f"{skill} 官方内置包未解析到"
    assert Path(resolved).is_file()
    expected = ROOT / ".grape" / "skills" / "org" / skill / "SKILL.md"
    assert Path(resolved) == expected


def test_resolve_scans_org_group_only(tmp_path, monkeypatch):
    """扫描按 <agent_type>/<skill> 两层匹配：org 命中，custom/repo 运行时目录不命中。"""
    import src.kernel.config as cfg

    skills_root = tmp_path / "skills"
    for rel in (
        "org/demo-org/SKILL.md",
        "custom/demo-org/SKILL.md",
        "repo/demo-repo/SKILL.md",
    ):
        target = skills_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# demo\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "SKILLS_DIR", str(skills_root))
    # 隔离本机 configs/skill-description.json，确保走目录扫描分支
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path / "configs"))

    assert Path(cfg.resolve_official_skill_path("demo-org")) == (
        skills_root / "org" / "demo-org" / "SKILL.md"
    )
    # 运行时目录不进官方包解析（避免把用户/仓库技能误判为内置技能）
    assert cfg.resolve_official_skill_path("demo-repo") == ""


@pytest.mark.parametrize("skill", ORG_SKILLS)
def test_materialize_fallback_uses_org_bundle(skill, tmp_path, monkeypatch):
    """主渠道缺失时，物化必须能从 org 官方内置包回退（非 degraded）。"""
    from src.domains.runtime.engine import skill_materialize as sm

    skills_root = tmp_path / ".skills"
    skills_root.mkdir()
    res = sm.materialize_skill_with_fallback(
        str(skills_root), skill, lambda: ("none", "skill zip unavailable"))
    assert res["status"] == "fallback", res
    assert os.path.isfile(os.path.join(str(skills_root), skill, "SKILL.md"))
    refs = os.listdir(os.path.join(str(skills_root), skill, "references"))
    assert refs, "官方包 references 必须完整落盘"


@pytest.mark.parametrize("skill", ORG_SKILLS)
def test_verify_materializable_accepts_org_bundle_without_db_row(skill, monkeypatch):
    """sid=0（平台未注册）时，官方内置包兜底应放行派发，不要求 skills detail。"""
    from src.domains.runtime.engine.service import verify_skill_materializable
    import src.domains.mgmt.user.db as user_db

    monkeypatch.setattr(user_db, "resolve_skill_id", lambda *a, **k: 0)
    ok, reason, sid = verify_skill_materializable(skill, skill_id=0)
    assert ok is True, reason
    assert reason == "official skill bundle ok"
    assert sid == 0