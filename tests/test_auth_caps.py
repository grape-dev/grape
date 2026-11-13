"""能力矩阵与作用域守卫的一致性测试（ADR-2）。

与 test_token_scope_guard.py 的分工：
- 本文件：矩阵本身 + require_no_session_token / require_jwt_only 对 **5 种 auth_type** 的判定；
- test_token_scope_guard.py：D 类端点端到端（session 403 / api_key·jwt 放行）。
"""

import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest
from fastapi import HTTPException

from src.api.auth_caps import (
    AGENT_SELF, CAPABILITIES, INTERACTIVE_ONLY, REPO_SESSION, USER_SCOPE,
    has_capability,
)
from src.api.deps import require_jwt_only, require_no_session_token


def _user(auth_type: str) -> dict:
    return {"id": 1, "username": "tester", "auth_type": auth_type,
            "token": "x", "token_type": auth_type}


ALL_CAPS = (REPO_SESSION, AGENT_SELF, USER_SCOPE, INTERACTIVE_ONLY)
ALL_TYPES = ("session_token", "takeover", "api_key", "jwt", "internal")


def test_matrix_shape():
    """每种已知 token 类型都必须显式登记全部能力位（漏登记 = 新增类型没走评审）。"""
    for t in ALL_TYPES:
        assert t in CAPABILITIES, f"{t} 未登记到能力矩阵"
        assert set(CAPABILITIES[t]) == set(ALL_CAPS), f"{t} 的能力位不完整"


@pytest.mark.parametrize("auth_type,cap,expected", [
    # worker 会话：能干 agent 的活，不能做人才能做的管理
    ("session_token", REPO_SESSION, True),
    ("session_token", AGENT_SELF, True),
    ("session_token", USER_SCOPE, False),
    ("session_token", INTERACTIVE_ONLY, False),
    # 接管会话：只有 repo 会话能力（关键：不能自证 agent、不能做管理）
    ("takeover", REPO_SESSION, True),
    ("takeover", AGENT_SELF, False),
    ("takeover", USER_SCOPE, False),
    ("takeover", INTERACTIVE_ONLY, False),
    # 用户 API Key
    ("api_key", REPO_SESSION, False),
    ("api_key", USER_SCOPE, True),
    ("api_key", INTERACTIVE_ONLY, False),
    # JWT
    ("jwt", USER_SCOPE, True),
    ("jwt", INTERACTIVE_ONLY, True),
    # 内部调用（autopilot 派发）
    ("internal", USER_SCOPE, True),
    ("internal", INTERACTIVE_ONLY, False),
])
def test_capabilities(auth_type, cap, expected):
    assert has_capability(_user(auth_type), cap) is expected


def test_unknown_auth_type_denied():
    """未知类型 fail-closed：不登记就不给任何能力。"""
    for cap in ALL_CAPS:
        assert has_capability(_user("some_new_type"), cap) is False


def test_missing_auth_type_defaults_to_jwt():
    assert has_capability({"id": 1}, USER_SCOPE) is True
    assert has_capability({}, INTERACTIVE_ONLY) is True


class TestUserScopeGuard:
    """require_no_session_token = user_scope 能力门。"""

    @pytest.mark.parametrize("auth_type", ["session_token", "takeover", "some_new_type"])
    def test_rejects_non_user_scope(self, auth_type):
        with pytest.raises(HTTPException) as ei:
            require_no_session_token(_user(auth_type))
        assert ei.value.status_code == 403
        # 保持历史错误文案不变（端点级断言见 test_token_scope_guard.py）
        assert "Session token not allowed" in ei.value.detail.get("message", "")

    @pytest.mark.parametrize("auth_type", ["api_key", "jwt", "internal"])
    def test_passes_user_scope(self, auth_type):
        assert require_no_session_token(_user(auth_type))["auth_type"] == auth_type


class TestInteractiveGuard:
    """require_jwt_only = interactive_only 能力门。"""

    @pytest.mark.parametrize("auth_type", ["session_token", "takeover", "api_key", "internal"])
    def test_rejects_non_jwt(self, auth_type):
        with pytest.raises(HTTPException) as ei:
            require_jwt_only(_user(auth_type))
        assert ei.value.status_code == 403

    def test_passes_jwt(self):
        assert require_jwt_only(_user("jwt"))["auth_type"] == "jwt"