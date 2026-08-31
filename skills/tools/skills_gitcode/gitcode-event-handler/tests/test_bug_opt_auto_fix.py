"""bug/优化类 issue 一律直接进机器人修复流水线测试(2026-08-13 用户要求)。

背景: 原逻辑按\"skill 贡献者是否含机器人账号\"(_skill_has_bot_contributor)
分流 —— 人工开发 skill 的 bug/优化走 codeowner 分配(艾特历史提交人/模块
负责人)。用户要求: 无论 skill 是否人工开发, 一律直接进入机器人修复流水线
(建 Multica 任务 @skills_dev)。
"""
import importlib.util
import os
from contextlib import ExitStack
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

# 测试提速: 关闭 ignore 标签 API 兜底的 2.5s 补查
os.environ["IGNORE_LABEL_RECHECK_DELAY"] = "0"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _issue_payload(kind="issue", number=200, issue_id=999200, title="t", body="b",
                   action="open"):
    return {
        "object_kind": kind,
        "object_attributes": {
            "id": issue_id, "iid": number, "title": title,
            "description": body, "action": action, "state": "opened",
            "author": {"username": "wj911831", "name": "王"},
        },
        "issue": {
            "id": issue_id, "iid": number, "number": number, "title": title,
            "body": body, "state": "opened",
            "url": f"https://gitcode.com/g30074593/skillspackage/issues/{number}",
            "author": {"username": "wj911831", "name": "王"},
            "created_at": "2026-08-13T10:00:00+08:00",
            "updated_at": "2026-08-13T10:00:00+08:00",
        },
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }


def _common_mocks(mod, classify=("bug", None), multica=("m-1", "SKI-1")):
    """创建路径公共 mock 栈: 主流程放行, 关键分流函数断言不调用。"""
    stack = ExitStack()
    patchers = [
        mock.patch.object(mod, "db_find_requirement_by_issue_id", return_value=None),
        mock.patch.object(mod, "classify_issue", return_value=classify),
        mock.patch.object(mod, "_dup_check_issue", return_value=None),
        mock.patch.object(mod, "_issue_has_bot_comment", return_value=False),
        mock.patch.object(mod, "_repo_has_skill", return_value=True),
        mock.patch.object(mod, "issue_quality_ok", return_value=(True, "ok")),
        mock.patch.object(mod, "multica_issue_create", return_value=multica),
        mock.patch.object(mod, "db_insert_requirement", return_value=1),
        mock.patch.object(mod, "db_insert_flow", return_value=None),
        mock.patch.object(mod, "ensure_labels", return_value=None),
        mock.patch.object(mod, "set_issue_labels", return_value=None),
        mock.patch.object(mod, "gitcode_issue_comment", return_value=1),
        mock.patch.object(mod, "gitcode_get", return_value=[]),
        # ---- 关键断言: 分流函数绝不能被调用 ----
        mock.patch.object(mod, "_skill_has_bot_contributor",
                          side_effect=AssertionError("不应再按贡献者分流")),
        mock.patch.object(mod, "codeowner_assign_issue",
                          side_effect=AssertionError("bug/优化不应走 codeowner 分配")),
    ]
    for p in patchers:
        stack.enter_context(p)
    return stack


# ===== bug + 仓库有 skill(人工开发) -> 直接建任务 =====
def test_bug_create_always_auto_fix():
    mod = _load_handler()
    payload = _issue_payload(
        title="huawei-cloud-xxx-list 查询报错",
        body="huawei-cloud-xxx-list 调用时返回 500 错误, 复现步骤: 1. 执行命令 2. 报错, 日志见附件")
    with _common_mocks(mod):
        r = mod.handle_event(payload)
    assert r["action"] == "created", f"bug 应直接建任务, 实际 {r}"
    assert r.get("multica_issue_id") == "m-1"
    assert r.get("label") == "bug"


# ===== bug + 限流(仓库存在性无法确认) -> 降级直接修复 =====
def test_bug_create_ratelimit_fallback_auto_fix():
    mod = _load_handler()
    payload = _issue_payload(
        title="huawei-cloud-xxx-list 查询报错",
        body="huawei-cloud-xxx-list 调用时返回 500 错误, 复现步骤: 1. 执行命令 2. 报错, 日志见附件")
    with _common_mocks(mod):
        # 覆盖 _repo_has_skill: 精确 None + 宽松 None(限流无法确认)
        with mock.patch.object(mod, "_repo_has_skill", return_value=None):
            r = mod.handle_event(payload)
    assert r["action"] == "created", f"限流降级应直接修复, 实际 {r}"


