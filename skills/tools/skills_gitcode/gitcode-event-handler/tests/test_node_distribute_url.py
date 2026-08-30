"""node_distribute 节点 URL 统一指向 Multica 任务测试(2026-08-13 用户要求)。

背景: node_distribute_url 原为仓库主页(逻辑8)/PR URL(逻辑7), 用户要求统一
改为 Multica 任务 URL(PR 链接语义由 node_source/node_repo 承担)。
"""
import importlib.util
from contextlib import ExitStack
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_node_distribute_call(m_flow):
    """从 db_update_flow 调用列表中找 node_distribute 那次, 返回 (args, kwargs)。"""
    for call in m_flow.call_args_list:
        args = call[0]
        if len(args) >= 2 and args[1] == "node_distribute":
            return args
    return None


# ===== 逻辑8: main 合入, 需求已存在未指派 -> 指派后标记分发中(6386 行) =====
def test_logic8_assign_node_distribute_url_is_multica():
    mod = _load_handler()
    pr = {"number": 555, "id": 999, "title": "add skill: huawei-cloud-x [v1.0.0]",
          "body": "Closes #200", "state": "merged",
          "user": {"login": "wj911831"},
          "created_at": "2026-08-13T10:00:00+08:00",
          "html_url": "https://gitcode.com/g30074593/skillspackage/pull/555"}
    payload = {"merge_request": pr,
               "project": {"path_with_namespace": "g30074593/skillspackage"}}
    req = {"id": 88, "multica_issue_id": "m-dist-1", "multica_issue_number": "SKI-60",
           "gitcode_issue_number": "200", "status": "pr_created"}

    with ExitStack() as stack:
        m_flow = mock.Mock()
        stack.enter_context(mock.patch.object(mod, "db_find_requirement_by_pr_id",
                                              return_value=req))
        stack.enter_context(mock.patch.object(mod, "db_find_requirement_by_issue_number",
                                              return_value=None))
        # multica issue 存在但未指派给分发小队 -> 走指派分支
        stack.enter_context(mock.patch.object(mod, "multica_issue_get",
                                              return_value=(True, {"assignee_type": "user",
                                                                   "assignee_id": "other-1"})))
        stack.enter_context(mock.patch.object(mod, "multica_issue_assign", return_value=None))
        stack.enter_context(mock.patch.object(mod, "multica_issue_comment", return_value=1))
        stack.enter_context(mock.patch.object(mod, "db_update_flow", m_flow))
        stack.enter_context(mock.patch.object(mod, "db_update_requirement", return_value=None))
        stack.enter_context(mock.patch.object(mod, "record_version_history", return_value=None))
        stack.enter_context(mock.patch.object(mod, "_issue_has_bot_comment", return_value=False))
        stack.enter_context(mock.patch.object(mod, "gitcode_issue_comment", return_value=1))
        stack.enter_context(mock.patch.object(mod, "close_gitcode_issue", return_value=None))
        stack.enter_context(mock.patch.object(mod, "gitcode_pr_comment", return_value=1))
        stack.enter_context(mock.patch.object(mod, "gitcode_get", return_value=[]))
        r = mod.dispatch_merged_pr(
            payload, pr, "g30074593/skillspackage", 555, 999,
            pr["title"], pr["body"], pr["html_url"])

    assert r.get("action") in ("distributed", "already_distributed"), r
    args = _find_node_distribute_call(m_flow)
    assert args is not None, f"应调用 db_update_flow(node_distribute), 实际: {m_flow.call_args_list}"
    assert args[2] == "processing", args
    expected = f"{mod.MULTICA_ISSUE_URL_PREFIX}/m-dist-1"
    assert args[3] == expected, f"node_distribute_url 应为 Multica 任务 URL, 实际 {args[3]}"


# ===== 逻辑7: distribute_for_github 分支合入, 命中原任务(6923 行) =====
def test_logic7_match_node_distribute_url_is_multica():
    mod = _load_handler()
    pr = {"number": 556, "id": 998, "title": "add skill: huawei-cloud-x [v1.0.0]",
          "body": "distribute", "state": "merged",
          "user": {"login": "wj911831"},
          "created_at": "2026-08-13T10:00:00+08:00",
          "html_url": "https://gitcode.com/g30074593/skillspackage/pull/556"}
    payload = {"merge_request": pr,
               "project": {"path_with_namespace": "g30074593/skillspackage"}}
    matched = {"id": 77, "multica_issue_id": "m-dist-2", "multica_issue_number": "SKI-66"}

    with ExitStack() as stack:
        m_flow = mock.Mock()
        stack.enter_context(mock.patch.object(mod, "db_find_requirement_by_pr_id",
                                              return_value=None))
        stack.enter_context(mock.patch.object(mod, "extract_skill_name_from_pr",
                                              return_value="huawei-cloud-x"))
        stack.enter_context(mock.patch.object(mod, "find_requirement_by_skill",
                                              return_value=matched))
        stack.enter_context(mock.patch.object(mod, "_distribute_notified", return_value=False))
        stack.enter_context(mock.patch.object(mod, "multica_issue_comment", return_value=1))
        stack.enter_context(mock.patch.object(mod, "db_update_flow", m_flow))
        stack.enter_context(mock.patch.object(mod, "gitcode_pr_comment", return_value=1))
        r = mod.handle_distribute_pr(
            payload, pr, "g30074593/skillspackage", 556, 998,
            pr["title"], pr["body"], pr["html_url"], "wj911831", "王")

    assert r.get("action") == "distribute_matched", r
    args = _find_node_distribute_call(m_flow)
    assert args is not None, f"应调用 db_update_flow(node_distribute), 实际: {m_flow.call_args_list}"
    assert args[2] == "in_progress", args
    expected = f"{mod.MULTICA_ISSUE_URL_PREFIX}/m-dist-2"
    assert args[3] == expected, f"node_distribute_url 应为 Multica 任务 URL, 实际 {args[3]}"
