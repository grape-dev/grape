"""issue 已关联 PR 时禁止建任务/入库测试(2026-08-18 用户要求)。

场景: 评测问题单等 issue 正文带"对应 PR: .../merge_requests/15"时, 说明
修复已在 PR 中推进, 不应再创建开发任务/入库(避免重复开发)。
"""
from unittest import mock

import gitcode_event_handler as h


# ---- _issue_linked_pr 检测 ----

def test_linked_pr_positive_forms():
    assert h._issue_linked_pr(
        "[huawei-cloud-dns-query] 问题修改",
        "对应 PR: https://gitcode.com/hd-skills-pre/skills-pre/merge_requests/15") == 15
    assert h._issue_linked_pr("t", "关联 PR: https://gitcode.com/x/y/pull/15") == 15
    assert h._issue_linked_pr("t", "PR #15") == 15
    assert h._issue_linked_pr("t", "关联 PR: #15") == 15
    assert h._issue_linked_pr("t", "相关 PR:15") == 15
    assert h._issue_linked_pr("t", "merge request 15") == 15


def test_linked_pr_negative():
    assert h._issue_linked_pr("t", "P0 功能缺陷 1: 私网记录集命令全部不存在") is None
    assert h._issue_linked_pr("t", "15 个问题") is None
    assert h._issue_linked_pr("t", "") is None
    assert h._issue_linked_pr("", "") is None


# ---- handle_issue: 命中关联 PR -> 不建任务不入库 ----

def _mk_payload(title="[huawei-cloud-dns-query] 问题修改",
                body="P0 功能缺陷\n对应 PR: https://gitcode.com/x/y/merge_requests/15"):
    return {
        "issue": {"number": 60, "id": 99999, "title": title, "body": body,
                  "user": {"login": "tester"}, "created_at": "2026-08-18T00:00:00+08:00",
                  "updated_at": "2026-08-18T00:00:00+08:00",
                  "html_url": "https://gitcode.com/r/x/issues/60"},
        "project": {"path_with_namespace": "hd-skills-pre/skills-pre"},
    }


def test_handle_issue_with_linked_pr_no_task_no_db():
    payload = _mk_payload()
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=False):
            with mock.patch.object(h, "safe_notify") as sn:
                with mock.patch.object(h, "_create_dev_task_or_notify") as ctn:
                    with mock.patch.object(h, "db_insert_requirement") as dbi:
                        r = h.handle_issue(payload)
    assert r["action"] == "issue_linked_pr", r
    assert r["requirement_id"] is None
    ctn.assert_not_called()   # 未创建开发任务
    dbi.assert_not_called()   # 未入库
    assert sn.called          # 发了"已关联 PR"评论


def test_handle_issue_without_linked_pr_creates_task():
    payload = _mk_payload(body="P0 功能缺陷, 无 PR 引用")
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=False):
            with mock.patch.object(h, "_has_waiting_prompt_comment", return_value=False):
                with mock.patch.object(h, "_created_label_override",
                                       return_value=("bug", ["评测问题单"])):
                    with mock.patch.object(h, "extract_skill_name_from_pr",
                                           return_value="huawei-cloud-dns-query"):
                        with mock.patch.object(h, "_repo_has_skill", return_value=True):
                            with mock.patch.object(h, "_bug_skill_open_pr", return_value=None):
                                with mock.patch.object(h, "_dup_check_issue", return_value=None):
                                    with mock.patch.object(h, "issue_quality_ok",
                                                           return_value=(True, "ok")):
                                        with mock.patch.object(h, "_create_dev_task_or_notify",
                                                               return_value=("mid-1", "SKI-9", None)):
                                            with mock.patch.object(h, "db_insert_requirement",
                                                                   return_value=77):
                                                with mock.patch.object(h, "db_insert_flow", return_value=1):
                                                    with mock.patch.object(h, "safe_notify",
                                                                           return_value=None):
                                                        r = h.handle_issue(payload)
    assert r["action"] == "created", r  # 无关联 PR 正常建任务


def test_handle_issue_linked_pr_idempotent():
    # 幂等: 已发过"已关联 PR"评论则不再重复发
    payload = _mk_payload()
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=True):
            with mock.patch.object(h, "safe_notify") as sn:
                r = h.handle_issue(payload)
    assert r["action"] == "issue_linked_pr"
    assert not sn.called


def test_handle_issue_feature_with_linked_pr_also_blocked():
    # 需求类(feature)issue 带 PR 引用同样不建任务不入库(检查不区分 label,
    # 在 bug 分流之前, feature 一样走该拦截)。mock 判重隔离(本地化后能力
    # 索引完整, 判重可能先命中已有 skill, 本测试只验证关联 PR 拦截)
    payload = _mk_payload(title="新增 huawei-cloud-dns-query 查询功能",
                          body="开发新 skill\n关联 PR: #18")
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=False):
            with mock.patch.object(h, "_created_label_override",
                                   return_value=("feature", [])):
                with mock.patch.object(h, "_dup_check_issue", return_value=None):
                    with mock.patch.object(h, "safe_notify") as sn:
                        with mock.patch.object(h, "_create_dev_task_or_notify") as ctn:
                            with mock.patch.object(h, "db_insert_requirement") as dbi:
                                r = h.handle_issue(payload)
    assert r["action"] == "issue_linked_pr", r
    assert r["requirement_id"] is None
    ctn.assert_not_called()
    dbi.assert_not_called()
    assert sn.called
