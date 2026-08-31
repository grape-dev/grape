"""skill 目录提取测试(2026-08-18 用户要求, PR #23 实测误报)。

PR #23: huawei-cloud-skill-analyzer 内部带模板 fixture-skill(含 SKILL.md),
原实现把嵌套 SKILL.md 当独立 skill 根 -> 检测点4 误报"PR 同时变更 2 个
skill 目录"。修复: 嵌套 SKILL.md(父根的子路径)不是独立根, 归父根。
"""
import importlib.util

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk(files):
    return [{"filename": fn, "status": st} for fn, st in files]


def test_nested_template_skill_md_not_separate_root():
    # PR #23 场景: 父 skill 内部模板 fixture-skill 含 SKILL.md -> 归父根
    mod = _load_handler()
    files = _mk([
        ("skills/devtools/common/huawei-cloud-skill-analyzer/SKILL.md", "added"),
        ("skills/devtools/common/huawei-cloud-skill-analyzer/scripts/analyze.py", "added"),
        ("skills/devtools/common/huawei-cloud-skill-analyzer/templates/fixture-skill/SKILL.md", "added"),
        ("skills/devtools/common/huawei-cloud-skill-analyzer/templates/fixture-skill/scripts/x.py", "added"),
    ])
    dirs = mod._extract_skill_dirs(files)
    assert len(dirs) == 1, f"嵌套模板不应成独立根: {list(dirs.keys())}"
    root = "skills/devtools/common/huawei-cloud-skill-analyzer"
    assert root in dirs
    assert len(dirs[root]) == 4, "模板文件应全部归父根"


def test_two_independent_skills_still_two_roots():
    mod = _load_handler()
    files = _mk([
        ("skills/network/ops/huawei-cloud-vpc-list/SKILL.md", "added"),
        ("skills/storage/rds/huawei-cloud-rds-list/SKILL.md", "added"),
    ])
    dirs = mod._extract_skill_dirs(files)
    assert len(dirs) == 2, list(dirs.keys())


def test_scripts_only_files_group_to_skill_root():
    # 无 SKILL.md 的脚本变更 -> 按 huawei-cloud-* 目录归属(旧行为保持)
    mod = _load_handler()
    files = _mk([
        ("skills/network/ops/huawei-cloud-vpc-list/scripts/list.py", "modified"),
    ])
    dirs = mod._extract_skill_dirs(files)
    assert list(dirs.keys()) == ["skills/network/ops/huawei-cloud-vpc-list"], dirs


def test_nested_skill_root_under_another_skill():
    # 真嵌套场景: 父 skill 下挂另一个完整独立 skill? 实际生态不存在
    # (模板/样例才有嵌套 SKILL.md) —— 嵌套一律归父根, 不产生第 2 个根
    mod = _load_handler()
    files = _mk([
        ("skills/a/b/huawei-cloud-parent/SKILL.md", "added"),
        ("skills/a/b/huawei-cloud-parent/references/x.md", "added"),
    ])
    dirs = mod._extract_skill_dirs(files)
    assert len(dirs) == 1
    assert "skills/a/b/huawei-cloud-parent" in dirs
