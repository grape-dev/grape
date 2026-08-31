"""PR 合入后关联 issue 关闭规则测试(2026-08-18 用户拍板)。

规则: 无论机器人/开发者提交的 PR, 合入后都关闭关联 issue; 但 issue 关联
多个 PR 且仍有 open 状态的 PR(开发还在继续) -> 不关闭, 评论说明待全部
PR 合入后关闭。原\"开发者 PR 不自动关\"分流移除。
"""
from unittest import mock
from contextlib import ExitStack

import gitcode_event_handler as h


def _mk_payload(pr_number=30, author="lccode"):
    return {"pull_request": {"number": pr_number, "title": "add skill: x",
                             "body": "关联 issue #55",
                             "user": {"login": author}},
            "project": {"path_with_namespace": "r/p"}}


# ===== _issue_has_open_pr =====
def test_has_open_pr_true_when_other_open():
    with mock.patch.object(h, "db_connect"):
        with mock.patch.object(h, "gitcode_get",
                               return_value={"state": "open"}) as gg:
            # mock db_connect 返回游标带 rows
            cm = mock.MagicMock()
            cm.__enter__.return_value.cursor.return_value.__enter__.return_value \
                .fetchall.return_value = [(66,)]
            with mock.patch.object(h, "db_connect", return_value=cm):
                assert h._issue_has_open_pr("r/p", 55, 30) is True
    gg.assert_called_once_with("/repos/r/p/pulls/66")


def test_has_open_pr_false_when_other_closed():
    cm = mock.MagicMock()
    cm.__enter__.return_value.cursor.return_value.__enter__.return_value \
        .fetchall.return_value = [(66,)]
    with mock.patch.object(h, "db_connect", return_value=cm):
        with mock.patch.object(h, "gitcode_get",
                               return_value={"state": "closed"}):
            assert h._issue_has_open_pr("r/p", 55, 30) is False


def test_has_open_pr_false_no_other_pr():
    cm = mock.MagicMock()
    cm.__enter__.return_value.cursor.return_value.__enter__.return_value \
        .fetchall.return_value = []
    with mock.patch.object(h, "db_connect", return_value=cm):
        assert h._issue_has_open_pr("r/p", 55, 30) is False


def test_has_open_pr_false_on_query_error():
    # 查询失败(DB/API) -> False, 不阻断关闭
    with mock.patch.object(h, "db_connect", side_effect=RuntimeError("db down")):
        assert h._issue_has_open_pr("r/p", 55, 30) is False


# ===== dispatch_merged_pr 1.4 段 =====
def _merged_ctx():
    """dispatch_merged_pr 后续分发逻辑的公共 mock(1.4 段在分发前执行,
    action 断言放宽, 重点断言 close 行为)"""
    return [
        mock.patch.object(h, "get_repo_full_name", return_value="r/p"),
        mock.patch.object(h, "db_find_requirement_by_pr_id",
                          return_value={"id": 9, "multica_issue_id": 7,
                                        "gitcode_issue_number": "55",
                                        "status": "developing"}),
        mock.patch.object(h, "db_update_flow"),
        mock.patch.object(h, "db_update_requirement"),
        mock.patch.object(h, "record_version_history"),
        mock.patch.object(h, "multica_issue_get",
                          return_value=(True, {"assignee_type": "squad",
                                               "assignee_id": h.SQUAD_DISTRIBUTE})),
    ]


def _run_merged(has_open_pr, author="lccode"):
    """执行 dispatch_merged_pr 并返回 (action, safe_notify_mock)。全部 mock
    与断言在 ExitStack 内(退出后 mock 还原, close_gitcode_issue 比较失效)。"""
    payload = _mk_payload(author=author)
    with ExitStack() as st:
        for p in _merged_ctx():
            st.enter_context(p)
        st.enter_context(mock.patch.object(h, "_issue_has_open_pr",
                                           return_value=has_open_pr))
        st.enter_context(mock.patch.object(h, "_issue_has_bot_comment",
                                           return_value=False))
        sn = st.enter_context(mock.patch.object(h, "safe_notify"))
        st.enter_context(mock.patch.object(h, "close_gitcode_issue"))
        r = h.dispatch_merged_pr(payload, payload["pull_request"], "r/p",
                                 30, 300, "t", "b", "https://x/pull/30")
        close_calls = [c for c in sn.call_args_list
                       if c[0][0] == h.close_gitcode_issue]
        bodies = [c[0][3] for c in sn.call_args_list
                  if c[0][0] == h.gitcode_issue_comment]
        return r, close_calls, bodies


def test_merged_closes_issue_no_other_open_pr():
    # 无其他 open PR -> 评论 + 关闭(无论机器人/开发者 PR)
    r, close_calls, bodies = _run_merged(has_open_pr=False)
    assert close_calls, "应通过 safe_notify 关闭 issue"
    assert bodies and "已开发完成并合入主仓" in bodies[0], bodies


def test_merged_keeps_issue_open_when_other_pr_open():
    # 有其他 open PR -> 不关闭, 评论说明
    r, close_calls, bodies = _run_merged(has_open_pr=True)
    assert not close_calls, "有其他 open PR 不应关闭 issue"
    assert bodies and "还有其他 PR 在处理中" in bodies[0], bodies


def test_merged_bot_author_also_closes():
    # 机器人提交的 PR 合入同样关闭(统一规则)
    r, close_calls, bodies = _run_merged(has_open_pr=False, author="skills_bot")
    assert close_calls, "机器人 PR 合入同样应关闭 issue"
