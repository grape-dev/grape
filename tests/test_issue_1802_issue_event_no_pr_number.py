"""Issue #1802: Issue 事件不得把 Issue 编号注入 PR_NUMBER

用户反馈 3342：issue_opened（Issue #43）事件被注入 PR_NUMBER=43，但仓库最大
PR 为 !40，PR !43 不存在。根因：`_extract_pr_number` 在非 note_hook_pr 分支的
候选链末尾以 `payload.object_attributes.iid` 兜底，而 Issue Hook 的
object_attributes 就是该 Issue 自身，iid 即 Issue 编号。

修复口径：按事件实体类型设准入闸门——只有 PR/MR 实体事件才允许从
object_attributes / pull_request / merge_request 取编号；Issue 实体的
object_attributes.iid 永不作 PR 号来源。无真实关联 PR 时静默不注入 PR_NUMBER；
仅 PR 实体解析失败保留 PR_NUMBER_PARSE_FAILED=1。

验收（对应 Spec 评论 id 199601775）：
1. issue_opened / issue_updated / issue_comment 无 PR_NUMBER，且 != ISSUE_NUMBER。
2. PR 事件（含 object_attributes.iid）仍注入真实 PR 号；note_hook_pr 取 noteable 所属 PR。
3. PR 事件解析失败：不注入 PR_NUMBER，置 PR_NUMBER_PARSE_FAILED=1；Issue 事件不误报。
4. A2A payload 无 PR 时 pr_number 为空串，不回落 issue_number。
"""
from src.api.event_pipeline import (
    _extract_pr_number,
    _inject_issue_pr_env,
    _is_pr_entity_event,
)

_TEST_REPO = "gitcode:owner/repo"


def _issue_evt(event_type="issue_opened", issue_id="#1802", payload=None):
    return {
        "type": event_type,
        "issue_id": issue_id,
        "title": "PR_NUMBER 环境变量注入了不存在的 PR 编号",
        "payload": payload if payload is not None else {},
    }


class TestIssueEntityNoPrNumber:
    """Issue 实体事件：object_attributes.iid 不得成为 PR 号。"""

    def test_issue_opened_iid_not_used_as_pr_number(self):
        env = {}
        _inject_issue_pr_env(env, _issue_evt(payload={
            "object_attributes": {
                "iid": 1802,
                "url": "https://gitcode.com/owner/repo/issues/1802",
            },
        }), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1802"
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER") != env.get("ISSUE_NUMBER")

    def test_issue_updated_iid_not_used_as_pr_number(self):
        env = {}
        _inject_issue_pr_env(env, _issue_evt(
            event_type="issue_updated",
            payload={"object_attributes": {"iid": 1802, "action": "update"}},
        ), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1802"
        assert "PR_NUMBER" not in env

    def test_issue_comment_noteable_iid_not_used_as_pr_number(self):
        """Issue 评论（issue_comment）的 noteable_iid 是 Issue 编号，不是 PR。"""
        env = {}
        _inject_issue_pr_env(env, _issue_evt(
            event_type="issue_comment",
            payload={"object_attributes": {
                "noteable_type": "Issue",
                "noteable_iid": 1802,
                "iid": 99001,
            }},
        ), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1802"
        assert "PR_NUMBER" not in env

    def test_issue_event_no_parse_failed_warning(self):
        """Issue 事件本就不该有 PR → 静默，不写 PR_NUMBER_PARSE_FAILED 告警。"""
        env = {}
        _inject_issue_pr_env(env, _issue_evt(payload={
            "object_attributes": {"iid": 1802},
        }), _TEST_REPO)
        assert "PR_NUMBER_PARSE_FAILED" not in env

    def test_extract_returns_empty_for_issue_entity(self):
        pr_num, source = _extract_pr_number(
            _issue_evt(payload={"object_attributes": {"iid": 1802}}),
            {"object_attributes": {"iid": 1802}},
            "issue_opened",
        )
        assert (pr_num, source) == ("", "")

    def test_pr_entity_gate(self):
        assert _is_pr_entity_event("pull_request_opened", {}) is True
        assert _is_pr_entity_event("note_hook_pr", {}) is True
        assert _is_pr_entity_event("issue_opened", {}) is False
        assert _is_pr_entity_event("issue_comment", {
            "object_attributes": {"noteable_type": "Issue"}}) is False
        assert _is_pr_entity_event("legacy_event", {
            "merge_request": {"iid": 7}}) is True


class TestPrEntityStillInjects:
    """PR/MR 实体事件仍能取到真实 PR 号（不回归 #528/#595/#1579）。"""

    def test_pull_request_number_injected(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "pull_request_opened", "issue_id": "#1802",
            "payload": {"pull_request": {"number": 1803}},
        }, _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1802"
        assert env.get("PR_NUMBER") == "1803"

    def test_pull_request_object_attributes_iid_injected(self):
        """PR 实体的 object_attributes.iid 是 PR 号，仍可注入（#595 语义保留）。"""
        env = {}
        _inject_issue_pr_env(env, {
            "type": "pull_request_updated", "issue_id": "#1802",
            "payload": {"object_attributes": {"iid": 1803}},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1803"

    def test_merge_request_iid_injected(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "pull_request_merged", "issue_id": "#1802",
            "payload": {"merge_request": {"iid": 1803}},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1803"

    def test_note_hook_pr_uses_noteable_pr(self):
        """note_hook_pr 仍取 noteable 所属 PR，而非评论自身 id。"""
        env = {}
        _inject_issue_pr_env(env, {
            "type": "note_hook_pr", "issue_id": "#1802",
            "payload": {"object_attributes": {
                "noteable_type": "MergeRequest",
                "noteable_iid": 1803,
                "iid": 88001,
            }},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1803"
        assert "PR_NUMBER_PARSE_FAILED" not in env

    def test_pr_comment_alias_uses_noteable_pr(self):
        """历史别名 pr_comment 同样按 PR 评论语义取 noteable_iid。"""
        env = {}
        _inject_issue_pr_env(env, {
            "type": "pr_comment", "issue_id": "#1802",
            "payload": {"object_attributes": {"noteable_iid": 1803}},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1803"

    def test_explicit_a2a_pr_number_injected(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "a2a_event", "issue_id": "#1802",
            "payload": {"pr_number": 1803},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1803"


class TestPrParseFailureWarning:
    """PR 实体解析失败：不注入 PR_NUMBER，但保留排障告警。"""

    def test_pr_event_no_source_marks_failed(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "pull_request_opened", "issue_id": "#1802",
            "payload": {"pull_request": {"title": "no number here"}},
        }, _TEST_REPO)
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"

    def test_pr_event_non_numeric_source_marks_failed(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "note_hook_pr", "issue_id": "#1802",
            "payload": {"merge_request": {"iid": "not-a-number"}},
        }, _TEST_REPO)
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"


class TestA2aPayloadNoIssueNumberFallback:
    """无 PR 时 A2A payload 的 pr_number 为空串，严禁回落 issue_number。"""

    def test_a2a_pr_number_empty_when_no_real_pr(self):
        env = {}
        _inject_issue_pr_env(env, _issue_evt(payload={
            "object_attributes": {"iid": 1802},
        }), _TEST_REPO)
        payload = {
            "issue_number": env.get("ISSUE_NUMBER", ""),
            "pr_number": env.get("PR_NUMBER", ""),
        }
        assert payload["pr_number"] == ""
        assert payload["pr_number"] != payload["issue_number"]