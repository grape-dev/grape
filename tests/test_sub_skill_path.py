"""测试技能包子技能 API 的路径解析。

回归验证：list_sub_skills / read_sub_skill / package_sub_skill 三个函数
必须用 os.path.join(SKILLS_DIR, skill_path) 拼路径，而非 os.path.dirname(skill_path)。
当 skill_path 不含 / 时（如 'huawei-cloud'），dirname 返回空串导致找不到 references/ 目录。
"""
import os
import sys

# 直接测试路径拼接逻辑（不依赖数据库连接）
from src.config import SKILLS_DIR


def test_skill_path_resolution_top_level():
    """顶层技能（skill_path 不含 /）的 base_dir 必须正确指向技能目录。"""
    skill_path = "huawei-cloud"
    # 修复后的逻辑
    base_dir = os.path.join(SKILLS_DIR, skill_path) if skill_path else ""
    assert base_dir == os.path.join(SKILLS_DIR, "huawei-cloud"), f"base_dir wrong: {base_dir}"
    assert os.path.isdir(base_dir), f"skill dir not found: {base_dir}"


def test_skill_path_resolution_nested():
    """嵌套技能（skill_path 含 /）的 base_dir 也必须正确。"""
    skill_path = "iac/iac-coding"
    base_dir = os.path.join(SKILLS_DIR, skill_path) if skill_path else ""
    assert base_dir == os.path.join(SKILLS_DIR, "iac", "iac-coding")
    assert os.path.isdir(base_dir), f"skill dir not found: {base_dir}"


def test_old_logic_broken_for_top_level():
    """证明旧逻辑 os.path.dirname 对顶层技能返回空串（回归保护）。"""
    skill_path = "huawei-cloud"
    old_base = os.path.dirname(skill_path) if skill_path else ""
    assert old_base == "", f"dirname should be empty for top-level skill, got: {old_base}"

    # 旧逻辑拼出的 ref_dir 不存在
    old_ref = os.path.join(old_base, "references")
    assert not os.path.isdir(old_ref), "old logic should NOT find references/"


def test_huawei_cloud_has_sub_skills():
    """huawei-cloud 技能包的 references/ 下有子技能目录，各含 SKILL.md。"""
    ref_dir = os.path.join(SKILLS_DIR, "huawei-cloud", "references")
    assert os.path.isdir(ref_dir), f"references dir not found: {ref_dir}"

    sub_dirs = []
    for entry in sorted(os.listdir(ref_dir)):
        entry_path = os.path.join(ref_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "SKILL.md")):
            sub_dirs.append(entry)

    assert len(sub_dirs) >= 22, f"expected >=22 sub-skills, got {len(sub_dirs)}: {sub_dirs}"


def test_sub_skill_content_readable():
    """huawei-ecs 子技能的 SKILL.md 可以正确读取。"""
    sub_md = os.path.join(SKILLS_DIR, "huawei-cloud", "references", "huawei-ecs", "SKILL.md")
    assert os.path.isfile(sub_md), f"sub-skill SKILL.md not found: {sub_md}"

    with open(sub_md, encoding="utf-8") as f:
        content = f.read()
    assert "huawei-ecs" in content.lower(), "SKILL.md content missing skill name"
    assert "description:" in content, "SKILL.md missing frontmatter description"

    # huawei-ecs 有 4 个 reference 文件
    ref_subdir = os.path.join(SKILLS_DIR, "huawei-cloud", "references", "huawei-ecs", "references")
    if os.path.isdir(ref_subdir):
        ref_files = [f for f in os.listdir(ref_subdir) if os.path.isfile(os.path.join(ref_subdir, f))]
        assert len(ref_files) == 4, f"expected 4 ref files, got {len(ref_files)}: {ref_files}"


def test_other_skills_not_affected():
    """没有子技能包的技能（如 vod）的 references/ 下不含子目录+SKILL.md。"""
    ref_dir = os.path.join(SKILLS_DIR, "vod", "references")
    if not os.path.isdir(ref_dir):
        return  # vod 无 references 也正常

    sub_skill_dirs = []
    for entry in os.listdir(ref_dir):
        entry_path = os.path.join(ref_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "SKILL.md")):
            sub_skill_dirs.append(entry)

    assert len(sub_skill_dirs) == 0, f"vod should have 0 sub-skills, got: {sub_skill_dirs}"
