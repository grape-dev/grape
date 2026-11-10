# -*- coding: utf-8 -*-
"""Issue #564：技能包原子物化 / 完整性校验 / 官方包回退 / 降级标注 / 原子性测试。"""
import os
import json
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


# ── Issue #617：按名未命中 → 按 agent_type 回退官方检查器 ──

def _mk_checker_dir(tmp_path, name="grape-skill-validator"):
    d = tmp_path / "official" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# checker skill without references\n", encoding="utf-8")
    return str(d)


def test_agent_type_fallback_success(skills_root, tmp_path, monkeypatch):
    # 按名未命中 + 官方包未找到 → agent_type 检查器回退成功，detail 含 skills detail --skill_id
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    checker = _mk_checker_dir(tmp_path)
    resolver = lambda at: ({"skill_name": "grape-skill-validator", "skill_id": 2789,
                            "skill_dir": checker} if at == "qa" else None)
    res = sm.materialize_skill_with_fallback(
        skills_root, "skill-batch-validator", lambda: ("none", "skill source not found"),
        agent_type="qa", agent_type_fallback=resolver)
    assert res["status"] == "fallback"
    assert res["skill_id"] == 2789
    assert "grape skills detail --skill_id 2789" in res["detail"]
    assert os.path.isfile(os.path.join(skills_root, "grape-skill-validator", "SKILL.md"))
    # 原错误名不落盘（不产生半写/错名目录）
    assert not os.path.isdir(os.path.join(skills_root, "skill-batch-validator"))


def test_agent_type_fallback_resolver_miss_still_degraded(skills_root, tmp_path, monkeypatch):
    # resolver 未命中（无检查器）→ 仍 degraded，不静默空注入
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    res = sm.materialize_skill_with_fallback(
        skills_root, "skill-batch-validator", lambda: ("none", "skill source not found"),
        agent_type="qa", agent_type_fallback=lambda at: None)
    assert res["status"] == "degraded"
    assert "skill source not found" in res["detail"]
    assert not os.path.isdir(os.path.join(skills_root, "skill-batch-validator"))


def test_agent_type_fallback_no_resolver_degraded(skills_root, monkeypatch):
    # 未注入 resolver（老调用方）→ 行为与旧版一致
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    res = sm.materialize_skill_with_fallback(
        skills_root, "skill-batch-validator", lambda: ("none", "skill zip unavailable"))
    assert res["status"] == "degraded"
    assert "skill zip unavailable" in res["detail"]


def test_direct_materialize_agent_type_fallback(skills_root, tmp_path, monkeypatch):
    # direct 端接线：按名未命中 → 检查器回退，prompt 提示含 --skill_id
    import src.config as cfg
    checker = _mk_checker_dir(tmp_path)
    monkeypatch.setattr(cfg, "resolve_official_skill_path",
                        lambda name: str(tmp_path / "official" / name / "SKILL.md")
                        if name == "grape-skill-validator" else "")
    import src.user.db as udb
    monkeypatch.setattr(udb, "resolve_official_checker_skill",
                        lambda at: ({"id": 2789, "skill_name": "grape-skill-validator",
                                     "skill_path": str(checker), "file_path": ""} if at == "qa"
                                    else None))
    monkeypatch.setattr(udb, "get_skill_path",
                        lambda sid: (os.path.join(checker, "SKILL.md") if sid == 2789 else ""))
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    out = _materialize_direct_workdir(tmp_path, "gitcode:acme/repo", "qa",
                                      "skill-batch-validator", evt, "",
                                      "{{SKILL_DIR}}/skill-batch-validator/SKILL.md")
    mat = evt.get("_skill_materialize") or {}
    assert mat.get("status") == "fallback"
    assert mat.get("skill_id") == 2789
    assert "grape skills detail --skill_id 2789" in out
    assert os.path.isfile(os.path.join(str(tmp_path), ".skills",
                                       "grape-skill-validator", "SKILL.md"))
    assert "{{SKILL_DIR}}" not in out


# ── Issue #607：物化失败 FALLBACK.md 降级兜底 ──────────────

def _read_fallback(skills_root, skill="demo-skill"):
    with open(os.path.join(skills_root, skill, "FALLBACK.md"),
              "r", encoding="utf-8") as f:
        return f.read()


def test_write_skill_fallback_creates_file(skills_root):
    assert sm.write_skill_fallback(skills_root, "demo-skill",
                                    "obs url expired", stage="obs_url")
    content = _read_fallback(skills_root)
    assert "demo-skill" in content
    assert "grape skills detail --skill demo-skill" in content
    assert "obs url expired" in content
    assert "obs_url" in content
    # 首行声明非 SKILL.md，防误读
    assert "NOT a SKILL.md" in content


