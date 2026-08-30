"""Skill 审查检测点28「文件数量限制」改造测试(2026-08-16 用户要求)。

改造: 校验"PR 合入后 skill 目录的文件总数" = 仓库现有文件 ∪ PR 变更文件
(去重), 阈值 40。旧逻辑只统计 PR 变更文件数(阈值 30)。
- 仓库清单拉取成功 -> 合并计算
- 仓库清单查询失败(限流/异常) -> fail-open 回退"只统计 PR 变更文件"
- 无 repo 上下文 -> 回退旧逻辑
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

SKILL_DIR = "skills/storage/sfsturbo/huawei-cloud-sfsturbo-list"
MD = ("---\nname: huawei-cloud-sfsturbo-list\ndescription: sfsturbo list test\n---\n"
      "# 概述\n测试\n# 前置条件\n无\n")


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _review(mod, files, repo="hd-skills-pre/skills-pre", pr_number=13):
    contents = {f["filename"]: "x" for f in files}
    contents[f"{SKILL_DIR}/SKILL.md"] = MD
    return mod._skill_review(SKILL_DIR, files, contents, repo, pr_number)


def _item28(results):
    return [r for r in results if r[0] == 28][0]


# ===== 合入后总数 = 仓库现有 ∪ PR 变更, 去重 =====
def test_merge_count_existing_plus_pr():
    mod = _load_handler()
    # 仓库已有 14 个文件, PR 修改其中 1 个(已存在, 去重) -> 合入后 14
    existing = {f"{SKILL_DIR}/{n}" for n in
                ("SKILL.md", ".markdownlint.json",
                 "scripts/list_sfsturbo_shares.py", "scripts/skill_quality_sdk.py",
                 "scripts/test-cli-commands.sh",
                 "references/acceptance-criteria.md", "references/cli-installation-guide.md",
                 "references/dataflow-diagram.md", "references/iam-policies.md",
                 "references/verification-method.md", "templates/test-vars.json")}
    pr_files = [{"filename": f"{SKILL_DIR}/scripts/list_sfsturbo_shares.py",
                 "patch": "", "raw_url": ""}]
    with mock.patch.object(mod, "_repo_dir_files", return_value=existing):
        _, results = _review(mod, pr_files)
    _, _, sev, res, note = _item28(results)
    assert sev == "🟡"
    assert res == "✅", f"11 个文件应通过(≤40), 实际 {res}: {note}"
    assert "11 个文件" in note, note
    assert "仓库 11" in note, f"说明应含仓库现有数, 实际 {note}"


# ===== PR 新增文件并入仓库清单 =====
def test_merge_count_new_files_added():
    mod = _load_handler()
    existing = {f"{SKILL_DIR}/SKILL.md", f"{SKILL_DIR}/scripts/run.sh"}
    # PR 新增 3 个文件(不在仓库) -> 合入后 2+3=5
    pr_files = [
        {"filename": f"{SKILL_DIR}/scripts/new1.py", "patch": "", "raw_url": ""},
        {"filename": f"{SKILL_DIR}/scripts/new2.py", "patch": "", "raw_url": ""},
        {"filename": f"{SKILL_DIR}/references/new-doc.md", "patch": "", "raw_url": ""},
    ]
    with mock.patch.object(mod, "_repo_dir_files", return_value=existing):
        _, results = _review(mod, pr_files)
    _, _, sev, res, note = _item28(results)
    assert res == "✅"
    assert "5 个文件" in note, f"合入后应为 5, 实际 {note}"
    assert "PR 新增 3" in note, note


# ===== 合入后超过 40 -> ⚠️ 提示不阻断 =====
def test_merge_count_over_40_warns():
    mod = _load_handler()
    existing = {f"{SKILL_DIR}/f{i:03d}.py" for i in range(45)}  # 仓库 45 个
    pr_files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    with mock.patch.object(mod, "_repo_dir_files", return_value=existing):
        _, results = _review(mod, pr_files)
    _, _, sev, res, note = _item28(results)
    assert sev == "🟡"
    assert res == "⚠️", f"46 个文件应提示, 实际 {res}: {note}"
    assert "46 个文件" in note, note


# ===== 仓库清单查询失败 -> fail-open 回退 PR 变更数 =====
def test_merge_query_fail_fallback():
    mod = _load_handler()
    pr_files = [
        {"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""},
        {"filename": f"{SKILL_DIR}/scripts/a.py", "patch": "", "raw_url": ""},
    ]
    with mock.patch.object(mod, "_repo_dir_files", return_value=None):
        _, results = _review(mod, pr_files)
    _, _, sev, res, note = _item28(results)
    assert sev == "🟡"
    assert "2 个文件" in note, f"回退应统计 PR 变更数, 实际 {note}"
    assert "回退" in note, note


# ===== 无 repo -> 回退 PR 变更数 =====
def test_merge_no_repo_fallback():
    mod = _load_handler()
    pr_files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    _, results = _review(mod, pr_files, repo=None)
    _, _, sev, res, note = _item28(results)
    assert sev == "🟡"
    assert "1 个文件" in note, f"无 repo 应回退 PR 变更数, 实际 {note}"


# ===== _repo_dir_files 单测: 递归收集文件 =====
def test_repo_dir_files_recursive():
    mod = _load_handler()
    root = [
        {"type": "file", "name": "SKILL.md"},
        {"type": "dir", "name": "scripts"},
    ]
    scripts = [
        {"type": "file", "name": "run.sh"},
        {"type": "dir", "name": "lib"},
    ]
    lib = [{"type": "file", "name": "util.py"}]
    def fake_get(url):
        if url.startswith(f"/repos/r/r/contents/{SKILL_DIR}?"):
            return root
        if url.startswith(f"/repos/r/r/contents/{SKILL_DIR}/scripts?"):
            return scripts
        if url.startswith(f"/repos/r/r/contents/{SKILL_DIR}/scripts/lib?"):
            return lib
        return []
    with mock.patch.object(mod, "gitcode_get", side_effect=fake_get):
        files = mod._repo_dir_files("r/r", SKILL_DIR)
    assert files == {f"{SKILL_DIR}/SKILL.md", f"{SKILL_DIR}/scripts/run.sh",
                     f"{SKILL_DIR}/scripts/lib/util.py"}, f"实际 {files}"


# ===== _repo_dir_files 单测: 查询失败返回 None =====
def test_repo_dir_files_fail_none():
    mod = _load_handler()
    with mock.patch.object(mod, "gitcode_get", side_effect=RuntimeError("429")):
        assert mod._repo_dir_files("r/r", SKILL_DIR) is None
