"""Issue #595: pull_request_opened 事件 PR_NUMBER 嵌套键兜底解析

PR 关联真实 Issue 时（resolve_pr_issue_id 命中，不走 PR 订阅分支），webhook
原始 payload 无顶层 pr_number、evt._pr_sub_pr_number 也未设置 → PR_NUMBER
恒为空，下游技能 A2A payload pr_number 无从填写（用户反馈 172）。

兜底解析：前两来源（payload.pr_number / evt._pr_sub_pr_number）均空时依次尝试
payload.pull_request.number（GitHub/GitCode）→ payload.merge_request.number
（GitLab MR）→ payload.object_attributes.iid（GitLab hook），沿用
lstrip("#").isdigit() 校验。

验收：
1. PR 关联真实 Issue（payload.pull_request.number）→ PR_NUMBER 注入。
2. GitLab 形态（merge_request.number / object_attributes.iid）→ 注入。
3. 优先级不变：顶层 pr_number > _pr_sub_pr_number > 嵌套键。
4. PR 订阅（无关联 Issue，仅 _pr_sub_pr_number）形态不回归。
5. 嵌套值非数字 → 不注入（沿用 Issue #528 纯数字校验）。
"""
from src.api.event_pipeline import _inject_issue_pr_env

_TEST_REPO = "gitcode:owner/repo"


def _pr_linked_evt(issue_id="#590", payload=None):
    """构造 PR 关联真实 Issue 的 pull_request_opened 事件（无顶层 pr_number）。"""
    return {
        "type": "pull_request_opened",
        "issue_id": issue_id,
        "payload": payload if payload is not None else {},
    }


class TestPrLinkedIssueNestedFallback:
    def test_pull_request_number_injected(self):
        """PR 关联真实 Issue：payload.pull_request.number 嵌套兜底注入 PR_NUMBER"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(
            payload={"pull_request": {"number": 773}}), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "590"
        assert env.get("PR_NUMBER") == "773"

    def test_merge_request_number_injected(self):
        """GitLab MR 形态：payload.merge_request.number 兜底注入"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(
            payload={"merge_request": {"number": 42}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "42"

    def test_object_attributes_iid_injected(self):
        """GitLab hook 形态：payload.object_attributes.iid 兜底注入"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(
            payload={"object_attributes": {"iid": 108}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "108"

    def test_nested_fallback_priority_order(self):
        """pull_request.number 优先于 merge_request.number / object_attributes.iid"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(payload={
            "pull_request": {"number": 1},
            "merge_request": {"number": 2},
            "object_attributes": {"iid": 3},
        }), _TEST_REPO)
        assert env.get("PR_NUMBER") == "1"

    def test_hash_prefixed_nested_number_normalized(self):
        """嵌套键带 # 前缀的数字 → 去前缀后注入"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(
            payload={"pull_request": {"number": "#773"}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "773"

    def test_non_numeric_nested_value_not_injected(self):
        """嵌套值非数字 → PR_NUMBER 不注入（沿用纯数字校验）"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(
            payload={"pull_request": {"number": "not-a-num"}}), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "590"
        assert "PR_NUMBER" not in env

    def test_top_level_pr_number_still_preferred(self):
        """顶层 payload.pr_number 存在时优先于嵌套键（优先级不变）"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(payload={
            "pr_number": 99, "pull_request": {"number": 100}}), _TEST_REPO)
        assert env.get("PR_NUMBER") == "99"


class TestPrSubscriptionNoRegression:
    def test_pr_sub_pr_number_preferred_over_nested(self):
        """PR 订阅形态：_pr_sub_pr_number 兜底正常，且优先于嵌套键"""
        evt = {
            "type": "pull_request_opened",
            "issue_id": "#pr_257_default",
            "_pr_sub_pr_number": 257,
            "payload": {"pull_request": {"number": 999}},
        }
        env = {}
        _inject_issue_pr_env(env, evt, _TEST_REPO)
        assert "ISSUE_NUMBER" not in env
        assert env.get("PR_NUMBER") == "257"

    def test_pr_sub_without_any_source_injects_nothing(self):
        """全来源均空 → PR_NUMBER 不注入（保持原行为）"""
        env = {}
        _inject_issue_pr_env(env, {
            "type": "issue_opened", "issue_id": "#66", "payload": {}}, _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "66"
        assert "PR_NUMBER" not in env


class TestNestedPayloadRobustness:
    def test_non_dict_nested_keys_ignored(self):
        """嵌套键非 dict（异常数据）→ 安全跳过不抛异常"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(payload={
            "pull_request": None, "merge_request": "oops",
            "object_attributes": []}), _TEST_REPO)
        assert "PR_NUMBER" not in env

    def test_missing_nested_keys_injects_nothing(self):
        """嵌套键缺失 → 不注入（普通 Issue 事件不受影响）"""
        env = {}
        _inject_issue_pr_env(env, _pr_linked_evt(payload={"title": "x"}), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "590"
        assert "PR_NUMBER" not in env
