"""Skill 审查检测点31「Skill目录存在性检查」测试(2026-08-14 用户要求,
2026-08-16 升级为逐层父目录检查)。

语义: 对 SKILL.md 所在路径的每一层父目录做仓库存在性检查, 直接给出
逐层结论(断点在哪一层一目了然), 不写"add 属正常/请核对"类人工判断文案。
- 路径完整存在 -> ✅
- 中间层缺失(类目路径错误) -> ⚠️ 列出断点层
- 仅最后一层缺失(skill 根新增) -> ✅ 客观陈述(新增 skill 属正常, 2026-08-18)
- 查询失败(限流/异常) -> ✅ 显示但注明跳过确认(fail-open)
- 无 repo 参数 -> 跳过确认
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

SKILL_DIR = "skills/network/ops/huawei-cloud-e2e-dircheck"
MD = ("---\nname: huawei-cloud-e2e-dircheck\ndescription: dir check test\n---\n"
      "# 概述\n测试\n# 前置条件\n无\n")


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _review(mod, repo="g30074593/skillspackage"):
    files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/SKILL.md": MD}
    return mod._skill_review(SKILL_DIR, files, contents, repo)


def _item31(results):
    return [r for r in results if r[0] == 31][0]


def _mock_dir_exists(mod, existing_dirs):
    """mock _repo_dir_exists: 目录在 existing_dirs 中 -> True, 否则 False。"""
    return mock.patch.object(
        mod, "_repo_dir_exists",
        side_effect=lambda repo, p: p in existing_dirs)


def test_path_fully_exists():
    mod = _load_handler()
    existing = {"skills", "skills/network", "skills/network/ops",
                "skills/network/ops/huawei-cloud-e2e-dircheck"}
    with _mock_dir_exists(mod, existing):
        _, results = _review(mod)
    _, _, sev, res, note = _item31(results)
    assert sev == "🟡"
    assert res == "✅", f"路径完整存在应通过, 实际 {res}: {note}"
    assert "路径完整存在" in note
    assert "skills✅" in note and "e2e-dircheck✅" in note


def test_only_skill_root_missing_add():
    # add 新 skill: 父路径 3 层都在, 仅 skill 根缺失 -> ✅ 客观陈述
    # (2026-08-18 用户要求: skill 目录不应被扫出不存在 —— 新增 skill 在
    # master 上不存在是正常预期, 不标 ⚠️ 误导)
    mod = _load_handler()
    existing = {"skills", "skills/network", "skills/network/ops"}
    with _mock_dir_exists(mod, existing):
        _, results = _review(mod)
    _, _, sev, res, note = _item31(results)
    assert sev == "🟡"
    assert res == "✅", f"skill 根缺失(新增)应通过, 实际 {res}: {note}"
    assert "skill 根为新增" in note
    assert "e2e-dircheck❌" in note
    assert "路径断裂" not in note, note  # 不再误标断裂
    # 不应出现需人工判断的文案
    assert "请核对" not in note


def test_middle_layer_missing():
    # 中间层缺失(类目路径错误): skills/network 不存在 -> 断点早暴露
    mod = _load_handler()
    existing = {"skills"}
    with _mock_dir_exists(mod, existing):
        _, results = _review(mod)
    _, _, sev, res, note = _item31(results)
    assert sev == "🟡"
    assert res == "⚠️", f"中间层缺失应提示, 实际 {res}: {note}"
    assert "路径断裂" in note
    assert "network❌" in note
    assert "仓库无 `skills/network` 目录" in note


def test_query_failure_fail_open():
    mod = _load_handler()
    # 查询失败(_repo_dir_exists 内部捕获返回 None) -> fail-open 跳过确认
    with mock.patch.object(mod, "_repo_dir_exists",
                           side_effect=lambda repo, p: None):
        _, results = _review(mod)
    _, _, sev, res, note = _item31(results)
    assert sev == "🟡"
    assert res == "✅", f"查询失败应 fail-open 不误报, 实际 {res}: {note}"
    assert "跳过确认" in note


def test_no_repo_skips():
    mod = _load_handler()
    _, results = _review(mod, repo=None)
    _, _, sev, res, note = _item31(results)
    assert sev == "🟡"
    assert res == "✅" and "跳过确认" in note, f"无 repo 应跳过确认, 实际 {res}: {note}"


def test_checklist_has_31():
    mod = _load_handler()
    items = dict((i, t) for i, t, _ in mod.SKILL_CHECKLIST)
    assert 31 in items, "SKILL_CHECKLIST 应含检测点31"
    assert items[31] == "Skill目录存在性检查"
