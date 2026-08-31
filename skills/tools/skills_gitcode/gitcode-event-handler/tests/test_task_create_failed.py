"""开发任务创建失败告知测试(2026-08-18 用户要求)。

场景: issue 创建开发任务失败时(如 #45 撞 409)不能静默崩溃, 要评论告知
创建者; 文案**不涉及任务系统名称及内部细节**(用户明确要求)。
"""
from unittest import mock

import gitcode_event_handler as h


def test_success_returns_3tuple():
    with mock.patch.object(h, "multica_issue_create",
                           return_value=("id-1", "SKI-9")):
        r = h._create_dev_task_or_notify("r", 1, "t", "b")
    assert r == ("id-1", "SKI-9", None)


def test_failure_notifies_creator_with_sanitized_msg():
    calls = []
    with mock.patch.object(h, "multica_issue_create",
                           side_effect=RuntimeError("The request conflicts with the current state")):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=False):
            with mock.patch.object(h, "safe_notify") as sn:
                sn.side_effect = lambda fn, *a: calls.append((fn.__name__, a))
                r = h._create_dev_task_or_notify("r", 45, "t", "b")
    assert r[0] is None and r[1] is None and r[2]
    assert len(calls) == 1
    assert calls[0][0] == "gitcode_issue_comment"
    msg = calls[0][1][2]
    # 告知创建者
    assert "开发任务创建失败" in msg
    assert "请稍后重试" in msg
    # 脱敏: 不涉及任务系统名称及内部细节
    assert "multica" not in msg.lower()
    assert "SKI-" not in msg
    assert "任务系统" not in msg and "工作区" not in msg and "接口" not in msg


def test_failure_idempotent_no_duplicate_comment():
    calls = []
    with mock.patch.object(h, "multica_issue_create", side_effect=RuntimeError("x")):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=True):
            with mock.patch.object(h, "safe_notify") as sn:
                sn.side_effect = lambda fn, *a: calls.append((fn.__name__, a))
                h._create_dev_task_or_notify("r", 45, "t", "b")
    assert len(calls) == 0  # 已有失败告知评论, 不重复发


def test_failure_marker_defined():
    # 幂等 marker 与文案关键词一致
    assert h.MARKERS["task_create_failed"] == "开发任务创建失败"
