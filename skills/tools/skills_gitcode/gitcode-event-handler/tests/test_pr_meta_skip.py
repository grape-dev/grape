"""PR 元数据 update 事件直接跳过测试(2026-08-18 用户拍板)。

add reviewer/approver/change merger/mr update 等元数据事件不改变任何代码
内容(head sha 不变), 审查结果不会变 —— 直接跳过, 不进预校验/审查/评论
(替代窗口合并方案: 窗口只是限频缓解, 根治是这类事件根本不该触发审查;
PR #21/#23 实测重复评论的根因)。source update(新 commit)/open/reopen
不参与(新代码必须重审)。
"""
from unittest import mock

import gitcode_event_handler as h


def _mk_payload(action="update", act_desc="", reason="add reviewer", state="opened"):
    return {"object_attributes": {"action": action, "act_desc": act_desc,
                                  "update_reason": reason, "state": state},
            "pull_request": {"number": 23, "title": "t", "body": "b",
                             "user": {"login": "x"}},
            "project": {"path_with_namespace": "r/p"}}


def test_meta_update_skipped_directly():
    # 元数据 update -> 直接跳过, 不进预校验/审查
    payload = _mk_payload()
    with mock.patch.object(h, "handle_distribute_pr") as hd:
        with mock.patch.object(h, "pr_precheck_and_notify") as pc:
            with mock.patch.object(h, "pr_review_and_notify") as rv:
                r = h.handle_pr(payload)
    assert r["action"] == "skipped_meta_update", r
    hd.assert_not_called()
    pc.assert_not_called()
    rv.assert_not_called()


def test_meta_update_variants_all_skipped():
    # 各种元数据 reason 都跳过
    for reason in ("add reviewer", "remove reviewer", "change merger",
                   "add approver", "remove approver", "add tester",
                   "mr update", "add assign", "remove assign"):
        payload = _mk_payload(reason=reason)
        with mock.patch.object(h, "handle_distribute_pr"):
            r = h.handle_pr(payload)
        assert r["action"] == "skipped_meta_update", (reason, r)


def test_source_update_not_skipped():
    # source update(新 commit)必须处理
    payload = _mk_payload(act_desc="source_branch_changed", reason="source update")
    with mock.patch.object(h, "handle_distribute_pr"):
        with mock.patch.object(h, "ENABLE_PR_PRECHECK", True):
            with mock.patch.object(h, "pr_precheck_and_notify",
                                   return_value=(True, [], None)) as pc:
                with mock.patch.object(h, "pr_review_and_notify",
                                       return_value=("pass", "")) as rv:
                    with mock.patch.object(h, "extract_issue_number_from_pr",
                                           return_value=None):
                        r = h.handle_pr(payload)
    assert r.get("action") != "skipped_meta_update", r
    pc.assert_called_once()


def test_open_not_skipped():
    payload = _mk_payload(action="open", reason="")
    with mock.patch.object(h, "handle_distribute_pr"):
        with mock.patch.object(h, "ENABLE_PR_PRECHECK", True):
            with mock.patch.object(h, "pr_precheck_and_notify",
                                   return_value=(True, [], None)):
                with mock.patch.object(h, "pr_review_and_notify",
                                       return_value=("pass", "")):
                    with mock.patch.object(h, "extract_issue_number_from_pr",
                                           return_value=None):
                        r = h.handle_pr(payload)
    assert r.get("action") != "skipped_meta_update", r
