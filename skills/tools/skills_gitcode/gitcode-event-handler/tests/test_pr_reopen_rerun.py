"""PR reopen 重开事件重新审查测试(2026-08-16 方案A)。

需求: 重开 PR 时, 若 PR 文件内容有变动(head sha 变化), 需要重新走
PR 审查和后续测试等流程; 内容未变动则保持幂等跳过。

实现:
- handle_event PR 白名单加 reopen/reopened -> 走 handle_pr
- handle_pr 检测 reopen + _pr_sha_changed(当前 head sha != 上次记录 sha)
  -> 置 _is_new_commit=True 强制重审重测
- 审查报告评论尾部记录"处理版本: <sha>", 供下次 reopen 对比
"""
import importlib.util
import os
from contextlib import ExitStack
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

os.environ["IGNORE_LABEL_RECHECK_DELAY"] = "0"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pr_payload(action="reopen", number=13,
                title="docs: 为 _get_ak_sk 函数增加环境变量读取说明注释",
                pr_id=9041596):
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "id": pr_id, "iid": number, "number": number, "title": title,
            "description": title, "action": action, "state": "opened",
            "source_branch": "lccode/add-comment-sfsturbo",
            "target_branch": "master",
            "url": f"https://gitcode.com/hd-skills-pre/skills-pre/pulls/{number}",
            "author": {"username": "lccode", "name": "汪亮初"},
            "created_at": "2026-08-16T19:11:49+08:00",
            "updated_at": "2026-08-16T19:11:49+08:00",
        },
        "project": {"path_with_namespace": "hd-skills-pre/skills-pre"},
        "merge_request": {
            "id": pr_id, "iid": number, "number": number, "title": title,
            "description": title, "state": "opened",
            "source_branch": "lccode/add-comment-sfsturbo",
            "target_branch": "master",
            "author": {"username": "lccode", "name": "汪亮初"},
        },
    }


def _common_mocks(mod):
    stack = ExitStack()
    patchers = [
        mock.patch.object(mod, "db_find_requirement_by_issue_id", return_value=None),
        mock.patch.object(mod, "db_find_requirement_by_pr_id", return_value=None),
        mock.patch.object(mod, "db_find_requirement_by_issue_number", return_value=None),
        mock.patch.object(mod, "gitcode_get", return_value=[]),
        mock.patch.object(mod, "gitcode_patch", return_value={}),
        mock.patch.object(mod, "gitcode_issue_comment", return_value=1),
        mock.patch.object(mod, "gitcode_pr_comment", return_value=1),
        mock.patch.object(mod, "db_insert_requirement", return_value=99),
        mock.patch.object(mod, "db_insert_flow", return_value=None),
        mock.patch.object(mod, "multica_issue_comment", return_value=1),
        mock.patch.object(mod, "_devflow_url", return_value="http://devflow?req=99"),
    ]
    for p in patchers:
        stack.enter_context(p)
    return stack


# ===== helper 单测: 正序评论取最后一个匹配 sha =====
def test_recorded_sha_takes_last_match():
    mod = _load_handler()
    comments = [
        {"body": "[skills_bot] ## PR 审查报告\n\n处理版本: aaaa1111bbbb"},
        {"body": "[skills_bot] 其它评论"},
        {"body": "[skills_bot] ## PR 审查报告\n\n处理版本: cccc2222dddd"},
    ]
    with mock.patch.object(mod, "gitcode_get", return_value=comments):
        sha = mod._pr_recorded_sha("r/r", 1)
    assert sha == "cccc2222dddd", f"应取最新(最后一个)记录, 实际 {sha}"


# ===== helper 单测: 无记录返回 None =====
def test_recorded_sha_none():
    mod = _load_handler()
    with mock.patch.object(mod, "gitcode_get", return_value=[]):
        assert mod._pr_recorded_sha("r/r", 1) is None


# ===== helper 单测: sha 变动判断 =====
def test_sha_changed_semantics():
    mod = _load_handler()
    # 当前 sha != 上次 -> 变动
    with mock.patch.object(mod, "_pr_head_sha", return_value="aaaa1111"), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value="bbbb2222"):
        assert mod._pr_sha_changed("r/r", 1) is True
    # 当前 sha == 上次 -> 未变动
    with mock.patch.object(mod, "_pr_head_sha", return_value="aaaa1111"), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value="aaaa1111"):
        assert mod._pr_sha_changed("r/r", 1) is False
    # 查询失败(无当前 sha) -> 保守重审
    with mock.patch.object(mod, "_pr_head_sha", return_value=None), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value="aaaa1111"):
        assert mod._pr_sha_changed("r/r", 1) is True
    # 上次无记录 -> 保守重审
    with mock.patch.object(mod, "_pr_head_sha", return_value="aaaa1111"), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value=None):
        assert mod._pr_sha_changed("r/r", 1) is True


# ===== 回归: 记录为 12 位截断, 当前完整 40 位, 前 12 位相同 -> 未变动 =====
# (2026-08-17 PR #14 实测: 记录 5f4edd200208, 当前完整 40 位, 旧对比
#  完整!=12位 恒判"有变动" -> 未改文件重开也触发审查+测试)
def test_sha_changed_12prefix_same_is_unchanged():
    mod = _load_handler()
    full = "5f4edd2002089048ddcc4edb1d86b30bf731a24c"
    with mock.patch.object(mod, "_pr_head_sha", return_value=full), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value="5f4edd200208"):
        assert mod._pr_sha_changed("r/r", 1) is False, \
            "前 12 位相同应判未变动(记录是 12 位截断)"


