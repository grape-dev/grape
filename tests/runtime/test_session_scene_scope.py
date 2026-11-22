"""会话作用域场景化（tasks notify 等 agent 动作端点）用例：S1–S8。

现场（Issue #3713 评论）：`POST /api/tasks/notify` 返回 403「会话 token 仓库作用域不匹配」，
调用方是 chat —— 它的会话 token 是 `chat:<user_id>` 伪作用域（chat.py:1055/1362/2297），
与任何真实 repo_key 都不可能相等。旧实现只做 `token.repo_key == 目标仓` 严格相等，
于是 chat 永远派不了任务、跨仓下派也会 403。

现在按场景判定：TASK_NOTIFY = task_plane —— 跨仓/伪作用域要「会话用户对目标仓可见」；
自省类（TASK_ACCESS）仍 repo_bound。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.domains.runtime.issues_center import _require_session_repo_token

REPO_A = "gitcode:owner-a/repo-a"
REPO_B = "gitee:owner-b/repo-b"


def _req(token: str = "tk_test") -> Request:
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    return Request(scope)


@pytest.fixture()
def wired(monkeypatch):
    """替换 token 校验与「用户可见仓库」，返回可调的会话上下文。"""
    import src.domains.mgmt.public as mgmt_public
    import src.kernel.auth_sdk.auth_token as at

    state = {"info": {"repo_key": REPO_A, "user_id": "42", "agent_type": "chat"},
             "visible": {REPO_B}}

    monkeypatch.setattr(at, "verify_session_token",
                        lambda token: dict(state["info"]) if token != "bad" else None)
    monkeypatch.setattr(mgmt_public, "get_managed_repos",
                        lambda uid, *a, **k: [{"repo_key": rk} for rk in state["visible"]])
    return state


def test_s1_chat_pseudo_scope_allowed_when_user_sees_target(wired):
    """S1：chat 伪作用域 + 用户对目标仓可见 → 放行（这就是被误伤的那条链路）。"""
    wired["info"]["repo_key"] = "chat:42"
    info = _require_session_repo_token(_req(), REPO_B)
    assert info["agent_type"] == "chat"


def test_s2_chat_pseudo_scope_denied_when_not_visible(wired):
    """S2：chat 伪作用域但用户对目标仓无可见权 → 403 SESSION_NOT_REPO_BOUND。"""
    wired["info"]["repo_key"] = "chat:42"
    wired["visible"] = set()
    with pytest.raises(HTTPException) as ei:
        _require_session_repo_token(_req(), REPO_B)
    assert ei.value.status_code == 403
    assert "SESSION_NOT_REPO_BOUND" in str(ei.value.detail)


def test_s3_same_repo_allowed(wired):
    """S3：同仓照常放行（绝大多数事件 agent 的路径）。"""
    info = _require_session_repo_token(_req(), REPO_A)
    assert info["repo_key"] == REPO_A


def test_s4_cross_repo_dispatch_allowed_with_visibility(wired):
    """S4：跨仓下派（源仓 agent 给子仓派任务）+ 用户可见 → 放行。"""
    wired["info"]["repo_key"] = "gitcode:owner-a/parent-repo"
    wired["info"]["agent_type"] = "issue"
    info = _require_session_repo_token(_req(), REPO_B)
    assert info["agent_type"] == "issue"


def test_s5_cross_repo_denied_without_visibility(wired):
    """S5：跨仓且用户不可见 → 403 CROSS_REPO_DENIED（不是笼统"不匹配"）。"""
    wired["info"]["repo_key"] = "gitcode:owner-a/parent-repo"
    wired["visible"] = set()
    with pytest.raises(HTTPException) as ei:
        _require_session_repo_token(_req(), REPO_B)
    assert ei.value.status_code == 403
    assert "CROSS_REPO_DENIED" in str(ei.value.detail)


def test_s6_self_task_access_stays_repo_bound(wired):
    """S6：自省类场景（TASK_ACCESS）即便用户可见目标仓，也不跨仓。"""
    from src.kernel.scene_policy import Scene

    wired["info"]["repo_key"] = "gitcode:owner-a/parent-repo"
    with pytest.raises(HTTPException) as ei:
        _require_session_repo_token(_req(), REPO_B, scene=Scene.TASK_ACCESS)
    assert ei.value.status_code == 403
    assert "REPO_SCOPE_MISMATCH" in str(ei.value.detail)


def test_s7_missing_token_rejected(wired):
    with pytest.raises(HTTPException) as ei:
        _require_session_repo_token(_req(token=""), REPO_A)
    assert ei.value.status_code == 403
    assert "需会话 token" in str(ei.value.detail)


def test_s8_invalid_token_rejected(wired):
    with pytest.raises(HTTPException) as ei:
        _require_session_repo_token(_req(token="bad"), REPO_A)
    assert ei.value.status_code == 403
    assert "会话 token 无效" in str(ei.value.detail)