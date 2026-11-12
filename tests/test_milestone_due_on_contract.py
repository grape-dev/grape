"""milestones create 的 due_on 契约开关（PR #1231 三平台实弹收敛，方案 A）。

背景（2026-11-11 三平台代理矩阵，29 能力点）：
- GitCode/Gitee v5 上游建里程碑强制 due_on（空值 → 400 "due_on must not be
  empty" / "due_on is missing/empty"，补 due_on 后 200；GitCode 侧已独立实弹
  复现，HTTP 400 PARAMETER_ERROR）。
- GitHub 上游 due_on 可选。
拍板口径：不做静默补缺省（假日期会进入 due_on 排序/逾期口径，形成不可审计
的数据污染），代理层入口按契约开关拦截，给可读 400。

本文件锁四条：
1. 契约开关在册：gitcode/gitee True，github 缺省 False（getattr 派生，无枚举）。
2. github 不拦：缺 due_on 原样透传（payload 不含 due_on 键）。
3. gitcode/gitee 拦：缺 due_on → 400 可读文案，且上游未被调用（零外呼）。
4. gitcode 带 due_on：日期形态裁剪（MILESTONE_DUE_DATE_ONLY）与必填拦截正交共存。
"""
import importlib

import pytest
from fastapi import HTTPException

gra = importlib.import_module("src.api.git_repo_api")
gitcode = importlib.import_module("src.domains.runtime.vcs.providers.gitcode")
gitee = importlib.import_module("src.domains.runtime.vcs.providers.gitee")
github = importlib.import_module("src.domains.runtime.vcs.providers.github")

_USER = {"id": 1, "username": "milestone_contract_tester"}


def test_contract_switches_declared():
    assert gitcode.MILESTONE_DUE_ON_REQUIRED is True
    assert gitee.MILESTONE_DUE_ON_REQUIRED is True
    # github=通用契约基线：开关缺省（消费侧 getattr 派生，无平台枚举分支）
    assert getattr(github, "MILESTONE_DUE_ON_REQUIRED", False) is False


def _mock_transport(monkeypatch):
    calls = []

    def fake_req(provider, method, url, token, payload=None):
        calls.append({"provider": provider, "method": method,
                      "url": url, "payload": payload})
        return {"number": 1070827, "title": "probe", "state": "active"}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    return calls


def test_github_missing_due_on_passes_through(monkeypatch):
    """GitHub：due_on 可选——缺省不拦，payload 不带 due_on 键原样透传。"""
    calls = _mock_transport(monkeypatch)
    out = gra.create_milestone("example-user", "example-repo",
                               gra.MilestoneCreateBody(title="probe"),
                               user=_USER, p="github")
    assert len(calls) == 1, "github 缺 due_on 必须放行到上游"
    assert "due_on" not in (calls[0]["payload"] or {})
    assert out["state"] == "open", "active→open 归一化保持"


@pytest.mark.parametrize("provider", ["gitcode", "gitee"])
def test_v5_family_missing_due_on_rejected_readable(provider, monkeypatch):
    """GitCode/Gitee：缺 due_on → 400 可读文案，且零上游外呼（入口即拦）。"""
    calls = _mock_transport(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        gra.create_milestone("example-user", "example-repo",
                             gra.MilestoneCreateBody(title="probe"),
                             user=_USER, p=provider)
    assert ei.value.status_code == 400
    msg = str(ei.value.detail)
    assert "due_on" in msg and "到期日" in msg, f"400 文案必须可读且点明字段: {msg}"
    assert "GitHub 可省略" in msg, "文案需写死契约差异，帮调用方定位"
    assert not calls, "拦截必须发生在上游调用之前（缺 due_on 不得外呼）"


def test_gitcode_due_on_still_date_only(monkeypatch):
    """gitcode 带 due_on：必填拦截放行 + 日期形态裁剪共存（ISO → YYYY-MM-DD）。"""
    calls = _mock_transport(monkeypatch)
    gra.create_milestone("example-user", "example-repo",
                         gra.MilestoneCreateBody(title="probe", due_on="2026-12-31T23:59:59Z"),
                         user=_USER, p="gitcode")
    assert calls[0]["payload"]["due_on"] == "2026-12-31", (
        "MILESTONE_DUE_DATE_ONLY 裁剪必须与必填拦截正交共存")


def test_gitee_due_on_passthrough_format_kept(monkeypatch):
    """gitee 带 due_on：放行且不做日期裁剪（上游宽容，DATE_ONLY 不适用）。"""
    calls = _mock_transport(monkeypatch)
    gra.create_milestone("example-user", "example-repo",
                         gra.MilestoneCreateBody(title="probe", due_on="2026-12-31"),
                         user=_USER, p="gitee")
    assert calls[0]["payload"]["due_on"] == "2026-12-31"


def test_github_date_only_due_on_normalized_to_datetime(monkeypatch):
    """GitHub：纯日期补成 T00:00:00Z（2026-11-11 实弹 422 "not a valid
    date-time" 的对称修复）——调用方按 v5 习惯传日期也能用。"""
    calls = _mock_transport(monkeypatch)
    gra.create_milestone("example-user", "example-repo",
                         gra.MilestoneCreateBody(title="probe", due_on="2026-12-11"),
                         user=_USER, p="github")
    assert calls[0]["payload"]["due_on"] == "2026-12-11T00:00:00Z"


def test_github_datetime_due_on_passthrough(monkeypatch):
    """GitHub：已是 ISO datetime 形态则原样透传（不重复补 T00:00:00Z）。"""
    calls = _mock_transport(monkeypatch)
    gra.create_milestone("example-user", "example-repo",
                         gra.MilestoneCreateBody(title="probe",
                                                due_on="2026-12-11T00:00:00Z"),
                         user=_USER, p="github")
    assert calls[0]["payload"]["due_on"] == "2026-12-11T00:00:00Z"


def test_update_milestone_due_on_dialect_normalized(monkeypatch):
    """update 端点与 create 同口径：gitcode ISO→日期、github 日期→datetime。"""
    calls = _mock_transport(monkeypatch)
    gra.update_milestone("example-user", "example-repo", "1070827",
                         gra.MilestoneUpdateBody(due_on="2026-12-11T23:59:59Z"),
                         user=_USER, p="gitcode")
    assert calls[0]["payload"]["due_on"] == "2026-12-11", "v5 侧截日期（create 同口径）"
    calls = _mock_transport(monkeypatch)
    gra.update_milestone("example-user", "example-repo", "1070827",
                         gra.MilestoneUpdateBody(due_on="2026-12-11"),
                         user=_USER, p="github")
    assert calls[0]["payload"]["due_on"] == "2026-12-11T00:00:00Z", (
        "github 侧补时间（create 同口径）")
