# -*- coding: utf-8 -*-
"""Issue #564：技能包原子物化 / 完整性校验 / 官方包回退 / 降级标注 / 原子性测试。"""
import os
import shutil
import stat
import zipfile
import io

import pytest

from src.runtime import skill_materialize as sm


SKILL_MD = """---
name: demo-skill
---
# Demo Skill

Read references/runbook.md and references/extra.md before use.
"""


def _mk_files(with_refs=True):
    files = {"SKILL.md": SKILL_MD}
    if with_refs:
        files["references/runbook.md"] = "# runbook"
        files["references/extra.md"] = "# extra"
    return files


def _mk_zip(files, root="demo-skill"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, content in files.items():
            zf.writestr(f"{root}/{rel}", content)
    return buf.getvalue()


@pytest.fixture
def skills_root(tmp_path):
    d = tmp_path / ".skills"
    d.mkdir()
    return str(d)


# ── 物化成功 ──────────────────────────────────────────────

def test_materialize_files_atomic_success(skills_root):
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert ok, reason
    dst = os.path.join(skills_root, "demo-skill")
    assert os.path.isfile(os.path.join(dst, "SKILL.md"))
    assert os.path.isfile(os.path.join(dst, "references/runbook.md"))
    # tmp 目录已清理
    assert not [p for p in os.listdir(skills_root) if p.startswith(sm.TMP_PREFIX)]


def test_materialize_zip_atomic_success(skills_root):
    ok, skills, reason = sm.materialize_skill_zip_atomic(skills_root, _mk_zip(_mk_files()))
    assert ok, reason
    assert "demo-skill" in skills
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "SKILL.md"))


def test_materialize_zip_restores_unix_mode(skills_root):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("demo-skill/run.sh")
        zi.external_attr = (0o755 | stat.S_IFREG) << 16
        zf.writestr(zi, "#!/bin/sh\n")
        zf.writestr("demo-skill/SKILL.md", "# plain skill without references\n")
    ok, _skills, reason = sm.materialize_skill_zip_atomic(skills_root, buf.getvalue())
    assert ok, reason
    mode = os.stat(os.path.join(skills_root, "demo-skill", "run.sh")).st_mode
    assert mode & 0o111  # 可执行位已恢复


def test_materialize_local_dir_atomic_success(skills_root, tmp_path):
    src = tmp_path / "official" / "demo-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (src / "references").mkdir()
    (src / "references" / "runbook.md").write_text("# runbook", encoding="utf-8")
    (src / "references" / "extra.md").write_text("# extra", encoding="utf-8")
    ok, reason = sm.materialize_skill_dir_atomic(skills_root, "demo-skill", str(src))
    assert ok, reason
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "references", "extra.md"))


# ── 完整性校验（SKILL.md / references 按定义清单）─────────

def test_files_missing_skill_md_fails(skills_root):
    files = _mk_files(with_refs=True)
    files.pop("SKILL.md")
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", files)
    assert not ok
    assert "SKILL.md" in reason
    # 失败不留目标目录
    assert not os.path.isdir(os.path.join(skills_root, "demo-skill"))


def test_files_missing_references_fails(skills_root):
    # #553 场景：SKILL.md 存在但 references 缺失
    files = {"SKILL.md": SKILL_MD}
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", files)
    assert not ok
    assert "references/" in reason


def test_zip_missing_references_fails(skills_root):
    ok, skills, reason = sm.materialize_skill_zip_atomic(skills_root, _mk_zip({"SKILL.md": SKILL_MD}))
    assert not ok
    assert "references/" in reason
    assert not os.path.isdir(os.path.join(skills_root, "demo-skill"))


# ── 官方包回退 / 降级 ─────────────────────────────────────

@pytest.fixture
def official_dir(tmp_path, monkeypatch):
    d = tmp_path / "official-skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (d / "references").mkdir()
    (d / "references" / "runbook.md").write_text("# runbook", encoding="utf-8")
    (d / "references" / "extra.md").write_text("# extra", encoding="utf-8")
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(d / "SKILL.md") if name == "demo-skill" else "")
    return str(d)


def test_fallback_to_official(skills_root, official_dir):
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("files", {"SKILL.md": SKILL_MD}))
    assert res["status"] == "fallback"
    # 官方包完整落盘
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "references", "extra.md"))


def test_degraded_when_fallback_also_fails(skills_root, tmp_path, monkeypatch):
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("none", "skill zip unavailable"))
    assert res["status"] == "degraded"
    assert "skill zip unavailable" in res["detail"]
    # 不静默空注入：目标目录不存在
    assert not os.path.isdir(os.path.join(skills_root, "demo-skill"))


