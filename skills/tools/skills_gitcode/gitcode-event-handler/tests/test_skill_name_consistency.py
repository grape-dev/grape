"""Skill 审查检测点6「name字段与目录名一致」测试(2026-08-18 用户自查要求)。

原实现只查 name 字段存在性, name≠目录名仍 ✅(PR #15 实测 name=
huawei-cloud-billing-scout 而目录=huawei-cloud-billing-scout2, 判重/引用
按 name 走会错位)。修复后: name 缺失或与目录名不一致 -> ❌ 并给出改法。
"""
import importlib.util

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"
SKILL_DIR = "skills/bss/billing/huawei-cloud-billing-scout2"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _review(mod, md):
    files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/SKILL.md": md}
    return mod._skill_review(SKILL_DIR, files, contents, repo="g30074593/skillspackage")


def _item6(results):
    return [r for r in results if r[0] == 6][0]


def test_name_matches_dirname_ok():
    mod = _load_handler()
    md = "---\nname: huawei-cloud-billing-scout2\ndescription: billing query\n---\n"
    _, res = _review(mod, md)
    idx, text, sev, mark, note = _item6(res)
    assert mark == "✅", (mark, note)
    assert "与目录名一致" in note


def test_name_mismatch_fails_with_fix_hint():
    # PR #15 实测场景: name 少了 2
    mod = _load_handler()
    md = "---\nname: huawei-cloud-billing-scout\ndescription: billing query\n---\n"
    _, res = _review(mod, md)
    idx, text, sev, mark, note = _item6(res)
    assert mark == "❌", (mark, note)
    assert "huawei-cloud-billing-scout2" in note  # 给出目标名
    assert "请将 name 改为" in note  # 具体改法


def test_name_missing_fails():
    mod = _load_handler()
    md = "---\ndescription: billing query\n---\n"
    _, res = _review(mod, md)
    idx, text, sev, mark, note = _item6(res)
    assert mark == "❌", (mark, note)
    assert "缺失" in note


def test_name_case_underscore_normalized():
    # 归一化: 大小写/下划线差异不算不一致(目录名规范是小写 kebab)
    mod = _load_handler()
    md = "---\nname: \"Huawei_Cloud-Billing-Scout2\"\ndescription: billing query\n---\n"
    _, res = _review(mod, md)
    idx, text, sev, mark, note = _item6(res)
    assert mark == "✅", (mark, note)