# ===== 回归: 12 位前缀不同 -> 有变动 =====
def test_sha_changed_12prefix_diff_is_changed():
    mod = _load_handler()
    with mock.patch.object(mod, "_pr_head_sha",
                           return_value="abcdef1234567890abcdef1234567890abcdef12"), \
         mock.patch.object(mod, "_pr_recorded_sha", return_value="5f4edd200208"):
        assert mod._pr_sha_changed("r/r", 1) is True, \
            "前 12 位不同应判有变动"


# ===== reopen 且 sha 变动 -> 强制重审重测(绕过幂等) =====
def test_reopen_sha_changed_reruns_review():
    mod = _load_handler()
    payload = _pr_payload(action="reopen")
    with _common_mocks(mod):
        # 模拟该 PR 已关联开发需求(有 multica 任务)
        with mock.patch.object(
                mod, "db_find_requirement_by_pr_id",
                return_value={"id": 12, "multica_issue_id": "m-67",
                              "gitcode_pr_id": "13"}), \
             mock.patch.object(mod, "_pr_sha_changed", return_value=True) as m_check, \
             mock.patch.object(mod, "pr_review_and_notify",
                               return_value=("pass", "report")) as m_review, \
             mock.patch.object(mod, "_test_squad_notified", return_value=False), \
             mock.patch.object(mod, "_notify_test_squad",
                               return_value=(None, None)) as m_notify:
            r = mod.handle_event(payload)
    assert r["event"] == "pr_created", str(r)
    m_check.assert_called_once()
    # 审查必须强制重新执行(force=True)
    call = m_review.call_args_list[0]
    assert call.kwargs.get("force") is True, \
        f"sha 变动应 force 重审, 实际 {call.kwargs}"
    assert m_notify.called, "sha 变动应重新 @测试小队"
    assert r["action"] == "matched", str(r)


# ===== reopen 且 sha 未变 -> 幂等跳过(不重审不重测) =====
def test_reopen_sha_unchanged_idempotent():
    mod = _load_handler()
    payload = _pr_payload(action="reopen")
    with _common_mocks(mod):
        with mock.patch.object(
                mod, "db_find_requirement_by_pr_id",
                return_value={"id": 12, "multica_issue_id": "m-67",
                              "gitcode_pr_id": "13"}), \
             mock.patch.object(mod, "_pr_sha_changed", return_value=False) as m_check, \
             mock.patch.object(mod, "pr_review_and_notify",
                               return_value=("pass", "report")) as m_review, \
             mock.patch.object(mod, "_test_squad_notified", return_value=True) as m_dup:
            r = mod.handle_event(payload)
    assert r["event"] == "pr_created", str(r)
    m_check.assert_called_once()
    # 未变动: 审查不 force(幂等跳过), 且走 matched_dup 不再 @测试
    call = m_review.call_args_list[0]
    assert call.kwargs.get("force") in (False, None), \
        f"sha 未变不应 force, 实际 {call.kwargs}"
    assert m_dup.called, "sha 未变应命中已下发幂等"
    assert r["action"] == "matched_dup", str(r)


# ===== reopen 且 sha 对比失败 -> 保守重审 =====
def test_reopen_sha_check_fail_conservative():
    mod = _load_handler()
    payload = _pr_payload(action="reopen")
    with _common_mocks(mod):
        with mock.patch.object(mod, "_pr_sha_changed",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(mod, "pr_review_and_notify",
                               return_value=("pass", "report")) as m_review:
            r = mod.handle_event(payload)
    assert r["event"] == "pr_created", str(r)
    # 对比异常按"未变动"处理(幂等跳过, 不误重审)
    call = m_review.call_args_list[0]
    assert call.kwargs.get("force") in (False, None), \
        f"sha 对比异常不应 force, 实际 {call.kwargs}"


# ===== 审查报告评论尾部记录处理版本 sha =====
def test_review_comment_appends_sha_note():
    mod = _load_handler()
    with mock.patch.object(mod, "pr_review_report",
                           return_value=("pass", "## PR 审查报告\n内容")), \
         mock.patch.object(mod, "_pr_head_sha", return_value="abcdef123456"), \
         mock.patch.object(mod, "gitcode_pr_comment",
                           return_value=1) as m_comment, \
         mock.patch.object(mod, "_assign_pr_reviewer", return_value=None):
        concl, report = mod.pr_review_and_notify("r/r", 1, "t", "b", force=True)
    call = m_comment.call_args_list[0]
    body = call[0][2]
    assert "处理版本: abcdef123456" in body, f"评论应记录处理版本 sha, 实际 {body[:100]}"


# ===== 非 reopen 的普通 open 事件不受影响 =====
def test_open_event_unchanged():
    mod = _load_handler()
    payload = _pr_payload(action="open")
    with _common_mocks(mod):
        with mock.patch.object(mod, "_pr_sha_changed",
                               side_effect=AssertionError("open 事件不应做 sha 对比")):
            r = mod.handle_event(payload)
    assert r["event"] == "pr_created", str(r)
