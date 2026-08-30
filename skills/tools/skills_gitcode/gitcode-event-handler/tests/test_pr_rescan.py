"""PR 评论触发"重新扫描"逻辑测试(2026-08-11 用户要求)。

触发条件: 评论必须**同时**含 ① 艾特机器人(@skills_bot/@check_bot/机器人)
+ ② 重新扫描关键字(重新扫描/重新审查/重新检查/重扫/再扫/重新评审/重新评估)。
两者缺一不可, 其他评论一律拒绝。
"""
import importlib.util
import time

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== 正例: 艾特机器人 + 重新扫描关键字 同时满足 =====
def test_rescan_positive_both_required():
    mod = _load_handler()
    positives = [
        "@skills_bot 请重新扫描",
        "@skills_bot 重新审查一下",
        "@check_bot 重新扫描",
        "机器人请重新扫描",
        "@skills_bot 重新检查",
        "机器人重新评审一下",
        "请机器人重新评估",
        "@skills_bot 重扫一遍",
        "机器人再扫一次",
        "@skills_bot 请重新生成报告",
    ]
    for text in positives:
        assert mod.is_rescan_request(text), f"应命中: {text}"


# ===== 反例: 缺任一条件都拒绝 =====
def test_rescan_negative_missing_at():
    """只有关键字, 没艾特机器人 -> 拒绝。"""
    mod = _load_handler()
    for text in ["请重新扫描", "重新审查一下", "重新检查", "重扫"]:
        assert not mod.is_rescan_request(text), f"不应命中(缺@机器人): {text}"


def test_rescan_negative_missing_keyword():
    """只艾特机器人, 没关键字 -> 拒绝。"""
    mod = _load_handler()
    for text in ["@skills_bot 你好", "@skills_bot 这个PR怎么样了",
                 "@check_bot 请处理", "机器人请看一下"]:
        assert not mod.is_rescan_request(text), f"不应命中(缺关键字): {text}"


def test_rescan_negative_irrelevant():
    """无关评论 -> 拒绝。"""
    mod = _load_handler()
    for text in ["好的", "谢谢", "+1", "测试报告已更新", "请开发者修改"]:
        assert not mod.is_rescan_request(text), f"不应命中(无关): {text}"


# ===== 空/异常 fail-open =====
def test_rescan_empty_failopen():
    mod = _load_handler()
    assert mod.is_rescan_request("") is False
    assert mod.is_rescan_request(None) is False


# ===== 限频: 1 分钟内同一 PR 只能触发一次 =====
def test_rescan_throttle_within_window():
    mod = _load_handler()
    mod._pr_rescan_last.clear()
    assert mod._pr_rescan_throttled("repo", 38) is False, "首次触发允许"
    assert mod._pr_rescan_throttled("repo", 38) is True, "1 秒内再次触发应拦截"


def test_rescan_throttle_different_pr():
    mod = _load_handler()
    mod._pr_rescan_last.clear()
    assert mod._pr_rescan_throttled("repo", 38) is False
    assert mod._pr_rescan_throttled("repo", 39) is False, "不同 PR 不受限频影响"


def test_rescan_throttle_window_expires():
    mod = _load_handler()
    mod._pr_rescan_last.clear()
    assert mod._pr_rescan_throttled("repo", 38) is False
    # 模拟超过 60s 窗口
    mod._pr_rescan_last[("repo", "38")] = time.time() - mod.PR_RESCAN_WINDOW - 1
    assert mod._pr_rescan_throttled("repo", 38) is False, "窗口过期后允许再次触发"


# ===== 2026-08-18 扩展: 并入 issue 侧重试类词(用户要求) =====
def test_rescan_positive_extended_words():
    mod = _load_handler()
    for text in ["@skills_bot 重新处理", "@skills_bot 重试一下", "机器人 重新触发",
                 "@check_bot 再试一次", "@skills_bot 重新走一遍流程", "机器人重来",
                 "@skills_bot 重新看下这个PR"]:
        assert mod.is_rescan_request(text), f"应命中(扩展词): {text}"


def test_rescan_negative_still_requires_at():
    mod = _load_handler()
    # 扩展词但没艾特 -> 仍拒绝
    for text in ["重新处理", "重试一下", "再试一次"]:
        assert not mod.is_rescan_request(text), f"不应命中(缺@机器人): {text}"


# ===== 2026-08-18 重扫路径补齐预校验(用户要求, PR #22) =====
def _mk_rescan_payload():
    return {"note": {"id": 1, "body": "@skills_bot 重新扫描",
                     "author": {"login": "tester"}},
            "pull_request": {"number": 22, "title": "t", "body": "b",
                             "user": {"login": "lccode"}},
            "project": {"path_with_namespace": "r/p"}}


def test_rescan_runs_precheck_before_review():
    # 重扫路径: 先预校验(标题修正+配置检查), 再用修正后标题审查
    from unittest import mock
    mod = _load_handler()
    payload = _mk_rescan_payload()
    calls = []
    with mock.patch.object(mod, "ENABLE_PR_REVIEW", True):
        with mock.patch.object(mod, "ENABLE_PR_PRECHECK", True):
            with mock.patch.object(mod, "_pr_rescan_throttled", return_value=False):
                with mock.patch.object(mod, "extract_issue_number_from_pr",
                                       return_value=None):
                    with mock.patch.object(mod, "pr_precheck_and_notify") as pc:
                        pc.return_value = (True, [], "add skill: fixed-title")
                        with mock.patch.object(mod, "pr_review_and_notify") as rv:
                            rv.return_value = ("pass", "报告")
                            with mock.patch.object(mod, "safe_notify"):
                                r = mod.handle_pr_comment(payload)
    pc.assert_called_once()
    # 审查用修正后的标题
    assert rv.call_args[0][2] == "add skill: fixed-title", rv.call_args


def test_rescan_precheck_exception_does_not_block():
    # 预校验异常不阻断重扫(fail-open)
    from unittest import mock
    mod = _load_handler()
    payload = _mk_rescan_payload()
    with mock.patch.object(mod, "ENABLE_PR_REVIEW", True):
        with mock.patch.object(mod, "ENABLE_PR_PRECHECK", True):
            with mock.patch.object(mod, "_pr_rescan_throttled", return_value=False):
                with mock.patch.object(mod, "extract_issue_number_from_pr",
                                       return_value=None):
                    with mock.patch.object(mod, "pr_precheck_and_notify",
                                           side_effect=RuntimeError("boom")):
                        with mock.patch.object(mod, "pr_review_and_notify") as rv:
                            rv.return_value = ("pass", "报告")
                            with mock.patch.object(mod, "safe_notify"):
                                r = mod.handle_pr_comment(payload)
    assert r["action"] == "rescan_requested", r
