"""issue 评论"@机器人 重新处理"重试触发测试(2026-08-18 方案A)。

用户要求: 支持 @机器人 主动触发 issue 重新处理(对齐 PR 评论"重新扫描"),
解决"建任务失败静默/判重关闭后用户干等, 无法主动触发重新处理"的痛点。
"""
from unittest import mock

import gitcode_event_handler as h


# ---- is_issue_retry_request: @机器人 + 重试词 ----

def test_retry_positive_phrasings():
    for txt in ["@skills_bot 重新处理",
                "[@skills_bot](https://gitcode.com/skills_bot) 请重新处理一下",
                "@skills_bot 重新评估这个issue",
                "@skills_bot 重试一次",
                "@skills_bot 再处理一次",
                "机器人 重新走一遍流程",
                "@skills_bot 重新检查一下"]:
        assert h.is_issue_retry_request(txt), txt


def test_retry_negative():
    for txt in ["@skills_bot 重新扫描",      # 扫描是 PR 语义
                "@skills_bot 帮我修复这个bug",  # 转机器人, 非重试
                "重新处理一下",              # 无@
                "@skills_bot 你好",          # 无重试词
                "@skills_bot 重新开发",      # 开发=转机器人语义
                "今天天气不错"]:
        assert not h.is_issue_retry_request(txt), txt


def test_retry_no_conflict_with_bot_fix():
    # 重试与转机器人互不吞并
    assert h.is_bot_fix_request("@skills_bot 重新处理") is False
    assert h.is_bot_fix_request("@skills_bot 帮我修复这个bug") is True
    assert h.is_issue_retry_request("@skills_bot 帮我修复这个bug") is False


# ---- 无关评论过滤豁免 ----

def test_retry_comment_not_filtered_as_irrelevant():
    # "重新处理"无功能词, 不加豁免会被判无关静默跳过, 重试永远不触发
    assert h.is_irrelevant_comment("@skills_bot 重新处理") is False
    assert h.is_irrelevant_comment("帮我修复这个bug") is False
    assert h.is_irrelevant_comment("天气很好呀") is True


# ---- handle_issue force_retry: 放行 bot 评论守卫 ----

def _mk_payload(title="[huawei-cloud-dns-query] 问题修改",
                 body="【问题描述】DNS查询接口返回慢\n【触发场景】使用查询接口时\n【影响】接口超时"):
    return {
        "issue": {"number": 50, "id": 99999, "title": title, "body": body,
                  "user": {"login": "tester"}, "created_at": "2026-08-18T00:00:00+08:00",
                  "updated_at": "2026-08-18T00:00:00+08:00",
                  "html_url": "https://gitcode.com/r/x/issues/50"},
        "project": {"path_with_namespace": "hd-skills-pre/skills-pre"},
    }


def test_handle_issue_force_retry_bypasses_bot_comment_guard():
    payload = _mk_payload()
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=True):
            with mock.patch.object(h, "_has_waiting_prompt_comment", return_value=False):
                with mock.patch.object(h, "_created_label_override",
                                       return_value=("bug", ["评测问题单"])):
                    with mock.patch.object(h, "extract_skill_name_from_pr",
                                           return_value="huawei-cloud-dns-query"):
                        with mock.patch.object(h, "_repo_has_skill", return_value=True):
                            with mock.patch.object(h, "_bug_skill_open_pr", return_value=None):
                                with mock.patch.object(h, "_dup_check_issue",
                                                   return_value=(None, False)):
                                    with mock.patch.object(h, "issue_quality_ok",
                                                           return_value=(True, "ok")):
                                        with mock.patch.object(h, "_create_dev_task_or_notify",
                                                               return_value=("mid-1", "SKI-9", None)):
                                            with mock.patch.object(h, "db_insert_requirement",
                                                                   return_value=77):
                                                with mock.patch.object(h, "db_insert_flow", return_value=1):
                                                    with mock.patch.object(h, "safe_notify",
                                                                           return_value=None):
                                                        r = h.handle_issue(payload, force_retry=True)
    assert r and r["action"] == "created", r  # 未被 bot 评论守卫拦截, 走完建任务


def test_handle_issue_without_force_retry_still_guarded():
    payload = _mk_payload()
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "_issue_has_bot_comment", return_value=True):
            with mock.patch.object(h, "_has_waiting_prompt_comment", return_value=False):
                r = h.handle_issue(payload, force_retry=False)
    assert r and r["action"] == "skip", r  # 守卫仍在, 不 force 时拦截


# ---- handle_issue_comment 重试分支 ----

def _mk_comment_payload(body="@"):
    return {
        "note": {"id": 777, "body": body, "author": {"login": "tester"}},
        "issue": {"number": 50, "id": 99999, "title": "[huawei-cloud-xxx] 问题修改",
                  "body": "P0", "user": {"login": "tester"}},
        "project": {"path_with_namespace": "hd-skills-pre/skills-pre"},
    }


def test_comment_retry_with_existing_task_tells_status():
    payload = _mk_comment_payload("@skills_bot 重新处理")
    with mock.patch.object(h, "db_find_requirement_by_issue_id",
                           return_value={"id": 77, "multica_issue_id": "mid-1"}):
        with mock.patch.object(h, "safe_notify") as sn:
            r = h.handle_issue_comment(payload)
    assert r["action"] == "retry_already_processing", r
    assert sn.called  # 已发"已在处理中"评论


def test_comment_retry_need_info_tells_supplement():
    payload = _mk_comment_payload("@skills_bot 重新处理")
    with mock.patch.object(h, "db_find_requirement_by_issue_id",
                           return_value={"id": 77, "multica_issue_id": None,
                                         "status": "need_info"}):
        with mock.patch.object(h, "safe_notify") as sn:
            r = h.handle_issue_comment(payload)
    assert r["action"] == "retry_need_info", r
    assert sn.called


def test_comment_retry_no_record_forces_re_evaluate():
    payload = _mk_comment_payload("@skills_bot 重新处理")
    with mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None):
        with mock.patch.object(h, "handle_issue") as hi:
            hi.return_value = {"action": "created", "requirement_id": 88}
            r = h.handle_issue_comment(payload)
    assert r["event"] == "issue_retried"
    assert r["action"] == "created"
    # force_retry=True 必须传给 handle_issue
    assert hi.call_args[0][0] is payload
    assert hi.call_args[1]["force_retry"] is True


# ---- 2026-08-18: issue reopen 事件重新评估(对齐 PR reopen) ----

def test_issue_reopen_dispatches_to_handle_issue_force_retry():
    payload = _mk_payload()
    with mock.patch.object(h, "handle_issue") as hi:
        hi.return_value = {"action": "created", "requirement_id": 88}
        r = h.handle_event({"object_kind": "issue",
                            "object_attributes": {"iid": 50, "action": "reopen"},
                            "issue": payload["issue"],
                            "project": payload["project"]})
    assert r["event"] == "issue_reopened"
    assert r["action"] == "created"
    assert hi.call_args[1]["force_retry"] is True


def test_issue_other_action_still_ignored():
    r = h.handle_event({"object_kind": "issue",
                        "object_attributes": {"iid": 50, "action": "closed"},
                        "issue": {"number": 50, "id": 1, "title": "t", "body": "b",
                                  "user": {"login": "x"}},
                        "project": {"path_with_namespace": "hd-skills-pre/skills-pre"}})
    assert r["action"] == "ignored"
