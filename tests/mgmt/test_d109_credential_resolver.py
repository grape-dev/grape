"""D109 凭据代理（credential_resolver）：解析序与原因码（2026-11-14 简化口径）。

OAuth 与 PAT 两个模式并存：① PAT（显式配置优先）② 渠道令牌（PAT 缺席时的正常回落）。
channel-repo-links.json 的配对**不参与**取 token 判定——它只决定登录后是否写用户平台关系。
判据全部打桩（不发网络、不连真库），只验证"选谁 + 为什么"。
"""
from __future__ import annotations

import pytest

from src.domains.platform import credential_resolver as cr


@pytest.fixture()
def stubs(monkeypatch):
    box = {"pat": "", "identities": [], "token": "", "status": "1"}

    monkeypatch.setattr(cr, "_pat_token", lambda u, p: box["pat"])
    monkeypatch.setattr(cr, "_active_channels",
                        lambda u: [{"provider": i[0], "status": i[1]} for i in box["identities"]])
    monkeypatch.setattr(cr, "_channel_token", lambda s, u, c: box["token"])

    return box


def test_pat_wins_over_channel(stubs):
    stubs.update(pat="pat-token", identities=[("gitee", "1")], token="oauth-token")
    c = cr.resolve_user_credential("u1", "gitee", need_scope="projects")
    assert c.ok and c.source == "pat" and c.token == "pat-token"


def test_channel_token_when_pat_missing(stubs):
    """N2/UC-32 反证：把 OAuth 能力砍成 PAT-only 时本用例必红。"""
    stubs.update(pat="", identities=[("gitee", "1")], token="oauth-token")
    c = cr.resolve_user_credential("u1", "gitee", need_scope="projects")
    assert c.ok and c.source == "oauth:gitee" and c.token == "oauth-token"


def test_any_active_channel_is_used(stubs):
    """不按渠道名/关系过滤：有活跃渠道就用它的令牌（能力够不够由上游 403 回答）。"""
    stubs.update(pat="", identities=[("huawei", "1")], token="hw-token")
    c = cr.resolve_user_credential("u1", "codearts")
    assert c.ok and c.source == "oauth:huawei" and c.token == "hw-token"


def test_scope_does_not_gate(stubs):
    """scope 不参与判定（2026-11-14 用户裁定）。"""
    stubs.update(pat="", identities=[("gitee", "1")], token="oauth-token")
    c = cr.resolve_user_credential("u1", "gitee", need_scope="projects issues pull_requests")
    assert c.ok and c.source == "oauth:gitee"


def test_expired_token(stubs):
    stubs.update(identities=[("gitee", "4")], token="")
    c = cr.resolve_user_credential("u1", "gitee")
    assert not c.ok and c.reason == cr.TOKEN_EXPIRED


def test_no_channel(stubs):
    stubs.update(identities=[], pat="")
    c = cr.resolve_user_credential("u1", "gitee")
    assert not c.ok and c.reason == cr.NO_CHANNEL


def test_describe_never_leaks_token(stubs):
    stubs.update(pat="secret-token")
    d = cr.describe("u1", "gitee")
    assert "secret-token" not in str(d) and d["source"] == "pat" and d["available"] is True