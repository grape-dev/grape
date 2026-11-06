"""Issue #640：issues update `milestone_id` 契约与运行时行为（里程碑假成功修复）。

- 契约层：gitcode provider `_RULES[("issues","update")]` 将 milestone_id 映射为上游 `milestone`。
- 运行时层：GitCode PATCH 必填 repo+title 自动补齐（仅传 milestone_id 不再 400）；
  写后回读校验（Issue #1076 起未落库/不一致 → 422 可读错误，5xx 仅保留上游真实故障）；响应回显 milestone。
"""
import importlib

import pytest
from fastapi import HTTPException


gra = importlib.import_module("src.api.git_repo_api")
gitcode = importlib.import_module("src.vcs.providers.gitcode")


def test_gitcode_rule_maps_milestone_id_to_upstream_milestone():
    rule = gitcode._RULES[("issues", "update")]
    assert rule["milestone_id"] == "milestone"
    native = gitcode.translate("issues", "update", {"milestone_id": 850012}, {})
    assert native == {"milestone": 850012}


def test_gitcode_translate_skips_empty_milestone():
    # API 层中性 payload 归一化：milestone_id=0 → None（body.milestone_id or None）
    native = gitcode.translate("issues", "update", {"milestone_id": None}, {})
    assert native == {}


def _patch_transport(monkeypatch, get_resp, patch_resp, milestones=None):
    calls = []
    state = {"n": 0}

    def fake_req(provider, method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            if url.split("?")[0].endswith("/milestones"):
                return milestones if milestones is not None else [{"number": 850012, "title": "weekly-03"}]
            if isinstance(get_resp, list):
                i = min(state["n"], len(get_resp) - 1)
                state["n"] += 1
                return get_resp[i]
            return get_resp
        return patch_resp

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    return calls


def test_update_issue_gitcode_backfills_repo_title_and_echoes_milestone(monkeypatch):
    """仅传 milestone_id：PATCH body 自动补 repo+title（消除上游 400），回读后响应回显 milestone。"""
    calls = _patch_transport(
        monkeypatch,
        get_resp={"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
        patch_resp={"number": 640, "state": "open"},
    )
    resp = gra.update_issue(
        "grape-dev", "grape", 640,
        gra.IssueUpdateBody(milestone_id=850012),
        user={"auth_type": "session_token"}, p="gitcode",
    )
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 1
    payload = patches[0][2]
    assert payload["milestone"] == 850012          # 上游参数名（非 milestone_id）
    assert payload["repo"] == "grape-dev/grape"    # PATCH 必填 repo 自动补齐
    assert payload["title"] == "issue title"       # PATCH 必填 title 回取回填
    assert "milestone_id" not in payload
    # 写后回读：响应回显 milestone
    assert resp["milestone"] == {"number": 850012, "title": "weekly-03"}


def test_update_issue_gitcode_nonexistent_milestone_raises_422_without_patch(monkeypatch):
    """不存在的 number → 422（非 502），且预校验失败即中止，绝不发出 PATCH（原值不被清空）。"""
    calls = _patch_transport(
        monkeypatch,
        get_resp={"title": "issue title", "milestone": {"number": 850012, "title": "weekly-03"}},
        patch_resp={"number": 640, "state": "open"},
        milestones=[{"number": 850012, "title": "weekly-03"}],
    )
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=999999),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 422
    assert "milestone_id=999999" in ei.value.detail["message"]
    assert "number" in ei.value.detail["message"]
    assert [c for c in calls if c[0] == "PATCH"] == []


def test_update_issue_gitcode_stale_milestone_ignored_raises_422(monkeypatch):
    """Issue 原挂旧里程碑（number=111），新值 850012 被上游静默忽略——
    回读非空但 number ≠ 请求的 milestone_id → 422（不得 200 假成功并回显错误值）。"""
    _patch_transport(
        monkeypatch,
        get_resp={"title": "issue title", "milestone": {"number": 111, "title": "old-weekly"}},
        patch_resp={"number": 640, "state": "open"},
        milestones=[{"number": 111, "title": "old-weekly"}, {"number": 850012, "title": "weekly-03"}],
    )
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(milestone_id=850012),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 422
    assert "milestone_id=850012" in ei.value.detail["message"]
    assert "111" in ei.value.detail["message"]


def test_update_issue_gitcode_title_backfill_empty_raises_502(monkeypatch):
    """审查 Minor 用例：GitCode PATCH 必填 title 回取为空 → 明确 502，而非以空串回填后被上游 400。"""
    calls = _patch_transport(
        monkeypatch,
        get_resp={"title": "", "milestone": None},
        patch_resp={"number": 640, "state": "open"},
    )
    with pytest.raises(HTTPException) as ei:
        gra.update_issue(
            "grape-dev", "grape", 640,
            gra.IssueUpdateBody(state="close"),
            user={"auth_type": "session_token"}, p="gitcode",
        )
    assert ei.value.status_code == 502
    assert "title" in ei.value.detail["message"]
    assert [c for c in calls if c[0] == "PATCH"] == []  # 补齐失败即中止，未发出 PATCH


def test_update_issue_gitcode_no_milestone_no_readback(monkeypatch):
    """不带 milestone_id：不做回读校验（无额外 GET-after-PATCH），行为保持存量。"""
    calls = _patch_transport(
        monkeypatch,
        get_resp={"title": "issue title"},
        patch_resp={"number": 640, "state": "opened"},
    )
    resp = gra.update_issue(
        "grape-dev", "grape", 640,
        gra.IssueUpdateBody(state="close"),
        user={"auth_type": "session_token"}, p="gitcode",
    )
    gets = [c for c in calls if c[0] == "GET"]
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 1 and patches[0][2]["state"] == "close"
    assert len(gets) == 1  # 仅 title 回取这一次 GET
    assert resp == {"number": 640, "state": "opened"}


def test_update_issue_github_unchanged_passthrough(monkeypatch):
    """GitHub 语义不变：milestone_id 同名透传，不做 repo/title 补齐与回读。"""
    calls = _patch_transport(
        monkeypatch,
        get_resp={},
        patch_resp={"number": 640},
    )
    resp = gra.update_issue(
        "o", "r", 640,
        gra.IssueUpdateBody(milestone_id=7),
        user={"auth_type": "session_token"}, p="github",
    )
    payload = [c for c in calls if c[0] == "PATCH"][0][2]
    assert payload == {"milestone_id": 7}
    assert [c for c in calls if c[0] == "GET"] == []
    assert resp == {"number": 640}