# ===== 优化类 + 单 skill 名(人工开发) -> 直接进机器人开发流水线 =====
def test_opt_create_always_auto_fix():
    mod = _load_handler()
    payload = _issue_payload(
        title="优化 huawei-cloud-xxx-list 查询速度",
        body="需要优化 huawei-cloud-xxx-list 的查询速度, 当前响应慢, 希望支持分页和缓存")
    with _common_mocks(mod, classify=("feature", None)):
        with mock.patch.object(mod, "has_optimization_intent", return_value=True), \
             mock.patch.object(mod, "extract_skill_names",
                               return_value=["huawei-cloud-xxx-list"]), \
             mock.patch.object(mod, "is_optimization_request", return_value=True):
            r = mod.handle_event(payload)
    assert r["action"] == "created", f"优化类应直接进开发流水线, 实际 {r}"
    assert r.get("label") == "feature"


# ===== update 路径(need_info 补充后升级) bug -> 直接建任务 =====
def test_bug_update_always_auto_fix():
    mod = _load_handler()
    payload = _issue_payload(
        action="update",
        title="huawei-cloud-xxx-list 查询报错",
        body="huawei-cloud-xxx-list 调用时返回 500 错误, 复现步骤: 1. 执行命令 2. 报错, 日志见附件")
    with _common_mocks(mod):
        # update 路径: 已有 need_info 记录(无 multica 任务) -> 重新评估
        with mock.patch.object(
                mod, "db_find_requirement_by_issue_id",
                return_value={"id": 55, "status": "need_info",
                              "multica_issue_id": None, "title": "t", "body": "b"}):
            r = mod.handle_event(payload)
    assert r["event"] == "issue_updated", r
    assert r.get("multica_issue_id") == "m-1", f"update 路径 bug 应建任务, 实际 {r}"


# ===== extract_skill_names 过滤问题单编号(#113 实测) =====
def test_extract_skill_names_filters_issue_number():
    mod = _load_handler()
    names = mod.extract_skill_names(
        "[huawei-cloud-monitoring-query]基本功评测发现1条问题",
        "# 问题单: huawei-cloud-monitoring-query\n"
        "编号: huawei-cloud-monitoring-query-ISSUE-001\n"
        "skill: huawei-cloud-monitoring-query")
    assert names == ["huawei-cloud-monitoring-query"], \
        f"问题编号不应被提取为 skill 名, 实际 {names}"


# ===== bug + 正文含优化词(改进/修复) + 多个疑似 skill 名 -> 不被 2.1.3 劫持(#113) =====
def test_bug_with_opt_words_skips_opt_split():
    mod = _load_handler()
    payload = _issue_payload(
        title="[huawei-cloud-monitoring-query]基本功评测发现1条问题",
        body="# 问题单: huawei-cloud-monitoring-query\n"
             "查询RDS监控数据时报错, 复现步骤: 1. 执行查询 2. 报错。\n"
             "修复建议: 改进拒绝逻辑, 穷举不支持的服务列表。\n"
             "编号: huawei-cloud-monitoring-query-ISSUE-001\n"
             "环境: 华为云测试环境")
    with _common_mocks(mod):
        # 模拟优化意图命中 + 多 skill 名(旧逻辑会 opt_need_skill_name 拦截)
        with mock.patch.object(mod, "has_optimization_intent", return_value=True), \
             mock.patch.object(mod, "extract_skill_names",
                               return_value=["huawei-cloud-monitoring-query",
                                             "huawei-cloud-monitoring-query-issue-001"]):
            r = mod.handle_event(payload)
    assert r["action"] == "created", \
        f"bug 即使正文含优化词也不应被 2.1.3 劫持, 实际 {r}"
    assert r.get("label") == "bug"


# ===== bug 类型 issue 入库责任人 = GITCODE_PR_ASSIGNEE(2026-08-16 e3a73bb 移植) =====
def test_bug_author_overridden_by_pr_assignee():
    mod = _load_handler()
    payload = _issue_payload(
        title="huawei-cloud-xxx-list 查询报错",
        body="huawei-cloud-xxx-list 调用时返回 500 错误, 复现步骤: 1. 执行命令 2. 报错, 日志见附件")
    with _common_mocks(mod):
        with mock.patch.dict(os.environ, {"GITCODE_PR_ASSIGNEE": "lccode"}), \
             mock.patch.object(mod, "db_insert_requirement",
                               return_value=1) as m_ins:
            r = mod.handle_event(payload)
    assert r["action"] == "created", f"bug 应建任务, 实际 {r}"
    assert m_ins.call_args_list, "应调用 db_insert_requirement"
    call = m_ins.call_args_list[0]
    kwargs = call[0][0] if call[0] else call[1]
    assert kwargs.get("author") == "lccode", \
        f"bug 类型 issue 入库责任人应覆盖为 GITCODE_PR_ASSIGNEE, 实际 {kwargs.get('author')}"
