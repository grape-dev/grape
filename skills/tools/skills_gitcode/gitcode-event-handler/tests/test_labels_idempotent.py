"""set_issue_labels 幂等优化测试(2026-08-11 用户要求)。

背景: 每次打标都"删 5 个管理标签 + 加 2 个目标标签", 多次 update 事件
触发重评估时页面标签反复闪烁(issue #101 实测)。优化: 当前标签 == 目标
标签(且无多余管理标签)时跳过, 不删不加。
"""
import importlib.util
from unittest.mock import patch

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== 幂等: 当前标签 == 目标 -> 跳过(不删不加) =====
def test_idempotent_skip_when_labels_match():
    mod = _load_handler()
    with patch.object(mod, "_api_issue_labels", return_value=["bug", "triaged"]) as m_read, \
         patch.object(mod, "gitcode_delete") as m_del, \
         patch.object(mod, "gitcode_patch") as m_patch:
        mod.set_issue_labels("repo", 101, ["bug", "triaged"])
    m_del.assert_not_called(), "标签已匹配不应删除"
    m_patch.assert_not_called(), "标签已匹配不应追加"


# ===== 有变化: 目标 [feature,triaged] vs 当前 [bug] -> 执行删/加 =====
def test_update_when_labels_differ():
    mod = _load_handler()
    with patch.object(mod, "_api_issue_labels", return_value=["bug"]) as _m, \
         patch.object(mod, "gitcode_delete") as m_del, \
         patch.object(mod, "gitcode_patch") as m_patch:
        mod.set_issue_labels("repo", 101, ["feature", "triaged"])
    assert m_del.call_count == len(mod.ALL_LABELS), "有变化时应删除全部管理标签"
    m_patch.assert_called_once(), "有变化时应追加目标标签"


# ===== 有多余管理标签(如 need-info 残留) -> 清理 =====
def test_cleanup_stale_managed_labels():
    mod = _load_handler()
    # 当前 [bug, need-info], 目标 [bug, triaged] -> need-info 多余, 需删+加
    with patch.object(mod, "_api_issue_labels",
                      return_value=["bug", "need-info"]), \
         patch.object(mod, "gitcode_delete") as m_del, \
         patch.object(mod, "gitcode_patch") as m_patch:
        mod.set_issue_labels("repo", 101, ["bug", "triaged"])
    assert m_del.call_count == len(mod.ALL_LABELS)
    m_patch.assert_called_once()


# ===== 非管理标签不影响判定 =====
def test_non_managed_labels_ignored():
    mod = _load_handler()
    # 当前 [bug, triaged, codeowner-xxx], 目标 [bug, triaged] -> codeowner 非管理标签, 跳过
    with patch.object(mod, "_api_issue_labels",
                      return_value=["bug", "triaged", "codeowner-ops"]) as m_read, \
         patch.object(mod, "gitcode_delete") as m_del, \
         patch.object(mod, "gitcode_patch") as m_patch:
        mod.set_issue_labels("repo", 101, ["bug", "triaged"])
    m_del.assert_not_called()
    m_patch.assert_not_called()


# ===== 读标签失败 -> 保守执行(不跳过) =====
def test_read_failure_fallback_execute():
    mod = _load_handler()
    with patch.object(mod, "_api_issue_labels", return_value=None), \
         patch.object(mod, "gitcode_delete") as m_del, \
         patch.object(mod, "gitcode_patch") as m_patch:
        mod.set_issue_labels("repo", 101, ["bug", "triaged"])
    assert m_del.call_count == len(mod.ALL_LABELS), "读取失败应保守执行(删+加)"
    m_patch.assert_called_once()
