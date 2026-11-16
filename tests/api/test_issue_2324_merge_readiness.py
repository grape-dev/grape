"""Issue #2324：统一合并判定（mergeable && can_merge_check && approval_reviewers_required_passed）。

覆盖 Spec 验收标准：
- PR #1340 组合（mergeable=true, can_merge_check=false, approval_reviewers=[],
  required_reviewers_count=1）→ 「待评审/不可合入」+ 缺失项；
- 三元全 true → 可合入；
- approval_reviewers=[] 且 count>=1 → 稳定输出「待指定评审人」，层级误读不退化；
- QA 判定路径（pulls get）与自动合入路径（pulls merge）共用同一判定实现；
- 字段缺失/层级误读显式不可合入并告警。
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api import git_repo_api
from src.api.git_repo_api import PRMergeBody, get_pull, merge_pull
from src.api.merge_readiness import evaluate_merge_readiness


def _detail(mergeable=True, can_merge_check=True, reviewers_passed=True,
            required_count=1, approval_reviewers=None):
    return {
        "number": 1340,
        "mergeable": mergeable,
        "can_merge_check": can_merge_check,
        "approval_reviewers": [] if approval_reviewers is None else approval_reviewers,
        "mergeable_state": {
            "approval_reviewers_required_passed": reviewers_passed,
            "approval_approvers_required_passed": True,
            "approval_approvers_result": 2,
            "merge_request_switch": {
                "review_mode": "approval",
                "merge_method": "ff",
                "approval_required_reviewers_count": required_count,
            },
        },
    }


# ===================== 纯函数：验收组合 =====================

def test_pr_1340_combo_pending_review_with_missing_items():
    """PR #1340 实测组合 → 待评审/不可合入，并列出 can_merge_check 与待指定评审人。"""
    r = evaluate_merge_readiness(_detail(mergeable=True, can_merge_check=False,
                                         reviewers_passed=False, required_count=1,
                                         approval_reviewers=[]))
    assert r["ready"] is False
    assert r["verdict"] == "not_mergeable"
    assert r["label"] == "待评审/不可合入"
    codes = [m["code"] for m in r["missing"]]
    assert "can_merge_check" in codes
    assert "approval_reviewers_required_passed" in codes
    assert "reviewers_not_assigned" in codes
    assert "待指定评审人" in r["summary"]


def test_all_true_is_ready():
    """三元全 true → 可合入。"""
    r = evaluate_merge_readiness(_detail(approval_reviewers=[{"login": "alice"}]))
    assert r["ready"] is True
    assert r["verdict"] == "ready"
    assert r["label"] == "可合入"
    assert r["missing"] == []


def test_reviewers_empty_and_count_ge_1_always_pending():
    """approval_reviewers=[] 且 count>=1 → 稳定输出「待指定评审人」。"""
    for count in (1, 2):
        r = evaluate_merge_readiness(_detail(reviewers_passed=False,
                                             required_count=count,
                                             approval_reviewers=[]))
        assert r["ready"] is False
        assert r["verdict"] == "pending_review"
        assert "reviewers_not_assigned" in [m["code"] for m in r["missing"]]
        assert "待指定评审人" in r["summary"]


def test_count_zero_skips_reviewer_check():
    """required_count=0（未开启评审人要求）→ 不纳入 reviewers 校验。"""
    r = evaluate_merge_readiness(_detail(reviewers_passed=False, required_count=0,
                                         approval_reviewers=[]))
    assert r["ready"] is True
    assert r["verdict"] == "ready"


def test_missing_can_merge_check_not_ready_with_warning():
    """顶层 can_merge_check 缺失 → 显式不可合入 + 告警（不退化为只看 mergeable）。"""
    d = _detail()
    d.pop("can_merge_check")
    r = evaluate_merge_readiness(d)
    assert r["ready"] is False
    assert "can_merge_check" in [m["code"] for m in r["missing"]]
    assert r["warnings"]


def test_wrong_field_path_does_not_silently_pass():
    """层级误读：can_merge_check 只在 mergeable_state 内（错误路径）→ 不可合入。"""
    d = _detail()
    d.pop("can_merge_check")
    d["mergeable_state"]["can_merge_check"] = True
    r = evaluate_merge_readiness(d)
    assert r["ready"] is False
    assert "can_merge_check" in [m["code"] for m in r["missing"]]


def test_missing_mergeable_not_ready_with_warning():
    """mergeable 缺失 → 显式不可合入 + 告警。"""
    d = _detail()
    d.pop("mergeable")
    r = evaluate_merge_readiness(d)
    assert r["ready"] is False
    assert r["verdict"] == "not_mergeable"
    assert "mergeable" in [m["code"] for m in r["missing"]]
    assert any("mergeable 字段缺失" in w for w in r["warnings"])


