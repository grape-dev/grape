"""逻辑4 PR 未匹配 issue 时不再按标题复用 multica issue 测试(2026-08-16 用户要求)。

背景: PR #5/#13 同分支同 commit 同标题, 旧逻辑 _find_existing_multica_issue
按标题全等复用同一个 SKI-67, 导致两个 PR 的 requirement 记录共享一个
multica 任务, 处理互相干扰。用户要求: 即使 Multica 已有同标题 issue,
每个 PR 也必须建独立任务(force_new=True, --allow-duplicate 两层放行)。
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


def _pr_payload(number=13, title="docs: 为 _get_ak_sk 函数增加环境变量读取说明注释",
                pr_id=9041596):
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "id": pr_id, "iid": number, "number": number, "title": title,
            "description": title, "action": "open", "state": "opened",
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


# ===== 即使 Multica 已有同标题 issue, 也必须 force_new 新建独立任务 =====
def test_logic4_no_reuse_same_title_multica():
    mod = _load_handler()
    payload = _pr_payload()
    with _common_mocks(mod):
        # Multica 已有同标题 SKI-67(模拟 PR #5 创建的任务)
        with mock.patch.object(
                mod, "_find_existing_multica_issue",
                return_value=("fd86dc96-b47a-4874-989a-c85d3cc4828a", "SKI-67")) as m_find, \
             mock.patch.object(
                mod, "multica_issue_create",
                return_value=("new-issue-001", "SKI-68")) as m_create, \
             mock.patch.object(
                mod, "db_insert_requirement",
                return_value=12) as m_ins:
            r = mod.handle_event(payload)
    # 必须新建独立任务, 不复用 SKI-67
    assert r["action"] == "created_new", str(r)
    assert r.get("multica_issue_id") == "new-issue-001"
    assert r.get("multica_issue_number") == "SKI-68"
    m_create.assert_called_once()
    # force_new 必须为 True
    call = m_create.call_args_list[0]
    kwargs = call[1]
    assert kwargs.get("force_new") is True, \
        f"multica_issue_create 必须 force_new=True, 实际 {kwargs}"
    # 入库用新任务的 id
    ins_call = m_ins.call_args_list[0]
    ins_kwargs = ins_call[0][0] if ins_call[0] else ins_call[1]
    assert ins_kwargs.get("multica_issue_id") == "new-issue-001"
    assert ins_kwargs.get("multica_issue_number") == "SKI-68"
    assert ins_kwargs.get("gitcode_pr_id") == "13"


# ===== force_new 时 multica_issue_create 给 CLI 加 --allow-duplicate =====
def test_multica_create_force_new_adds_allow_duplicate():
    mod = _load_handler()
    with mock.patch.object(
            mod, "_multica_cli",
            return_value=(0, '{"id": "x", "number": 1, "identifier": "SKI-9"}', "")) as m_cli:
        iid, ident = mod.multica_issue_create("t", "b", "squad", force_new=True)
    assert iid == "x"
    assert "--allow-duplicate" in m_cli.call_args_list[0][0][0], \
        f"force_new 应加 --allow-duplicate, 实际 {m_cli.call_args_list[0][0][0]}"


# ===== 非 force_new 默认仍走复用兜底(其他调用方不受影响) =====
def test_multica_create_no_force_new_keeps_fallback():
    mod = _load_handler()
    with mock.patch.object(
            mod, "_multica_cli",
            return_value=(1, "Active duplicate issue exists: SKI-67", "")) as m_cli, \
         mock.patch.object(
            mod, "_find_existing_multica_issue",
            return_value=("fd86dc96-b47a-4874-989a-c85d3cc4828a", "SKI-67")) as m_find:
        iid, ident = mod.multica_issue_create("t", "b", "squad", force_new=False)
    assert iid == "fd86dc96-b47a-4874-989a-c85d3cc4828a", "非 force_new 应复用已有任务"
    assert ident == "SKI-67"
    m_find.assert_called_once()


# ===== force_new=True 撞 duplicate 不再复用, 直接抛错(由调用方处理) =====
def test_multica_create_force_new_duplicate_no_reuse():
    mod = _load_handler()
    with mock.patch.object(
            mod, "_multica_cli",
            return_value=(1, "Active duplicate issue exists: SKI-67", "")), \
         mock.patch.object(
            mod, "_find_existing_multica_issue",
            side_effect=AssertionError("force_new 不应走复用")) as m_find:
        try:
            mod.multica_issue_create("t", "b", "squad", force_new=True)
            raised = False
        except Exception:
            raised = True
    assert raised, "force_new=True 撞 duplicate 应抛错(不静默复用)"
    m_find.assert_not_called()
