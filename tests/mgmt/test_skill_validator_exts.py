"""技能上传扩展名校验（Issue #2674：.ps1 文本脚本白名单遗漏）。

覆盖三组断言：
1. `.ps1` 文本脚本放行（validate_skill 返回空错误列表）；
2. 二进制可执行文件（.exe/.bat/.dll）仍被拒绝；
3. 未知扩展名的兜底报错包含动态生成的允许列表且含 `.ps1`。
"""
from src.domains.mgmt.user.validator import (
    ALLOWED_REF_EXTS,
    SCRIPT_EXTS,
    validate_skill,
)

SKILL_NAME = "test-skill"
SKILL_MD = (
    "---\n"
    f"name: {SKILL_NAME}\n"
    "description: 扩展名校验测试技能\n"
    "---\n"
    "# 测试技能\n"
)


def _make_skill(tmp_path, extra_files=()):
    skill_dir = tmp_path / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    for name in extra_files:
        target = skill_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env pwsh\nWrite-Host hi\n", encoding="utf-8")
    return skill_dir


def test_ps1_script_is_allowed(tmp_path):
    """含 SKILL.md + build.ps1 的技能目录通过校验（无错误）。"""
    skill_dir = _make_skill(tmp_path, extra_files=["build.ps1", "scripts/task.ps1"])
    assert validate_skill(str(skill_dir), SKILL_NAME) == []


def test_ps1_in_script_exts():
    """.ps1 属于文本脚本白名单，且不在任何禁止集合中。"""
    assert ".ps1" in SCRIPT_EXTS


def test_binary_executables_still_rejected(tmp_path):
    """.exe/.bat/.dll 仍被拒绝，命中「不允许上传可执行文件」。"""
    for ext in (".exe", ".bat", ".dll"):
        skill_dir = _make_skill(tmp_path / ext.strip("."), extra_files=[f"bin/payload{ext}"])
        errors = validate_skill(str(skill_dir), SKILL_NAME)
        assert any("不允许上传可执行文件" in e for e in errors), (ext, errors)


def test_unknown_ext_error_lists_allowed_and_includes_ps1(tmp_path):
    """未知扩展名报错包含允许列表，且列表由常量动态生成、含 .ps1。"""
    skill_dir = _make_skill(tmp_path, extra_files=["data/archive.xyz"])
    errors = validate_skill(str(skill_dir), SKILL_NAME)
    assert len(errors) == 1, errors
    message = errors[0]
    assert "不支持的文件类型" in message
    assert ".ps1" in message
    for ext in sorted(ALLOWED_REF_EXTS | SCRIPT_EXTS):
        assert ext in message, (ext, message)