def test_missing_reviewers_count_not_ready_with_warning():
    """approval_required_reviewers_count 缺失 → 不可合入 + 告警。"""
    d = _detail()
    d["mergeable_state"]["merge_request_switch"].pop(
        "approval_required_reviewers_count")
    r = evaluate_merge_readiness(d)
    assert r["ready"] is False
    assert "approval_required_reviewers_count" in [m["code"] for m in r["missing"]]
    assert any("approval_required_reviewers_count 字段缺失" in w for w in r["warnings"])


@pytest.mark.parametrize("bad_count", ["1", 1.5, True, {"n": 1}])
def test_invalid_reviewers_count_not_ready_with_warning(bad_count):
    """approval_required_reviewers_count 非整数 → 不可合入 + 告警。"""
    d = _detail()
    d["mergeable_state"]["merge_request_switch"][
        "approval_required_reviewers_count"] = bad_count
    r = evaluate_merge_readiness(d)
    assert r["ready"] is False
    assert "approval_required_reviewers_count" in [m["code"] for m in r["missing"]]
    assert any("取值非法" in w for w in r["warnings"])


def test_schema_all_missing_gitcode_is_not_mergeable():
    """gitcode 下 schema 全缺失 → 不可合入 + 缺失项 + 告警（不退化为 unknown 放行）。"""
    r = evaluate_merge_readiness({"number": 1340})
    assert r["ready"] is False
    assert r["verdict"] == "not_mergeable"
    codes = [m["code"] for m in r["missing"]]
    assert {"mergeable", "can_merge_check", "approval_required_reviewers_count"} <= set(codes)
    assert r["warnings"]


def test_schema_all_missing_non_gitcode_is_unknown():
    """非 gitcode provider 下 schema 全缺失仍 unknown fail-open。"""
    r = evaluate_merge_readiness({"number": 1340}, "github")
    assert r["verdict"] == "unknown"
    assert r["ready"] is False


def test_non_gitcode_provider_is_unknown():
    """非 gitcode provider（schema 不同）→ unknown，不阻断。"""
    r = evaluate_merge_readiness(_detail(), "github")
    assert r["verdict"] == "unknown"
    assert r["ready"] is False


# ===================== 路由：QA 判定路径 =====================

def test_get_pull_attaches_merge_readiness():
    """pulls get 响应附加 merge_readiness（QA hit 判定路径）。"""
    detail = _detail(mergeable=True, can_merge_check=False, reviewers_passed=False)
    with patch.object(git_repo_api, "_vcs_req", lambda *a, **k: dict(detail)), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"):
        resp = get_pull("o", "hello", 1340, user={"id": 1}, p="gitcode")
    assert resp["merge_readiness"]["ready"] is False
    assert resp["merge_readiness"]["label"] == "待评审/不可合入"
    assert "待指定评审人" in resp["merge_readiness"]["summary"]


# ===================== 路由：自动合入前置检查路径 =====================

_USER = {"id": 1, "username": "tester", "auth_type": "session_token"}


def _merge_with(detail, provider="gitcode"):
    calls = []

    def _fake_vcs_req(provider, method, url, token, body=None):
        calls.append((method, url))
        if method == "GET" and "/pulls/1340" in url:
            return dict(detail)
        return {"merged": True, "sha": "abc"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.run_post_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log"):
        return merge_pull("o", "hello", 1340, PRMergeBody(), user=_USER, p=provider), calls


def test_merge_route_blocks_when_not_ready():
    """自动合入路径复用同一判定：不可合入 → 409，且不调用上游 merge。"""
    with pytest.raises(HTTPException) as exc:
        _merge_with(_detail(mergeable=True, can_merge_check=False,
                            reviewers_passed=False, approval_reviewers=[]))
    assert exc.value.status_code == 409
    gate = exc.value.detail["gate"]
    assert gate["phase"] == "pre_merge_readiness"
    assert "reviewers_not_assigned" in [m["code"] for m in gate["missing"]]


def test_merge_route_schema_all_missing_blocks():
    """gitcode schema 全缺失 → 自动合入路径 409（不再 unknown 放行）。"""
    with pytest.raises(HTTPException) as exc:
        _merge_with({"number": 1340})
    assert exc.value.status_code == 409
    gate = exc.value.detail["gate"]
    assert gate["phase"] == "pre_merge_readiness"
    assert gate["verdict"] == "not_mergeable"
    assert gate["missing"]


def test_merge_route_unknown_provider_fail_open():
    """非 gitcode → unknown，合入路径按原流程继续（fail-open）。"""
    resp, calls = _merge_with(_detail(), provider="github")
    assert resp == {"merged": True, "sha": "abc"}
    assert any(m == "PUT" and url.endswith("/merge") for m, url in calls)


def test_merge_route_ready_passes():
    """自动合入路径：判定可合入 → 正常调用上游 merge。"""
    resp, calls = _merge_with(_detail(approval_reviewers=[{"login": "alice"}]))
    assert resp == {"merged": True, "sha": "abc"}
    assert any(m == "PUT" and url.endswith("/merge") for m, url in calls)
