"""Issue #1076：issues update `milestone_id` 取值口径、4xx 语义与失败原子性。

- 合法口径 = milestones list 的 `number`（如 850012），设置成功后回读一致。
- 非法/不存在 number（3、999999）、非数字 → 4xx 可读错误，绝不 502。
- 写入前预校验；失败请求保持原 milestone（必要时回滚），不清空既有值。
- 上游真实故障（回读/预校验不可用）仍返回 5xx。
"""
import importlib

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

gra = importlib.import_module("src.api.git_repo_api")

MILESTONES = [
    {"number": 850012, "title": "weekly-03"},
    {"number": 850013, "title": "weekly-04"},
]


def _transport(monkeypatch, issue_responses, patch_resp=None, milestones=None,
               milestone_error=None):
    calls = []
    state = {"n": 0}

    def fake_req(provider, method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and url.split("?")[0].endswith("/milestones"):
            if milestone_error is not None:
                raise milestone_error
            return MILESTONES if milestones is None else milestones
        if method == "GET":
            i = min(state["n"], len(issue_responses) - 1)
            state["n"] += 1
            return issue_responses[i]
        return patch_resp if patch_resp is not None else {"number": 640, "state": "open"}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    return calls


def test_valid_number_sets_and_reads_back(monkeypatch):
    """AC1：milestone_id=850012（number）设置成功，响应回显回读 milestone。"""
    calls = _transport(monkeypatch, [
        {"title": "issue title", "milestone": None},
        {"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
    ])
    resp = gra.update_issue(
        "grape-dev", "grape", 640,
        gra.IssueUpdateBody(milestone_id=850012),
        user={"auth_type": "session_token"}, p="gitcode",
    )
    patches = [c for c in calls if c[0] == "PATCH"]
    assert patches[0][2]["milestone"] == 850012
    assert resp["milestone"] == {"number": 850012, "title": "weekly-03"}


@pytest.mark.parametrize("bad", [3, 999999])
def test_invalid_number_returns_400_without_patch(monkeypatch, bad):
    """AC2（Issue #1005 起）：不存在的 id/number → 400 可读错误，无 502，且不发出 PATCH。"""
    calls = _transport(monkeypatch, [
        {"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
    ])
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=bad),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 400
    assert f"milestone_id={bad}" in ei.value.detail["message"]
    assert "number" in ei.value.detail["message"]
    assert "850012" in ei.value.detail["message"]  # 给出合法取值来源/可用值
    assert [c for c in calls if c[0] == "PATCH"] == []


def test_non_numeric_milestone_id_rejected_by_contract():
    """AC2：非数字 → 契约层 4xx（Pydantic ValidationError，文案指向合法取值来源）。"""
    with pytest.raises(ValidationError) as ei:
        gra.IssueUpdateBody(milestone_id="abc")
    assert "milestone number" in str(ei.value)


def test_readback_mismatch_rolls_back_existing_milestone(monkeypatch):
    """AC3：先挂 850012，请求 850013 后上游回读为空 → 422，回滚并回读确认为原 milestone=850012。"""
    calls = _transport(monkeypatch, [
        {"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
        {"title": "issue title", "milestone": None},
        {"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
    ])
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=850013),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 422
    assert "回滚为原 milestone=850012" in ei.value.detail["message"]
    patches = [c for c in calls if c[0] == "PATCH"]
    assert [p[2]["milestone"] for p in patches] == [850013, 850012]


def test_rollback_silently_ignored_reports_not_restored(monkeypatch):
    """AC3：回滚 PATCH 被上游静默忽略时，回读确认失败必须标注「原值未恢复」，不得谎称已回滚。"""
    calls = _transport(monkeypatch, [
        {"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
        {"title": "issue title", "milestone": None},
        {"title": "issue title", "milestone": None},
    ])
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=850013),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 422
    assert "原值未恢复" in ei.value.detail["message"]
    assert "已回滚为原 milestone=850012" not in ei.value.detail["message"]
    patches = [c for c in calls if c[0] == "PATCH"]
    assert [p[2]["milestone"] for p in patches] == [850013, 850012]


def test_milestone_scan_does_not_stop_at_short_page(monkeypatch):
    """Spec：上游静默钳制页大小时，不能以「本页不足 per_page」提前结束，必须扫到空页。"""
    pages = {
        1: [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}],
        2: [{"number": 850012, "title": "weekly-03"}],
        3: [],
    }
    seen = []

    def fake_req(provider, method, url, token, payload=None):
        page = int(url.split("page=")[1].split("&")[0])
        seen.append(page)
        return pages.get(page, [])

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    found, numbers = gra._gitcode_milestone_number_exists(
        "grape-dev", "grape", 850012, user={"auth_type": "session_token"}, p="gitcode")
    assert found is True
    assert 850012 in numbers
    assert seen == [1, 2, 3]


def test_upstream_unavailable_during_readback_still_5xx(monkeypatch):
    """AC4：回读时上游真实不可用 → 502（不被 4xx 掩盖）。"""
    from src.user.repo_manager import UpstreamAPIError

    calls = []
    state = {"n": 0}

    def fake_req(provider, method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and url.split("?")[0].endswith("/milestones"):
            return MILESTONES
        if method == "GET":
            state["n"] += 1
            if state["n"] == 1:
                return {"title": "issue title", "milestone": None}
            raise UpstreamAPIError("GET", url, 503, "service unavailable")
        return {"number": 640}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=850012),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code >= 500