def test_ok_status_primary_success(skills_root):
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("files", _mk_files()))
    assert res["status"] == "ok"


# ── zip 部分成功时的目标技能校验（PR #765 审查 Required）────

def _mk_multi_zip():
    """双技能 zip：other-skill 完整、demo-skill 缺 references（校验失败）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other-skill/SKILL.md", "# no refs\n")
        zf.writestr("demo-skill/SKILL.md", SKILL_MD)  # 引用 references/ 但缺失
    return buf.getvalue()


def test_zip_partial_success_target_present_ok(skills_root):
    # 目标技能在 installed 列表 → ok
    res = sm.materialize_skill_with_fallback(
        skills_root, "other-skill", lambda: ("zip", _mk_multi_zip()))
    assert res["status"] == "ok"
    assert res["skill_dir"] == os.path.join(skills_root, "other-skill")
    assert os.path.isfile(os.path.join(skills_root, "other-skill", "SKILL.md"))


def test_zip_partial_success_target_missing_falls_back(skills_root, official_dir):
    # 目标技能校验失败 → 不能凭部分成功注入，走官方包回退
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("zip", _mk_multi_zip()))
    assert res["status"] == "fallback"
    assert "target skill 'demo-skill' not in materialized list" in res["detail"]
    # 官方包完整落盘
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "references", "extra.md"))


def test_zip_partial_success_target_missing_degraded(skills_root, monkeypatch):
    # 目标技能校验失败且无官方包 → degraded，不静默空注入
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("zip", _mk_multi_zip()))
    assert res["status"] == "degraded"
    assert "target skill 'demo-skill' not in materialized list" in res["detail"]


def test_zip_full_success_target_present(skills_root):
    res = sm.materialize_skill_with_fallback(
        skills_root, "demo-skill", lambda: ("zip", _mk_zip(_mk_files())))
    assert res["status"] == "ok"


# ── 原子性 / 幂等 ─────────────────────────────────────────

def test_atomicity_no_dirty_dir_on_rename_failure(skills_root, monkeypatch):
    # rename 阶段抛异常 → tmp 清理、目标目录不出现
    def _boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "rename", _boom)
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert not ok
    assert "disk full" in reason
    assert not [p for p in os.listdir(skills_root) if p.startswith(sm.TMP_PREFIX)]
    assert not os.path.isdir(os.path.join(skills_root, "demo-skill"))


def test_idempotent_when_target_valid(skills_root):
    ok, _ = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert ok
    # 第二次物化：目标合法 → 幂等跳过，不报错
    ok2, reason2 = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert ok2, reason2
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "SKILL.md"))


def test_empty_target_dir_replaced(skills_root):
    os.makedirs(os.path.join(skills_root, "demo-skill"))  # 历史空目录（Issue #564 现场症状）
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert ok, reason
    assert os.path.isfile(os.path.join(skills_root, "demo-skill", "SKILL.md"))


# ── direct 端 _materialize_direct_workdir 降级标注 ─────────

def test_direct_materialize_marks_degradation(skills_root, tmp_path, monkeypatch):
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    prompt = "Skill file: {{SKILL_DIR}}/demo-skill/SKILL.md"
    out = _materialize_direct_workdir(tmp_path, "gitcode:acme/repo", "issue",
                                      "demo-skill", evt, "", prompt)
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "degraded"
    # prompt 含降级说明与 DEVOPS_SKILLS_DIR 路径
    assert "[Skill Materialization Notice]" in out
    assert "DEVOPS_SKILLS_DIR" in out
    # 占位符仍被替换（路径注入与内容说明并存）
    assert "{{SKILL_DIR}}" not in out


def test_direct_materialize_ok_no_notice(skills_root, tmp_path, monkeypatch):
    import src.config as cfg
    official = tmp_path / "off" / "demo-skill"
    official.mkdir(parents=True)
    (official / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (official / "references").mkdir()
    (official / "references" / "runbook.md").write_text("# r", encoding="utf-8")
    (official / "references" / "extra.md").write_text("# e", encoding="utf-8")
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(official / "SKILL.md"))
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    out = _materialize_direct_workdir(tmp_path, "gitcode:acme/repo", "issue",
                                      "demo-skill", evt, "", "{{SKILL_DIR}}/demo-skill/SKILL.md")
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "ok"
    assert "[Skill Materialization Notice]" not in out
