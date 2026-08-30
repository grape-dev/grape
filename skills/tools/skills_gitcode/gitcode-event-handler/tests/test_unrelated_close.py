"""完全无关 issue 质量不达标直接关闭不追问不入库测试(2026-08-13 #125)。

背景: 「上海天气很好」灌水 issue 因描述短(<30)先被质量检查追问
(need-info), 用户补充后才走 2.1.4 无关关闭, 且被 need-info 路径错误入库。
修复: 三处质量检查失败分支先判"完全无关"(huawei_related_level=none 且
非优化类) -> 直接 invalid 关闭, 不追问不入库(已有记录一并清理)。
"""
import importlib.util
from contextlib import ExitStack
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _issue_payload(action="open", number=125, issue_id=4248582, title="t", body="b"):
    return {
        "object_kind": "issue",
        "object_attributes": {
            "id": issue_id, "iid": number, "title": title,
            "description": body, "action": action, "state": "opened",
            "author": {"username": "lccode", "name": "lccode"},
        },
        "issue": {
            "id": issue_id, "iid": number, "number": number, "title": title,
            "body": body, "state": "opened",
            "url": f"https://gitcode.com/g30074593/skillspackage/issues/{number}",
            "author": {"username": "lccode", "name": "lccode"},
            "created_at": "2026-08-13T11:00:00+08:00",
            "updated_at": "2026-08-13T11:00:00+08:00",
        },
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }


def _base_mocks(mod):
    stack = ExitStack()
    patchers = [
        mock.patch.object(mod, "db_find_requirement_by_issue_id", return_value=None),
        mock.patch.object(mod, "classify_issue", return_value=("feature", None)),
        mock.patch.object(mod, "_dup_check_issue", return_value=None),
        mock.patch.object(mod, "_issue_has_bot_comment", return_value=False),
        mock.patch.object(mod, "issue_quality_ok",
                          return_value=(False, "实质描述仅 6 字(<30), 需求描述不够清晰")),
        mock.patch.object(mod, "set_issue_labels", return_value=None),
        mock.patch.object(mod, "close_gitcode_issue", return_value=None),
        mock.patch.object(mod, "gitcode_issue_comment", return_value=1),
        mock.patch.object(mod, "gitcode_get", return_value=[]),
        # 不入库断言
        mock.patch.object(mod, "db_insert_requirement",
                          side_effect=AssertionError("无关 issue 不应入库")),
        mock.patch.object(mod, "db_insert_flow",
                          side_effect=AssertionError("无关 issue 不应写 flow")),
    ]
    for p in patchers:
        stack.enter_context(p)
    return stack


# ===== 通用 codeowner 分支(标题无 skill 词): 无关+短描述 -> 直接关闭(#125 真实路径) =====
def test_unrelated_short_desc_codeowner_branch_closes():
    mod = _load_handler()
    payload = _issue_payload(title="上海天气很好", body="上海天气很好")
    with _base_mocks(mod):
        # 真实判定: is_skill_task=False(无 skill 词), huawei_related_level=none
        r = mod.handle_event(payload)
    assert r["action"] == "invalid_unrelated", f"无关短描述应直接关闭, 实际 {r}"
    assert r.get("requirement_id") is None


# ===== 通用 codeowner 分支: 无关+长描述(质量通过) -> 直接关闭(#126 真实路径) =====
def test_unrelated_long_desc_codeowner_branch_closes():
    mod = _load_handler()
    payload = _issue_payload(
        title="今天天气怎么样",
        body="今天天气怎么样今天天气怎么样今天天气怎么样今天天气怎么样"
             "今天天气怎么样今天天气怎么样今天天气怎么样")
    with _base_mocks(mod):
        # 49 字质量检查通过, 但完全无关 -> 也应直接关闭(旧逻辑打标签+skip 评论)
        with mock.patch.object(mod, "issue_quality_ok", return_value=(True, "ok")):
            r = mod.handle_event(payload)
    assert r["action"] == "invalid_unrelated", \
        f"无关长描述(质量通过)也应直接关闭, 实际 {r}"
    assert r.get("requirement_id") is None


# ===== 主路径质量检查(is_skill_task=True): 无关+短描述 -> 直接关闭 =====
def test_unrelated_short_desc_main_quality_closes():
    mod = _load_handler()
    payload = _issue_payload(
        title="帮我开发一个查询上海天气的skill",
        body="查询上海天气")
    with _base_mocks(mod):
        # 已存在检查三件套(主路径质量检查前)mock 放行
        with mock.patch.object(mod, "find_skill_in_repo", return_value=(None, None)), \
             mock.patch.object(mod, "find_skill_in_open_prs",
                               return_value=(False, None, None)), \
             mock.patch.object(mod, "is_skill_modification", return_value=False):
            r = mod.handle_event(payload)
    assert r["action"] == "invalid_unrelated", f"无关短描述应直接关闭, 实际 {r}"


# ===== update 路径: 无关+短描述+已有记录 -> 直接关闭且清理旧记录 =====
def test_unrelated_update_closes_and_cleans_db():
    mod = _load_handler()
    payload = _issue_payload(
        action="update",
        title="上海天气很好",
        body="上海天气很好")
    with _base_mocks(mod):
        with mock.patch.object(
                mod, "db_find_requirement_by_issue_id",
                return_value={"id": 37, "status": "need_info",
                              "multica_issue_id": None, "title": "上海天气很好",
                              "body": "上海天气很好"}) as m_find, \
             mock.patch.object(mod, "db_update_requirement", return_value=None) as m_upd, \
             mock.patch.object(mod, "db_connect") as m_db:
            r = mod.handle_event(payload)
    assert r["action"] == "invalid_unrelated", f"update 无关短描述应直接关闭, 实际 {r}"
    assert m_upd.call_args_list, "应同步 body 后关闭"
    # 断言调用了 _close_unrelated_issue 且 req_id 清理(通过 db_connect 执行 DELETE)
    assert m_db.call_args_list, "应连接 DB 清理旧记录"
