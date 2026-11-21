"""git-credential 的授权面（QA A9）：仓库作用域 + chat 不得回落归属者。

两条缺陷（证据见 PR #1946 第 6 轮架构守护审查）：

  1. 会话 token 只校验了 auth_type，没有把路径 {owner}/{repo} 与 token 的 repo_key
     比对 → 持 A 仓 tk_ 的 agent 可以对 B 仓换券，拿到 B 仓凭据；
  2. `chat` 分支的用户档第二级回落到归属者 → 非 owner 的 chat 会话在自己没有凭据时
     拿到 owner 的 PAT，可以 owner 身份 clone/push/评论（代签 + 权限放大）。

本组用例锁五件事：
  S1 跨仓换券 → 403（仓库作用域不匹配），不得走到取券；
  S2 同仓 → 不因作用域被拒（回归保护）；
  S3 路径 owner 带 provider 前缀但语义同仓 → 通过；
  S4 chat 自己无凭据、归属者有凭据 → 不得返回归属者 token（期望 424 可判定码）；
  S5 devops（非 chat）仍可回落归属者（path-B 原始语义，回归保护）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes.repos import _repo_platform_token

OWNER_PAT = "owner-pat"


def _wire(monkeypatch, *, host_user_id: str = "owner-1", host_login: str = "owner",
          owner_token: str = OWNER_PAT, session_token: str = ""):
    import src.api.deps as deps
    import src.domains.mgmt.user.db as udb
    import src.domains.mgmt.user.repo_manager as rm
    import src.kernel.token_resolver as tr

    monkeypatch.setattr(rm, "repo_token_getters", lambda provider: {})
    monkeypatch.setattr(udb, "get_repo_credential_meta", lambda repo_key, cred_type: None)
    monkeypatch.setattr(udb, "get_host_user_id", lambda repo_key: host_user_id)
    monkeypatch.setattr(udb, "get_host_vcs_username", lambda repo_key: host_login)

    def _get_user_token(username, user_id="", provider="gitcode"):
        if username == host_login:
            return owner_token
        return session_token

    monkeypatch.setattr(deps, "get_user_token_for", _get_user_token)

    class _EmptyResolver:
        def __init__(self, provider, *, bypass_path_b=False):
            pass

        def get_token(self, repo_key):
            return ""

    monkeypatch.setattr(tr, "TokenResolver", _EmptyResolver)
    monkeypatch.setattr(tr, "create_provider", lambda provider: object())


def _session(repo_key: str = "", agent_type: str = "coding", uid="0", username=""):
    return {"auth_type": "session_token", "id": uid, "username": username,
            "agent_type": agent_type, "repo_key": repo_key}


def test_s1_cross_repo_token_is_rejected(monkeypatch):
    """S1：tk_ 绑定 a/r，路径却是 b/r → 403，不得换券。"""
    _wire(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("b", "r", _session("gitcode:a/r"), "gitcode")
    assert ei.value.status_code == 403
    assert "作用域" in str(ei.value.detail)


def test_s2_same_repo_passes_scope_gate(monkeypatch):
    """S2：tk_ 绑定与路径一致 → 不因作用域被拒（其余档位照常返回 owner token）。"""
    _wire(monkeypatch)
    tok = _repo_platform_token("a", "r", _session("gitcode:a/r"), "gitcode")
    assert tok == OWNER_PAT


def test_s3_provider_prefixed_path_matches(monkeypatch):
    """S3：路径 owner 带 `gitcode:` 前缀、语义同仓 → 通过（Issue #466 形态）。"""
    _wire(monkeypatch)
    tok = _repo_platform_token("gitcode:a", "r", _session("gitcode:a/r"), "gitcode")
    assert tok == OWNER_PAT


def test_s4_chat_must_not_fall_back_to_owner(monkeypatch):
    """S4：chat 会话自己无凭据、归属者有 → 不得返回归属者 PAT（此前会返回）。"""
    _wire(monkeypatch, session_token="")
    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("a", "r", _session("gitcode:a/r", agent_type="chat",
                                                uid="42", username="alice"), "gitcode")
    assert ei.value.status_code == 424
    assert "CREDENTIAL_NOT_CONFIGURED" in str(ei.value.detail)


def test_s5_devops_still_falls_back_to_owner(monkeypatch):
    """S5：devops 事件（非 chat）维持 path-B 原始语义 —— 归属者优先。"""
    _wire(monkeypatch)
    tok = _repo_platform_token("a", "r", _session("gitcode:a/r", agent_type="coding"), "gitcode")
    assert tok == OWNER_PAT


def test_s6_token_without_repo_key_is_not_broken(monkeypatch):
    """S6：历史 token 上下文没有 repo_key → 不因作用域误拒（向后兼容）。"""
    _wire(monkeypatch)
    tok = _repo_platform_token("a", "r", _session(""), "gitcode")
    assert tok == OWNER_PAT

def test_s7_provider_must_match(monkeypatch):
    """S7：`gitcode:o/r` 的会话打 `?provider=gitee` 读同名仓 → 403（平台不匹配）。

    同名不同平台是两套凭据；跨平台关联仓需要显式登记关联关系，不能靠这里放开。
    """
    _wire(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("a", "r", _session("gitcode:a/r"), "gitee")
    assert ei.value.status_code == 403
    assert "平台不匹配" in str(ei.value.detail)


def test_s8_bare_repo_key_defaults_to_gitcode(monkeypatch):
    """S8：无前缀 repo_key 按 gitcode 解释（to_repo_key 既有约定）→ 请求 gitcode 通过。"""
    _wire(monkeypatch)
    tok = _repo_platform_token("a", "r", _session("a/r"), "gitcode")
    assert tok == OWNER_PAT


def test_s9_bare_repo_key_cannot_borrow_other_provider(monkeypatch):
    """S9：无前缀 repo_key 请求 gitee → 403（裸键不等于"任意平台"）。"""
    _wire(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("a", "r", _session("a/r"), "gitee")
    assert ei.value.status_code == 403
