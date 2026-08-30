"""PR 扫描后自动设置评审人测试(2026-08-12 用户要求)。

分流规则(设计文档 docs/superpowers/specs/2026-08-12-pr-reviewer-assign-design.md):
- 非 bot 作者(开发者 PR) -> DEFAULT_REVIEWER(胡盖/HugLovesWine)
- bot 作者 -> 按关联 issue 标签: bug=skill 贡献者 / feature=issue 创建人;
  无 feature/bug 标签静默跳过; 找不到评审人回退 DEFAULT_REVIEWER
- 目标非 collaborator 回退 DEFAULT_REVIEWER; 目标==作者跳过; 幂等跳过
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pr_detail(author, reviewers=None):
    """构造 GET /pulls/{n} 的返回(作者 + 现有评审人)。"""
    return {"user": {"login": author},
            "approval_reviewers": reviewers or []}


def _run(mod, author, issue_labels=None, issue_creator=None, contributor=None,
         reviewers=None, collabs=None, skill=None):
    """执行 _assign_pr_reviewer, 返回 (result, post_calls)。"""
    post_calls = []

    def fake_post(path, body):
        post_calls.append((path, body))
        return []

    with mock.patch.object(mod, "gitcode_get",
                           side_effect=lambda path: (
                               _pr_detail(author, reviewers) if "/pulls/" in path
                               else ({"user": {"login": issue_creator},
                                      "labels": issue_labels or []}
                                     if "/issues/" in path
                                     else ({"login": x} for x in (collabs or []))
                                     if "/collaborators" in path
                                     else {})), autospec=True), \
         mock.patch.object(mod, "gitcode_post", side_effect=fake_post), \
         mock.patch.object(mod, "_repo_collaborators",
                           return_value=set(collabs or [])), \
         mock.patch.object(mod, "_api_issue_labels",
                           return_value=issue_labels or []), \
         mock.patch.object(mod, "extract_skill_name_from_pr",
                           return_value=skill), \
         mock.patch.object(mod, "_skill_primary_contributor",
                           return_value=contributor):
        result = mod._assign_pr_reviewer(
            "g30074593/skillspackage", 40, "", issue_no=102,
            title="add skill: compute/huawei-cloud-computing-query", body="")
    return result, post_calls


# ===== 0c. 超限 PR 跳过审查时仍设置评审人(2026-08-12 PR #41 教训) =====
def test_oversized_pr_still_assigns_reviewer():
    """PR #41 实测: 219 文件超限(>50)跳过审查, 评审人跟着空 —— 评审人与
    审查解耦: 超限分支也调用 _assign_pr_reviewer。"""
    mod = _load_handler()
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "id": 41, "iid": 41, "action": "open", "state": "opened",
            "title": "fix: huawei-cloud-network-query 修复基本功评测发现的2个问题",
            "description": "Fixes #104",
        },
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(mod, "check_pr_file_limit", return_value=True), \
         mock.patch.object(mod, "extract_issue_number_from_pr",
                           return_value=104), \
         mock.patch.object(mod, "_assign_pr_reviewer",
                           return_value=True) as m_rev:
        r = mod.handle_event(payload)
    assert r["event"] == "pr_created" and r["action"] == "skipped", r
    m_rev.assert_called_once()
    assert m_rev.call_args.kwargs.get("issue_no") == 104, \
        "超限分支也应把关联 issue 传给评审人设置"


# ===== 1. 开发者 PR(非 bot) -> 固定 DEFAULT_REVIEWER =====
def test_dev_pr_assigns_default_reviewer():
    mod = _load_handler()
    result, calls = _run(mod, "lccode", collabs=["hugloveswine", "lccode"])
    assert result is True, result
    assert calls and calls[0][0].endswith("/reviewers"), calls
    assert calls[0][1] == {"reviewers": "HugLovesWine"}, calls[0][1]


# ===== 2. bot PR + bug 标签 + 有贡献者 -> 设置贡献者 =====
def test_bot_bug_assigns_contributor():
    mod = _load_handler()
    result, calls = _run(mod, "skills_bot", issue_labels=["bug", "triaged"],
                         contributor="Jason7602", collabs=["jason7602"],
                         skill="huawei-cloud-computing-query")
    assert result is True
    assert calls[0][1] == {"reviewers": "Jason7602"}, calls[0][1]


# ===== 3. bot PR + bug 标签 + 无贡献者 -> 回退 DEFAULT_REVIEWER =====
def test_bot_bug_no_contributor_falls_back():
    mod = _load_handler()
    result, calls = _run(mod, "skills_bot", issue_labels=["bug"],
                         contributor=None, collabs=["hugloveswine"],
                         skill="huawei-cloud-computing-query")
    assert result is True
    assert calls[0][1] == {"reviewers": "HugLovesWine"}, calls[0][1]


# ===== 4. bot PR + feature 标签 -> 设置 issue 创建人 =====
def test_bot_feature_assigns_issue_creator():
    mod = _load_handler()
    result, calls = _run(mod, "skills_bot", issue_labels=["feature", "triaged"],
                         issue_creator="wj911831", collabs=["wj911831"])
    assert result is True
    assert calls[0][1] == {"reviewers": "wj911831"}, calls[0][1]


# ===== 5. bot PR + 无 feature/bug 标签 -> 静默跳过(不调 POST) =====
def test_bot_no_feature_bug_label_skips():
    mod = _load_handler()
    result, calls = _run(mod, "skills_bot", issue_labels=["triaged"])
    assert result is False
    assert calls == [], "无标签不应设置评审人"


# ===== 6. 目标非 collaborator -> 回退 DEFAULT_REVIEWER =====
def test_target_not_collaborator_falls_back():
    mod = _load_handler()
    # 贡献者 Jason7602 不在 skillspackage collaborator -> 回退胡盖
    result, calls = _run(mod, "skills_bot", issue_labels=["bug"],
                         contributor="Jason7602", collabs=["hugloveswine"],
                         skill="huawei-cloud-computing-query")
    assert result is True
    assert calls[0][1] == {"reviewers": "HugLovesWine"}, calls[0][1]


# ===== 7. 目标 == PR 作者 -> 跳过(API 硬限制) =====
def test_target_is_author_skips():
    mod = _load_handler()
    # 胡盖自己提交 PR(开发者 PR), 目标=胡盖=作者 -> 跳过
    result, calls = _run(mod, "HugLovesWine", collabs=["hugloveswine"])
    assert result is False
    assert calls == []


# ===== 8. 幂等: approval_reviewers 已含目标 -> 跳过 =====
def test_already_reviewer_skips():
    mod = _load_handler()
    result, calls = _run(mod, "lccode", reviewers=[{"login": "HugLovesWine"}],
                         collabs=["hugloveswine"])
    assert result is True  # 已存在视为成功(幂等)
    assert calls == [], "已含目标不应重复设置"


# ===== 9. POST 失败 -> 静默容忍(不抛异常) =====
def test_post_failure_tolerated():
    mod = _load_handler()

    def fake_post(path, body):
        raise RuntimeError("HTTP 400: author cannot operate")

    with mock.patch.object(mod, "gitcode_get",
                           side_effect=lambda path: _pr_detail("lccode")), \
         mock.patch.object(mod, "gitcode_post", side_effect=fake_post), \
         mock.patch.object(mod, "_repo_collaborators",
                           return_value={"hugloveswine", "lccode"}), \
         mock.patch.object(mod, "_api_issue_labels", return_value=[]):
        result = mod._assign_pr_reviewer(
            "g30074593/skillspackage", 40, "", issue_no=102, title="t", body="")
    assert result is False  # 失败返回 False, 不抛异常


# ===== 10. 关闭开关 -> 直接跳过 =====
def test_disabled_skips():
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_PR_REVIEWER_ASSIGN", False):
        result, calls = _run(mod, "lccode", collabs=["hugloveswine"])
    assert result is False
    assert calls == []