def test_write_skill_fallback_skips_existing_skill_md(skills_root):
    ok, _ = sm.materialize_skill_files_atomic(skills_root, "demo-skill", _mk_files())
    assert ok
    # 物化成功路径不受影响：已有 SKILL.md 不写 FALLBACK.md
    assert sm.write_skill_fallback(skills_root, "demo-skill", "late failure") is False
    assert not os.path.isfile(os.path.join(skills_root, "demo-skill", "FALLBACK.md"))


def test_write_skill_fallback_empty_name(skills_root):
    assert sm.write_skill_fallback(skills_root, "", "no skill") is False


def test_write_skill_fallback_logs_structured_warning(skills_root, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="src.config"):
        sm.write_skill_fallback(skills_root, "demo-skill", "conn refused", stage="zip_b64")
    # 失败日志可检索：含 tag=skill、技能名、stage、失败原因
    txt = caplog.text
    assert "[skill]" in txt and "demo-skill" in txt
    assert "zip_b64" in txt and "conn refused" in txt


def test_write_skill_fallback_survives_bad_root():
    # 兜底自身失败仅 log.error 静默降级，不抛异常
    assert sm.write_skill_fallback("/proc/definitely/not/writable", "demo-skill",
                                    "x", stage="x") is False


def test_direct_materialize_degraded_writes_fallback(skills_root, tmp_path, monkeypatch):
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    from src.api.event_pipeline import _materialize_direct_workdir
    evt = {"_skill_id": 0, "_skill_path": ""}
    _materialize_direct_workdir(tmp_path, "gitcode:acme/repo", "issue",
                                "demo-skill", evt, "", "{{SKILL_DIR}}/demo-skill/SKILL.md")
    # direct 端 ws/.skills/ 下出现 FALLBACK.md，含技能名与权威取回指引
    content = _read_fallback(str(tmp_path / ".skills"))
    assert "demo-skill" in content
    assert "grape skills detail --skill demo-skill" in content


def test_direct_materialize_ok_no_fallback(skills_root, tmp_path, monkeypatch):
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
    _materialize_direct_workdir(tmp_path, "gitcode:acme/repo", "issue",
                                "demo-skill", evt, "", "{{SKILL_DIR}}/demo-skill/SKILL.md")
    # 物化成功路径不受影响：不出现 FALLBACK.md
    assert not os.path.isfile(str(tmp_path / ".skills" / "demo-skill" / "FALLBACK.md"))


# ── Issue #607：worker 端 _materialize_worker_skill 兜底 ──

def _mk_task(tmp_path, **kw):
    from src.runtime import agent as ra
    task = {"id": "t-607", "cwd": str(tmp_path), "skill": "demo-skill"}
    task.update(kw)
    skill_dir = os.path.join(str(tmp_path), ".skills")
    return ra, task, {}, "prompt-body", skill_dir


def test_worker_obs_fail_and_files_empty_writes_fallback(tmp_path, monkeypatch):
    # 验收①：skill_obs_url 失败 + skill_files 为空 → FALLBACK.md 存在且含取回指引
    ra, task, env, prompt, skill_dir = _mk_task(
        tmp_path, skill_obs_url="https://obs/expired", skill_files="{}")
    monkeypatch.setattr(ra, "_fetch_skill_zip", lambda url, task_id="": None)
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    out = ra._materialize_worker_skill(task, env, prompt, skill_dir)
    content = _read_fallback(skill_dir)
    assert "demo-skill" in content
    assert "grape skills detail --skill demo-skill" in content
    # 不再静默空注入 DEVOPS_SKILLS_DIR
    assert "DEVOPS_SKILLS_DIR" not in env
    # prompt 降级说明与 FALLBACK.md 取回口径一致（均为 grape skills detail）
    assert "grape skills detail" in out
    assert "[Skill Materialization Notice]" in out


def test_worker_zip_without_skill_md_writes_fallback(tmp_path, monkeypatch):
    # 验收②：zip 解压后无 SKILL.md（空 zip）→ 同样触发 FALLBACK.md
    ra, task, env, prompt, skill_dir = _mk_task(
        tmp_path, skill_obs_url="https://obs/zip", skill_files="{}")
    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("empty-dir/", "")  # 无 SKILL.md 的空目录 zip
    monkeypatch.setattr(ra, "_fetch_skill_zip",
                        lambda url, task_id="": empty_zip.getvalue())
    import src.config as cfg
    monkeypatch.setattr(cfg, "resolve_official_skill_path", lambda name: "")
    ra._materialize_worker_skill(task, env, prompt, skill_dir)
    content = _read_fallback(skill_dir)
    assert "demo-skill" in content
    assert "grape skills detail --skill demo-skill" in content


