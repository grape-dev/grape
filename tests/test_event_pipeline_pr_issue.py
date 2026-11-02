# -*- coding: utf-8 -*-
"""Issue #680: PR 合并事件关联 Issue 解析优先级重排 + 统一解析入口。

事故回放（事件 38851）：PR #116 合并时 issues 列表 [#282, #274] 被旧逻辑
"取第一个" 解析为 #274（正文背景引用的旧 issue），#274 场景无 sre 节点 →
场景校验 not_in_scene 丢弃事件 → SRE 断链 71 分钟。

修复要点（src/api/event_pipeline.py）：
1. resolve_pr_issue_id 优先级重排：分支名 issue-N → PR 标题 #N → issues 取最大 → PR 详情 API 兜底（同规则）
2. resolve_issue_id 的 issues 兜底段同改"取最大"
3. _resolve_event_issue_id 统一入口：一次解析存 evt["_resolved_issue_id"]，全链复用
4. _norm_issue_id：##274 双井号拼接缺陷修复
"""

import pytest


def _pr_merged_payload(title="feat: issue #282", issues=None, branch="feat/issue-282",
                       pr_number=116):
    """GitCode Merge Request Hook payload 形态（PR #116 事故原始形态）"""
    return {
        "object_attributes": {
            "iid": pr_number,
            "number": pr_number,
            "title": title,
            "source_branch": branch,
            "action": "merge",
            "state": "merged",
        },
        "issues": issues if issues is not None else [{"number": 282}, {"number": 274}],
        "project": {"path_with_namespace": "grape-dev/grape"},
    }


# ===== 1. 事故回放：PR #116 原始形态 =====

class TestIncidentReplay:
    def test_pr116_original_shape_resolves_282(self):
        """PR #116 原始形态（title 含 issue #282、issues=[#282,#274]、head=feat/issue-282）→ #282"""
        from src.api.event_pipeline import resolve_pr_issue_id
        assert resolve_pr_issue_id(_pr_merged_payload()) == "#282"

    def test_issues_list_disordered_still_max(self):
        """多 issue 列表乱序 [#274,#282] → #282（不依赖列表顺序）"""
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="some work", branch="feat/x",
                               issues=[{"number": 274}, {"number": 282}])
        assert resolve_pr_issue_id(p) == "#282"

    def test_branch_wins_over_body_background_ref(self):
        """分支名 feat/issue-282 + issues 列表仅含背景引用 #274 → 分支强信号 #282"""
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="some work", branch="feat/issue-282",
                               issues=[{"number": 274}])
        assert resolve_pr_issue_id(p) == "#282"

    def test_title_signal_when_branch_generic(self):
        """分支无 issue-N + title 含 #282 → 标题信号 #282"""
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="fix: resolve issue #282", branch="feat/x",
                               issues=[])
        assert resolve_pr_issue_id(p) == "#282"


# ===== 2. 优先级：分支 > 标题 > issues 最大 =====

class TestPriorityOrder:
    def test_branch_beats_title_and_issues(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="fix #274", branch="feat/issue-282",
                               issues=[{"number": 274}])
        assert resolve_pr_issue_id(p) == "#282"

    def test_title_beats_issues_max(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="fix #300", branch="feat/x",
                               issues=[{"number": 274}, {"number": 282}])
        assert resolve_pr_issue_id(p) == "#300"

    def test_no_signal_returns_empty(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="no ref here", branch="feat/x", issues=[])
        assert resolve_pr_issue_id(p) == ""

    def test_title_self_reference_skipped(self):
        """title 自引用（/pulls/N == PR 自身编号）跳过 → issues 最大兜底"""
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title=f"see /pulls/116", branch="feat/x",
                               issues=[{"number": 274}, {"number": 282}])
        assert resolve_pr_issue_id(p) == "#282"

    def test_issues_containing_pr_self_number_skipped(self):
        """issues 列表混入 PR 自身编号（116）→ 跳过，取剩余最大 #282"""
        from src.api.event_pipeline import resolve_pr_issue_id
        p = _pr_merged_payload(title="work", branch="feat/x",
                               issues=[{"number": 116}, {"number": 274}, {"number": 282}])
        assert resolve_pr_issue_id(p) == "#282"


# ===== 3. resolve_issue_id 的 issues 兜底段"取最大" =====

class TestResolveIssueIdMaxFallback:
    def test_issues_fallback_takes_max(self):
        from src.api.event_pipeline import resolve_issue_id
        p = {"object_attributes": {"iid": 116, "source_branch": "feat/x"},
             "issues": [{"number": 274}, {"number": 282}]}
        assert resolve_issue_id(p) == "#282"

    def test_single_issue_unchanged(self):
        from src.api.event_pipeline import resolve_issue_id
        p = {"object_attributes": {"iid": 116, "source_branch": "feat/x"},
             "issues": [{"number": 23}]}
        assert resolve_issue_id(p) == "#23"

    def test_non_pr_event_object_attributes_first(self):
        """Issue 类事件 object_attributes 优先（Issue #89 行为不变）"""
        from src.api.event_pipeline import resolve_issue_id
        p = {"object_attributes": {"iid": 55, "action": "open"},
             "issues": [{"number": 50}]}
        assert resolve_issue_id(p) == "#55"


