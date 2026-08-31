"""extract_issue_number_from_pr DB 反查兜底测试(2026-08-11)。"""
import importlib.util
from unittest.mock import patch

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== body 未写关联 -> DB 反查 =====
def test_db_fallback_finds_issue():
    mod = _load_handler()
    pr = {"body": "New skill: huawei-cloud-ecs-list, no related issue mentioned",
          "title": "add skill: compute/ecs/huawei-cloud-ecs-list",
          "close_related_issue": True}  # True 非数字, 不命中
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92",
                                    "multica_issue_id": "SKI-10"}):
        num = mod.extract_issue_number_from_pr(pr, "g30074593/skillspackage", 38)
    assert num == 92, "body 未写关联时应从 DB 反查"

    # 确认 DB 是按 PR 编号 + 仓库查询的
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92"}) as m:
        mod.extract_issue_number_from_pr(pr, "g30074593/skillspackage", 38)
        m.assert_called_once_with(38, "g30074593/skillspackage")


# ===== body 已写关联 -> 优先用 body, 不走 DB =====
def test_body_relation_wins():
    mod = _load_handler()
    pr = {"body": "Closes #77", "title": "add skill: x"}
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92"}) as m:
        num = mod.extract_issue_number_from_pr(pr, "repo", 1)
    assert num == 77, "body 有关联时应优先用 body"
    m.assert_not_called(), "body 已命中不应走 DB"


# ===== close_related_issue 数字编号优先 =====
def test_close_related_issue_number_wins():
    mod = _load_handler()
    pr = {"body": "nothing", "close_related_issue": 55}
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92"}) as m:
        num = mod.extract_issue_number_from_pr(pr, "repo", 1)
    assert num == 55
    m.assert_not_called()


# ===== close_related_issue=True(布尔) 不当编号(#38 实测回归) =====
def test_close_related_issue_bool_not_treated_as_number():
    mod = _load_handler()
    pr = {"body": "no relation", "close_related_issue": True}
    with patch.object(mod, "db_find_requirement_by_pr_id", return_value=None):
        num = mod.extract_issue_number_from_pr(pr, "repo", 38)
    assert num is None, "布尔 True 不能误判为 issue #1"

    # 若 DB 有记录, 应走 DB 反查拿到真实编号
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92"}):
        num2 = mod.extract_issue_number_from_pr(pr, "repo", 38)
    assert num2 == 92, "布尔开关应跳过, 继续走 body 正则+DB 反查"


# ===== DB 无记录 -> None =====
def test_db_no_record_returns_none():
    mod = _load_handler()
    pr = {"body": "no relation", "title": "add skill: x"}
    with patch.object(mod, "db_find_requirement_by_pr_id", return_value=None):
        num = mod.extract_issue_number_from_pr(pr, "g30074593/skillspackage", 99)
    assert num is None


# ===== 未传 repo/pr_number(旧调用) -> 不反查, 保持 None =====
def test_no_repo_skips_db():
    mod = _load_handler()
    pr = {"body": "no relation", "title": "add skill: x"}
    with patch.object(mod, "db_find_requirement_by_pr_id",
                      return_value={"gitcode_issue_number": "92"}) as m:
        num = mod.extract_issue_number_from_pr(pr)  # 无 repo/pr_number
    assert num is None
    m.assert_not_called(), "未传 repo/pr_number 不应走 DB"
