"""P3 小项二回归：experience 身份缺失 502 → 409 语义纠正。

背景：POST /api/scenes/market/{id}/experience 在当前用户 gitcode 身份不可用时
返回 502「无法获取当前用户 gitcode 身份」——身份缺失是客户端状态问题，非网关/
上游故障，502 语义错误。纠正为 409 MISSING_IDENTITY 并附明确指引（需绑定/重新
授权平台身份）；上游真故障仍保留 502。

锁定的契约：
- 身份缺失（平台返回空 login）→ 409 {code:409, error_code:"MISSING_IDENTITY", message 含绑定指引}
- user_login 为空返回 → 同上 409
- 其他异常（上游真故障）→ 仍为 502（防误伤）
- with_oauth_token 的 token 刷新重试语义不变（异常继承 RuntimeError，消息口径不变）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes import scenes as scenes_routes
from src.domains.mgmt.routes.scenes import (
    experience_market_scene,
    _scene_user_login_via_card,
    _SceneUserIdentityMissing,
)


_USER = {"id": 1, "username": "p3b_user", "login_provider": "gitcode"}
_SCENE = {
    "id": 4321,
    "recommended_template": "tpl-p3b",
    "nodes_json": "{}",
    "onboarding": "",
}


@pytest.fixture()
def scene_env(monkeypatch):
    """搭好 handler 前置依赖（场景/模板存在，provider=gitcode），只留 login 分支可变。"""
    monkeypatch.setattr(
        "src.domains.mgmt.user.db.get_scene_market_by_id",
        lambda mid: dict(_SCENE) if int(mid) == 4321 else None,
    )
    monkeypatch.setattr(
        "src.domains.mgmt.routes.templates._find_template",
        lambda tid: {"template": {"id": tid, "default_repo_name": "p3b-repo"}},
    )
    monkeypatch.setattr(scenes_routes, "_scene_user_provider", lambda user: "gitcode")
    return monkeypatch


def _run(market_id=4321, user=_USER):
    return asyncio.run(experience_market_scene(market_id, user))


class TestIdentityMissing409:
    def test_empty_login_identity_missing_returns_409(self, scene_env):
        """平台返回空 login（身份缺失）→ 409 MISSING_IDENTITY，而非 502。"""
        scene_env.setattr(
            scenes_routes, "_scene_user_login_via_card",
            lambda user, provider: (_ for _ in ()).throw(_SceneUserIdentityMissing(
                "GitCode API GET /user returned empty login (401): token may be invalid")),
        )
        with pytest.raises(HTTPException) as ei:
            _run()
        assert ei.value.status_code == 409
        detail = ei.value.detail
        assert detail["code"] == 409
        assert detail["error_code"] == "MISSING_IDENTITY"
        assert "绑定" in detail["message"] or "授权" in detail["message"]
        assert "gitcode" in detail["message"].lower()

    def test_empty_user_login_return_returns_409(self, scene_env):
        """login 解析返回空字符串（身份不可用）→ 409 MISSING_IDENTITY。"""
        scene_env.setattr(scenes_routes, "_scene_user_login_via_card", lambda user, provider: "")
        with pytest.raises(HTTPException) as ei:
            _run()
        assert ei.value.status_code == 409
        assert ei.value.detail["error_code"] == "MISSING_IDENTITY"

    def test_upstream_failure_still_502(self, scene_env):
        """防误伤：非身份缺失的解析异常（上游真故障）→ 仍为 502。"""
        scene_env.setattr(
            scenes_routes, "_scene_user_login_via_card",
            lambda user, provider: (_ for _ in ()).throw(ConnectionError("upstream connect timeout")),
        )
        with pytest.raises(HTTPException) as ei:
            _run()
        assert ei.value.status_code == 502

    def test_identity_missing_is_runtime_error_subclass(self):
        """保持 with_oauth_token 的异常族兼容；消息不得伪造 (401)（HTTP 401 才触发刷新）。"""
        assert issubclass(_SceneUserIdentityMissing, RuntimeError)
        msg = str(_SceneUserIdentityMissing("GitCode API GET /user returned empty login: no usable user identity"))
        assert "empty login" in msg
        assert "(401)" not in msg

    def test_login_via_card_raises_identity_missing_on_empty_login(self, scene_env, monkeypatch):
        """_scene_user_login_via_card：平台返回空 login 时抛 _SceneUserIdentityMissing。"""
        class _FakeProv:
            def get_user_login(self, token):
                return ""

        monkeypatch.setattr("src.kernel.token_resolver.create_provider", lambda p: _FakeProv())
        monkeypatch.setattr(
            "src.domains.runtime.vcs.platform_card.get_platform_card",
            lambda p: type("C", (), {"name": "GitCode"})(),
        )
        monkeypatch.setattr(
            scenes_routes, "with_oauth_token",
            lambda user, provider, fn, *a, **kw: fn("tok"),
        )
        with pytest.raises(_SceneUserIdentityMissing):
            _scene_user_login_via_card(_USER, "gitcode")
