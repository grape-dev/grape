"""接管 token（tko_）签发 / 校验 / 吊销 / 认证分发。

对应设计文档 §2：新前缀 + kind='takeover' + session_key='takeover:<task_id>'。
"""

import os

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.auth_token import (
    SESSION_TOKEN_PREFIX, TAKEOVER_TOKEN_PREFIX,
    issue_session_token, issue_takeover_token, revoke_takeover_token,
    takeover_session_key, verify_session_token, verify_takeover_token,
)
from src.api.deps import get_current_user_or_api_key


def _req(token: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


def _issue(task_id: str = "t_tko_1", **kw) -> dict:
    return issue_takeover_token(task_id=task_id, repo_key="gitcode:o/r",
                                agent_type="coding", user_id=1, **kw)


def test_issue_and_verify():
    t = _issue("t_issue_ok")
    assert t and t["token"].startswith(TAKEOVER_TOKEN_PREFIX)
    info = verify_takeover_token(t["token"])
    assert info is not None
    assert info["session_key"] == "takeover:t_issue_ok"
    assert info["repo_key"] == "gitcode:o/r"
    assert info["agent_type"] == "coding"
    assert info["user_id"] == 1


def test_session_key_namespace():
    """接管 key 必须以 takeover: 前缀，避免被 cancel 的 revoke(task_id) 命中。"""
    assert takeover_session_key("t_x") == "takeover:t_x"
    assert takeover_session_key("takeover:t_x") == "takeover:t_x"


def test_kind_separation():
    """tk_ 与 tko_ 互不通用（各带 kind 过滤）。"""
    tk = issue_session_token(session_key="t_worker_1", repo_key="gitcode:o/r",
                             agent_type="coding", user_id=1)
    assert tk.startswith(SESSION_TOKEN_PREFIX)
    assert verify_takeover_token(tk) is None

    tko = _issue("t_kind_sep")["token"]
    assert verify_session_token(tko) is None


def test_revoke_is_immediate():
    t = _issue("t_revoke")["token"]
    assert verify_takeover_token(t) is not None
    assert revoke_takeover_token("t_revoke") is True
    assert verify_takeover_token(t) is None


def test_expired_token_rejected():
    # 用 auth_token 模块自己的连接改过期时间：全树收集时其它模块可能 reload
    # session_manager，另取新连接会写到别的库，签发/校验却还在旧库 → 断言假失败
    from src.api.auth_token import db, db_lock
    t = _issue("t_expired", ttl=1)["token"]
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE session_api_tokens SET expires_at=1 WHERE session_key=?", ("takeover:t_expired",))
        db.commit()
    assert verify_takeover_token(t) is None


def test_dispatch_yields_takeover_auth_type():
    t = _issue("t_dispatch")["token"]
    user = get_current_user_or_api_key(_req(t))
    assert user["auth_type"] == "takeover"
    assert user["token_type"] == "takeover"
    assert user["kind"] == "takeover"
    assert user["repo_key"] == "gitcode:o/r"
    assert user["agent_type"] == "coding"
    assert user["session_key"] == "takeover:t_dispatch"


def test_dispatch_invalid_tko_401():
    with pytest.raises(HTTPException) as ei:
        get_current_user_or_api_key(_req("tko_not_a_real_token"))
    assert ei.value.status_code == 401
    assert ei.value.detail.get("error_code") == "TAKEOVER_TOKEN_INVALID"