"""bug 类 issue 判断 skill 是否已有 open PR 在处理测试(2026-08-16 用户要求)。

新增 2.2.2 ③: 识别为 bug 类 issue 且确认 skill 存在(进入机器人修复流水线
之前), 先判断该 skill 是否已有 **open 状态**的 PR 在处理:
- 有 -> 评论"该 skill 已有 PR 在处理中" + 关闭 issue, 不入库不建任务
  (对齐判重关闭语义, 避免重复修复)
- 无 -> 继续走修复流水线(建 Multica 任务)
- open PR 列表查询失败(限流/异常) -> fail-open 不阻断, 继续修复流水线
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


def _issue_payload(kind="issue", number=300, issue_id=999300, title="t", body="b",
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
    """创建路径公共 mock 栈: 主流程放行, 关键分流函数可覆盖。"""
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
        mock.patch.object(mod, "close_gitcode_issue", return_value=True),
        mock.patch.object(mod, "gitcode_get", return_value=[]),
    ]
    for p in patchers:
        stack.enter_context(p)
    return stack


BUG_TITLE = "huawei-cloud-xxx-list 查询报错"
BUG_BODY = ("huawei-cloud-xxx-list 调用时返回 500 错误, 复现步骤: "
            "1. 执行命令 2. 报错, 日志见附件")


# ===== 有 open PR 处理该 skill -> 评论 + 关闭, 不建任务 =====
def test_bug_skill_has_open_pr_close_no_task():
    mod = _load_handler()
    payload = _issue_payload(title=BUG_TITLE, body=BUG_BODY)
    with _common_mocks(mod):
        with mock.patch.object(
                mod, "_bug_skill_open_pr",
                return_value=(101, "add skill: xxx-list")) as m_check, \
             mock.patch.object(mod, "multica_issue_create",
                               side_effect=AssertionError("不应建任务")) as m_create, \
             mock.patch.object(mod, "gitcode_issue_comment",
                               return_value=1) as m_comment, \
             mock.patch.object(mod, "close_gitcode_issue",
                               return_value=True) as m_close:
            r = mod.handle_event(payload)
    assert r["action"] == "bug_pr_in_progress", f"实际 {r}"
    assert r.get("requirement_id") is None, "命中 open PR 不应入库"
    m_check.assert_called_once()
    # 评论应包含 PR 链接
    call = m_comment.call_args_list[0]
    body_text = call[0][3] if len(call[0]) > 3 else str(call)
    assert "已有 PR 在处理中" in body_text
    assert "pull/101" in body_text
    m_close.assert_called_once()


# ===== 无 open PR -> 继续走修复流水线(建任务) =====
def test_bug_skill_no_open_pr_continue_fix():
    mod = _load_handler()
    payload = _issue_payload(title=BUG_TITLE, body=BUG_BODY)
    with _common_mocks(mod):
        with mock.patch.object(mod, "_bug_skill_open_pr", return_value=None) as m_check:
            r = mod.handle_event(payload)
    assert r["action"] == "created", f"无 open PR 应继续修复流水线, 实际 {r}"
    assert r.get("multica_issue_id") == "m-1"
    m_check.assert_called_once()


# ===== _bug_skill_open_pr 单测: 命中(路径含 skill 目录) =====
def test_bug_skill_open_pr_hit():
    mod = _load_handler()
    prs = [
        {"number": 11, "title": "add skill: dws"},
        {"number": 12, "title": "update skill: xxx-list"},
    ]
    files_by_pr = {
        11: [{"filename": "skills/bigdata/dws/huawei-cloud-dws-list/SKILL.md"}],
        12: [{"filename": "skills/storage/sfsturbo/huawei-cloud-xxx-list/scripts/run.sh"}],
    }
    def fake_get(url):
        if url.endswith("/pulls?state=open&per_page=100"):
            return prs
        pr_num = int(url.split("/pulls/")[1].split("/files")[0])
        return files_by_pr.get(pr_num, [])
    with mock.patch.object(mod, "gitcode_get", side_effect=fake_get):
        hit = mod._bug_skill_open_pr("g30074593/skillspackage", "huawei-cloud-xxx-list")
    assert hit == (12, "update skill: xxx-list"), f"应命中 PR #12, 实际 {hit}"


# ===== _bug_skill_open_pr 单测: 仅 open PR, 无命中 =====
def test_bug_skill_open_pr_miss():
    mod = _load_handler()
    prs = [{"number": 11, "title": "add skill: dws"}]
    def fake_get(url):
        if url.endswith("/pulls?state=open&per_page=100"):
            return prs
        return [{"filename": "skills/bigdata/dws/huawei-cloud-dws-list/SKILL.md"}]
    with mock.patch.object(mod, "gitcode_get", side_effect=fake_get):
        hit = mod._bug_skill_open_pr("g30074593/skillspackage", "huawei-cloud-xxx-list")
    assert hit is None, f"不应命中, 实际 {hit}"


# ===== _bug_skill_open_pr 单测: 查询失败返回 None =====
def test_bug_skill_open_pr_query_fail():
    mod = _load_handler()
    with mock.patch.object(mod, "gitcode_get", side_effect=RuntimeError("429")):
        hit = mod._bug_skill_open_pr("g30074593/skillspackage", "huawei-cloud-xxx-list")
    assert hit is None, "查询失败应返回 None(fail-open)"


# ===== _bug_skill_open_pr 单测: 无 repo/skill 参数 =====
def test_bug_skill_open_pr_no_args():
    mod = _load_handler()
    assert mod._bug_skill_open_pr(None, "huawei-cloud-xxx-list") is None
    assert mod._bug_skill_open_pr("g30074593/skillspackage", "") is None
