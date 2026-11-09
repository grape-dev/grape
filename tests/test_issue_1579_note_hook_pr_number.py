"""Issue #1579: note_hook_pr 事件注入 PR_NUMBER

复现场景：qa 任务 t_2da97c2d28cf（events seq 59972，issue #1412 / PR #1011）
env_extra 仅注入 ISSUE_NUMBER，PR_NUMBER 缺失，Agent 只能从标题猜号。

验收：
1. Note Hook payload 的 noteable_iid / merge_request.iid → PR_NUMBER 注入。
2. payload 无 PR 字段时标题 `note_hook_pr #<N>` 兜底。
3. 标题来源须与关联 Issue 校验；跨 Issue 不匹配 → 不注入并标记失败。
4. 解析失败 → PR_NUMBER_PARSE_FAILED=1 + 告警，禁止用 ISSUE_NUMBER 顶替。
5. 非 note_hook_pr 事件（含 a2a_event）注入逻辑不回归。
"""
import src.api.event_pipeline as ep
from src.api.event_pipeline import _inject_issue_pr_env

_TEST_REPO = "gitcode:owner/repo"


def _note_event(payload=None, title="", issue_id="#1412"):
    return {
        "type": "note_hook_pr",
        "issue_id": issue_id,
        "title": title,
        "payload": payload if payload is not None else {},
    }


class TestNoteHookPayloadSources:
    def test_noteable_iid_injected(self):
        env = {}
        _inject_issue_pr_env(env, _note_event(
            payload={"object_attributes": {"noteable_type": "MergeRequest",
                                           "noteable_iid": 1011}}), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1412"
        assert env.get("PR_NUMBER") == "1011"
        assert "PR_NUMBER_PARSE_FAILED" not in env

    def test_merge_request_iid_injected(self):
        env = {}
        _inject_issue_pr_env(env, _note_event(
            payload={"merge_request": {"iid": 1011}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "1011"

    def test_top_level_pr_number_preferred(self):
        env = {}
        _inject_issue_pr_env(env, _note_event(payload={
            "pr_number": 1011, "merge_request": {"iid": 999}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "1011"

    def test_note_iid_not_used_as_pr_number(self):
        """Note Hook 的 object_attributes.iid 是评论 id，不得当 PR 号。"""
        env = {}
        _inject_issue_pr_env(env, _note_event(
            payload={"object_attributes": {"noteable_type": "MergeRequest",
                                           "iid": 88001}}), _TEST_REPO)
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"


class TestNoteHookTitleFallback:
    def test_title_fallback_validated(self, monkeypatch):
        monkeypatch.setattr(ep, "_validate_pr_issue_binding",
                            lambda repo, pr, issue: True)
        env = {}
        _inject_issue_pr_env(env, _note_event(title="note_hook_pr #1011"), _TEST_REPO)
        assert env.get("PR_NUMBER") == "1011"
        assert "PR_NUMBER_PARSE_FAILED" not in env

    def test_title_fallback_fail_open_when_unverifiable(self, monkeypatch):
        monkeypatch.setattr(ep, "_validate_pr_issue_binding",
                            lambda repo, pr, issue: None)
        env = {}
        _inject_issue_pr_env(env, _note_event(title="note_hook_pr #1011"), _TEST_REPO)
        assert env.get("PR_NUMBER") == "1011"

    def test_title_cross_issue_mismatch_marks_failed(self, monkeypatch):
        monkeypatch.setattr(ep, "_validate_pr_issue_binding",
                            lambda repo, pr, issue: False)
        env = {}
        _inject_issue_pr_env(env, _note_event(title="note_hook_pr #1011"), _TEST_REPO)
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"


class TestNoteHookParseFailure:
    def test_no_source_marks_failed_and_never_uses_issue_number(self):
        env = {}
        _inject_issue_pr_env(env, _note_event(payload={}), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1412"
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"

    def test_non_numeric_source_marks_failed(self):
        env = {}
        _inject_issue_pr_env(env, _note_event(
            payload={"merge_request": {"iid": "not-a-number"}}), _TEST_REPO)
        assert "PR_NUMBER" not in env
        assert env.get("PR_NUMBER_PARSE_FAILED") == "1"


class TestNonNoteHookNoRegression:
    def test_a2a_event_still_injects_payload_pr_number(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "a2a_event",
            "issue_id": "#1412",
            "payload": {"pr_number": 1011},
        }, _TEST_REPO)
        assert env.get("PR_NUMBER") == "1011"
        assert "PR_NUMBER_PARSE_FAILED" not in env

    def test_issue_event_without_pr_has_no_failure_marker(self):
        env = {}
        _inject_issue_pr_env(env, {
            "type": "issue_comment",
            "issue_id": "#1412",
            "payload": {"object_attributes": {"noteable_iid": 1412}},
        }, _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "1412"
        assert "PR_NUMBER" not in env
        assert "PR_NUMBER_PARSE_FAILED" not in env