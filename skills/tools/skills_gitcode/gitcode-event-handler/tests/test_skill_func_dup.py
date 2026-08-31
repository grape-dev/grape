"""检测点33「与仓库已有skill功能重复」测试(2026-08-17 用户要求)。

语义: 仅对**新增 skill**(仓库无此目录)判断 —— PR 提交的新 skill 功能是否
与仓库已有 skill 语义重复。复用 _llm_skills_covers_issue(LLM 语义判重,
fail-open)。命中 -> 🟡 提示列相似 skill 名(不阻断); update 场景(仓库已有
同名目录)跳过; 查询失败/无上下文跳过确认。
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

SKILL_DIR = "skills/network/ops/huawei-cloud-vpc-peering-list"
MD = ("---\nname: huawei-cloud-vpc-peering-list\ndescription: 查询VPC对等连接列表\n---\n"
      "# 概述\n测试\n# 前置条件\n无\n")


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _review(mod, repo="hd-skills-pre/skills-pre", pr_number=13,
            dir_exists=False, llm_result=(False, None, "未发现")):
    files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/SKILL.md": MD}
    with mock.patch.object(mod, "_repo_dir_exists", return_value=dir_exists), \
         mock.patch.object(mod, "_llm_skills_covers_issue",
                           return_value=llm_result):
        return mod._skill_review(SKILL_DIR, files, contents, repo, pr_number)


def _item33(results):
    return [r for r in results if r[0] == 33][0]


# ===== 新增 skill 且 LLM 判重复 -> 🟡 提示列 skill 名(不阻断) =====
def test_new_skill_dup_warns():
    mod = _load_handler()
    _, results = _review(mod, dir_exists=False,
                         llm_result=(True, "huawei-cloud-vpc-list", "功能相同"))
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "⚠️", f"功能重复应提示, 实际 {res}: {note}"
    assert "huawei-cloud-vpc-list" in note, note
    # 2026-08-18 用户拍板: 判重类输出只列候选供人工比对, 不写结论性判定
    assert "疑似" in note and "请人工确认" in note, note
    assert "功能重复的 skill" not in note, note  # 旧结论性文案已废弃


# ===== 新增 skill 且 LLM 判不重复 -> ✅ =====
def test_new_skill_no_dup_pass():
    mod = _load_handler()
    _, results = _review(mod, dir_exists=False,
                         llm_result=(False, None, "未发现"))
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅", f"无重复应通过, 实际 {res}: {note}"
    assert "未发现功能重复" in note, note


# ===== update 场景(仓库已有同名目录) -> 跳过, 不判重 =====
def test_update_skill_skips_dup_check():
    mod = _load_handler()
    _, results = _review(mod, dir_exists=True)
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅"
    assert "迭代更新" in note, f"update 应跳过判重, 实际 {note}"


# ===== 仓库目录探测失败 -> fail-open 跳过确认 =====
def test_dir_probe_fail_fail_open():
    mod = _load_handler()
    _, results = _review(mod, dir_exists=None)
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅" and "跳过确认" in note, f"实际 {res}: {note}"


# ===== LLM 判重失败(返回 False+失败原因) -> 不误报, 视为未发现 =====
def test_llm_fail_no_warn():
    mod = _load_handler()
    _, results = _review(mod, dir_exists=False,
                         llm_result=(False, None, "LLM 调用失败: 429"))
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅", f"LLM 失败不应误报重复, 实际 {res}: {note}"


# ===== 无仓库上下文 -> 跳过确认 =====
def test_no_repo_skips():
    mod = _load_handler()
    _, results = _review(mod, repo=None, pr_number=None)
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅" and "无仓库上下文" in note, f"实际 {res}: {note}"


# ===== 新增 skill 但无 SKILL.md(仓库无目录, 描述为空) -> 跳过判重 =====
def test_new_skill_no_desc_skips():
    mod = _load_handler()
    # files 无 SKILL.md, 仓库无目录 -> md_content 为空 -> _func_desc 为空
    files = [{"filename": f"{SKILL_DIR}/scripts/run.sh", "patch": "", "raw_url": ""}]
    contents = {}
    with mock.patch.object(mod, "_repo_dir_exists", return_value=False), \
         mock.patch.object(mod, "_llm_skills_covers_issue",
                           side_effect=AssertionError("描述为空不应调 LLM")):
        _, results = mod._skill_review(SKILL_DIR, files, contents, "r/r", 100)
    _, _, sev, res, note = _item33(results)
    assert sev == "🟡"
    assert res == "✅" and "跳过确认" in note, f"描述为空应跳过, 实际 {res}: {note}"


# ===== SKILL_CHECKLIST 含 33 =====
def test_checklist_has_33():
    mod = _load_handler()
    items = dict((i, t) for i, t, _ in mod.SKILL_CHECKLIST)
    assert 33 in items, "SKILL_CHECKLIST 应含检测点33"
    assert items[33] == "与仓库已有skill功能重复"
