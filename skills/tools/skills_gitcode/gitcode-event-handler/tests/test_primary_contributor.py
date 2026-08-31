"""提交历史驱动分配测试(2026-08-11 用户要求)。

背景: skillspackage 无 codeowner 映射 -> 非 bot skill 的 bug 全部"已跳过"
卡死(issue #101)。方案: 从提交历史推断主要人工开发者, 直接分配。
"""
import importlib.util
from unittest.mock import patch

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _commits(*authors):
    return [{"author": {"login": a}} for a in authors if a]


# ===== 提交历史推断: 提交最多的非 bot 开发者 =====
def test_primary_contributor_picks_top():
    mod = _load_handler()
    with patch.object(mod, "_get_repo_products",
                      return_value={"huawei-cloud-x": "skills/compute/huawei-cloud-x"}), \
         patch.object(mod, "gitcode_get",
                      return_value=_commits("Jason7602", "Jason7602", "Jason7602",
                                            "lccode", "skills_bot", None)):
        p = mod._skill_primary_contributor("repo", "huawei-cloud-x")
    assert p == "Jason7602", "应取提交次数最多的非 bot 开发者"


# ===== 全是 bot/空作者 -> None =====
def test_primary_contributor_all_bot_none():
    mod = _load_handler()
    with patch.object(mod, "_get_repo_products",
                      return_value={"huawei-cloud-x": "skills/x"}), \
         patch.object(mod, "gitcode_get",
                      return_value=_commits("skills_bot", "check_bot", None)):
        p = mod._skill_primary_contributor("repo", "huawei-cloud-x")
    assert p is None, "全是 bot/空作者应返回 None"


# ===== 查不到路径 -> None =====
def test_primary_contributor_no_path_none():
    mod = _load_handler()
    with patch.object(mod, "_get_repo_products", return_value={}):
        p = mod._skill_primary_contributor("repo", "huawei-cloud-x")
    assert p is None


# ===== codeowner 无映射 -> 提交历史推断分配 =====
def test_codeowner_fallback_assigns_contributor():
    mod = _load_handler()
    with patch.object(mod, "fetch_codeowner_mapping", return_value=([], None)), \
         patch.object(mod, "extract_skill_names", return_value=["huawei-cloud-x"]), \
         patch.object(mod, "_skill_primary_contributor",
                      return_value="Jason7602"), \
         patch.object(mod, "_assign_contributor",
                      return_value={"action": "assigned", "owner": "Jason7602"}) as m:
        r = mod.codeowner_assign_issue("g30074593/skillspackage", 101, "title",
                                       "huawei-cloud-x bug", 1, "url", None, None,
                                       "wj911831", "wj911831")
    # 2026-08-12: 不做跨仓兜底(测试环境, 现网无跨仓), codeowner_assign_issue
    # 单次调用 _skill_primary_contributor 拿当前仓库推断者, 传给 _assign_contributor。
    assert m.call_args_list, "_assign_contributor 应被调用"
    _call = m.call_args_list[0][0]
    assert _call[2] == "Jason7602", f"应分配推断出的 Jason7602, 实际 {_call[2]}"
    assert len(m.call_args_list) >= 1


# ===== 推断不出 -> 原跳过逻辑 =====
def test_codeowner_fallback_skip_when_no_contributor():
    mod = _load_handler()
    with patch.object(mod, "fetch_codeowner_mapping", return_value=([], None)), \
         patch.object(mod, "extract_skill_names", return_value=["huawei-cloud-x"]), \
         patch.object(mod, "_skill_primary_contributor", return_value=None), \
         patch.object(mod, "_issue_has_bot_comment", return_value=True):
        r = mod.codeowner_assign_issue("repo", 1, "t", "huawei-cloud-x", 1,
                                       "u", None, None, "a", "a")
    assert r.get("action") == "skip", "推断不出应回退原跳过逻辑"
