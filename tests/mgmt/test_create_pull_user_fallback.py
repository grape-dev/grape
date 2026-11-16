"""建 PR 被 403 时回退到任务发起人身份的看护。

背景（0fccafb57）：bot 能推代码但不是仓库成员，POST /pulls 返回 403，代码已在分支上，
只差 PR 壳，因此改用任务发起人 token 重试。回退链路依赖 ``user["id"]`` 解析用户身份。

D105 之后 ``grape_accounts.id`` 是 UUIDv7 字符串，旧实现 ``int(user.get("id") or 0)``
对 UUID 直接抛 ValueError，被外层 except 吞成 debug —— _ut 恒为空，回退路径在任何
现网数据上都不可达。以下三条用例锁定修复后的语义：

1. UUID 主键：回退成功，第二次 POST 用发起人 token；
2. id=0（未归属）：拿不到发起人 token，原样抛 403；
3. 发起人 token 与 bot token 相同（未真正换身份）：不重试，原样抛 403。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api import git_repo_api as api
from src.api.git_repo_api import PRCreateBody
from src.domains.runtime.vcs.providers import gitcode

UUID_ID = "01943f2e-7c3a-7000-8000-0123456789ab"
BOT_TOKEN = "bot_tok"
USER_TOKEN = "user_tok"


def _install(monkeypatch, *, user_token):
    calls = []

    def _fake_req(provider, method, url, token, body=None):
        calls.append({"method": method, "url": url, "token": token})
        if len(calls) == 1:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden"})
        return {"ok": True, "number": 7}

    monkeypatch.setattr(api, "_vcs_req", _fake_req)
    monkeypatch.setattr(api, "_repo_token", lambda owner, repo, user, provider: BOT_TOKEN)
    monkeypatch.setattr(gitcode, "user_token", user_token)
    return calls


def _body():
    return PRCreateBody(title="T", head="feat/x", base="main")


def test_uuid_id_retries_with_initiator_token(monkeypatch):
    seen = []

    def _user_token(username, user_id):
        seen.append((username, user_id))
        return USER_TOKEN

    calls = _install(monkeypatch, user_token=_user_token)
    user = {"id": UUID_ID, "username": "alice", "agent_type": "devops"}

    resp = api.create_pull(owner="o", repo="hello", body=_body(), user=user, p="gitcode")

    assert resp == {"ok": True, "number": 7}
    assert len(calls) == 2, calls
    assert calls[0]["token"] == BOT_TOKEN
    assert calls[1]["token"] == USER_TOKEN
    # UUID 原值透传给 user_token，不被截断/强转
    assert seen == [("alice", UUID_ID)]


def test_id_zero_reraises_original_403(monkeypatch):
    calls = _install(monkeypatch, user_token=lambda username, user_id: "" if not user_id else USER_TOKEN)
    user = {"id": 0, "username": "alice", "agent_type": "devops"}

    with pytest.raises(HTTPException) as exc:
        api.create_pull(owner="o", repo="hello", body=_body(), user=user, p="gitcode")

    assert exc.value.status_code == 403
    assert len(calls) == 1, calls


def test_same_as_bot_token_does_not_retry(monkeypatch):
    calls = _install(monkeypatch, user_token=lambda username, user_id: BOT_TOKEN)
    user = {"id": UUID_ID, "username": "alice", "agent_type": "devops"}

    with pytest.raises(HTTPException) as exc:
        api.create_pull(owner="o", repo="hello", body=_body(), user=user, p="gitcode")

    assert exc.value.status_code == 403
    assert len(calls) == 1, calls