# ===== 4. GitHub 形态（无 issues 列表）行为不变 =====

class TestGithubShape:
    def test_github_pull_request_head_ref_branch(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = {"pull_request": {"number": 116, "title": "fix", "head": {"ref": "feat/issue-282"}}}
        assert resolve_pr_issue_id(p) == "#282"

    def test_github_title_hash(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = {"pull_request": {"number": 116, "title": "fix #282", "head": {"ref": "main"}}}
        assert resolve_pr_issue_id(p) == "#282"

    def test_github_no_signal_empty(self):
        from src.api.event_pipeline import resolve_pr_issue_id
        p = {"pull_request": {"number": 116, "title": "plain", "head": {"ref": "main"}}}
        assert resolve_pr_issue_id(p) == ""


# ===== 5. _norm_issue_id：双井号规范化 =====

class TestNormIssueId:
    def test_double_hash_normalized(self):
        from src.api.event_pipeline import _norm_issue_id
        assert _norm_issue_id("##274") == "#274"
        assert _norm_issue_id("#274") == "#274"
        assert _norm_issue_id("274") == "#274"
        assert _norm_issue_id(" #274 ") == "#274"

    def test_invalid_values(self):
        from src.api.event_pipeline import _norm_issue_id
        assert _norm_issue_id("") == ""
        assert _norm_issue_id(None) == ""
        assert _norm_issue_id("0") == ""
        assert _norm_issue_id("#0") == ""
        assert _norm_issue_id("abc") == ""

    def test_number_field_already_hashed(self):
        """payload issue.number 字段自带 # 前缀 → 不再拼出 ##274"""
        from src.api.event_pipeline import resolve_issue_id
        assert resolve_issue_id({"issues": [{"number": "#274"}]}) == "#274"
        assert resolve_issue_id({"issue_number": "#5"}) == "#5"


# ===== 6. 统一入口 _resolve_event_issue_id：一次解析全链同值 =====

class TestUnifiedEventResolution:
    def _evt(self, payload, evt_type="pull_request_merged"):
        return {"type": evt_type, "payload": payload, "issue_id": ""}

    def test_resolved_once_and_cached(self):
        """同一 evt 多调用点解析一致（_resolved_issue_id 只计算一次）"""
        from src.api.event_pipeline import _resolve_event_issue_id
        evt = self._evt(_pr_merged_payload())
        first = _resolve_event_issue_id(evt, "gitcode:grape-dev/grape")
        assert first == "#282"
        assert evt["_resolved_issue_id"] == "#282"
        # 再次调用（含带 token 场景）返回缓存，不重算
        again = _resolve_event_issue_id(evt, "gitcode:grape-dev/grape", api_token="tok")
        assert again == "#282"
        assert evt["_resolved_issue_id"] == "#282"

    def test_resolve_called_once_across_calls(self):
        from src.api.event_pipeline import _resolve_event_issue_id
        evt = self._evt(_pr_merged_payload())
        calls = {"n": 0}
        import src.api.event_pipeline as ep
        orig = ep.resolve_pr_issue_id

        def _counting(payload, api_token="", repo_key=""):
            calls["n"] += 1
            return orig(payload, api_token, repo_key=repo_key)

        ep.resolve_pr_issue_id = _counting
        try:
            _resolve_event_issue_id(evt, "gitcode:grape-dev/grape")
            _resolve_event_issue_id(evt, "gitcode:grape-dev/grape", api_token="tok")
            _resolve_event_issue_id(evt, "gitcode:grape-dev/grape")
        finally:
            ep.resolve_pr_issue_id = orig
        assert calls["n"] == 1

    def test_non_pr_event_uses_resolve_issue_id(self):
        from src.api.event_pipeline import _resolve_event_issue_id
        evt = self._evt({"issue": {"number": 21}}, evt_type="issue_comment")
        assert _resolve_event_issue_id(evt, "gitcode:o/r") == "#21"

    def test_fallback_to_evt_issue_id(self):
        from src.api.event_pipeline import _resolve_event_issue_id
        evt = self._evt({"object_attributes": {"title": "no ref"}}, evt_type="push")
        evt["issue_id"] = "#99"
        assert _resolve_event_issue_id(evt, "gitcode:o/r") == "#99"

    def test_token_upgrade_only_when_empty(self):
        """首次无 token 解析为空 → 带 token 补一次 API 兜底；非空结果一经产生即锁定"""
        from src.api.event_pipeline import _resolve_event_issue_id
        evt = self._evt(_pr_merged_payload(title="no ref", branch="feat/x", issues=[]))
        assert _resolve_event_issue_id(evt, "gitcode:o/r") == ""
        # 带 token（无网络环境下 API 兜底失败返回空，但不再重算）
        assert evt.get("_resolved_issue_id_token_tried") is not True  # 首次无 token 未标记
        _resolve_event_issue_id(evt, "gitcode:o/r", api_token="tok")
        assert evt.get("_resolved_issue_id_token_tried") is True