def test_worker_success_path_unchanged(tmp_path, monkeypatch):
    # 验收⑥：物化成功路径不受影响
    ra, task, env, prompt, skill_dir = _mk_task(
        tmp_path, skill_files=json.dumps({"demo-skill": _mk_files()}))
    out = ra._materialize_worker_skill(task, env, prompt, skill_dir)
    assert env.get("DEVOPS_SKILLS_DIR") == skill_dir
    assert "[Skill Materialization Notice]" not in out
    assert os.path.isfile(os.path.join(skill_dir, "demo-skill", "SKILL.md"))
    assert not os.path.isfile(os.path.join(skill_dir, "demo-skill", "FALLBACK.md"))


def test_worker_exception_branch_writes_fallback(tmp_path):
    # 外层 except（skill materialize failed）分支 → 同样写 FALLBACK.md
    ra, task, env, prompt, skill_dir = _mk_task(
        tmp_path, skill_files="{broken json")
    ra._materialize_worker_skill(task, env, prompt, skill_dir)
    content = _read_fallback(skill_dir)
    assert "demo-skill" in content
    assert "grape skills detail --skill demo-skill" in content
    assert "exception" in content


# ── Issue #1845：点文件路径归一化 + 非法相对路径安全闸 ─────

def test_zip_materialize_preserves_dot_files(skills_root):
    # T1：zip 含 .gitignore 与嵌套点目录 .grader/rules.md
    files = {"SKILL.md": "# plain skill without references\n",
             ".gitignore": "dist/\n",
             ".grader/rules.md": "# grader rules"}
    ok, skills, reason = sm.materialize_skill_zip_atomic(skills_root, _mk_zip(files))
    assert ok, reason
    assert "demo-skill" in skills
    dst = os.path.join(skills_root, "demo-skill")
    with open(os.path.join(dst, ".gitignore"), encoding="utf-8") as f:
        assert f.read() == "dist/\n"
    assert os.path.isfile(os.path.join(dst, ".grader", "rules.md"))


@pytest.mark.parametrize("key", ["./.gitignore", ".gitignore"])
def test_files_materialize_preserves_dotfile(skills_root, key):
    # T2：files 字典键带/不带 ./ 前缀两种形态，落盘均为 .gitignore
    files = {"SKILL.md": "# plain skill without references\n", key: "dist/\n"}
    ok, reason = sm.materialize_skill_files_atomic(skills_root, "demo-skill", files)
    assert ok, reason
    dst = os.path.join(skills_root, "demo-skill")
    assert os.path.isfile(os.path.join(dst, ".gitignore"))
    assert not os.path.exists(os.path.join(dst, "gitignore"))


def test_dir_materialize_preserves_dot_files(skills_root, tmp_path):
    # T3：目录物化源目录含点文件/点目录
    src = tmp_path / "official" / "demo-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# plain skill without references\n", encoding="utf-8")
    (src / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (src / ".grader").mkdir()
    (src / ".grader" / "rules.md").write_text("# grader rules", encoding="utf-8")
    ok, reason = sm.materialize_skill_dir_atomic(skills_root, "demo-skill", str(src))
    assert ok, reason
    dst = os.path.join(skills_root, "demo-skill")
    assert os.path.isfile(os.path.join(dst, ".gitignore"))
    assert os.path.isfile(os.path.join(dst, ".grader", "rules.md"))


@pytest.mark.parametrize("bad", ["../evil", "/abs/path", "C:\\x"])
def test_illegal_relative_path_rejected(skills_root, tmp_path, bad):
    # T4：非法相对路径在 validate 侧 fail closed，在 files 侧拒绝且不落盘
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "SKILL.md").write_text("# plain skill without references\n", encoding="utf-8")
    ok, missing, _extra, _vd = sm.validate_skill_dir(str(src), [bad])
    assert not ok
    assert bad in missing

    ok, reason = sm.materialize_skill_files_atomic(
        skills_root, "demo-skill",
        {bad: "x", "SKILL.md": "# plain skill without references\n"})
    assert not ok
    assert reason == f"illegal relative path: {bad}"
    assert not os.path.isdir(os.path.join(skills_root, "demo-skill"))
    assert not os.path.exists(os.path.join(tmp_path, "evil"))
    assert not os.path.exists(os.path.join(skills_root, "evil"))
    assert not [p for p in os.listdir(skills_root) if p.startswith(sm.TMP_PREFIX)]
