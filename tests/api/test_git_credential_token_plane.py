"""git-credential 的 token 平面：显式 bot 闸 + 归属者兜底 + 可判定错误码。

背景（三层漂移，证据见 docs/weekly-11/git-credential-token-plane-revision.md）：
  · 初始设计 R2.1 是 `repo bot → global bot → host_user_id 用户 token`；
  · 策略层给 RepoBot/GlobalBot 加了 path-B 守卫（host_user_id 非空即跳过），
    于是建仓时写了 host_user_id 的仓库，之后配 bot 也用不到；
  · 2026-11-13 git-credential 的 `_bot()` 又用 `read_stored_token`（逐策略 _fetch_stored：
    不刷新、不看过期，UserToken 只读 OAuth），gitee（只有 PAT）这条腿恒空；
    `_user()` 只看会话用户（agent 的 tk_ 会话 user_id 常 0）→ PAT 分支被跳过 → 整条链断，
    且失败被伪装成 502。

本组用例锁七件事：
  1. `_bot()` 不得再调 `read_stored_token`，也不得裸传 bypass_path_b；
  2. 闸是两个信号：`_has_bot_row`（显式意图）+ `_gate_bot`（值可用）；
  3. 显式 bot 值可用 → 直取 rc 值，不经策略链（不会顺带解锁全局 bot）；
  4. 显式 bot 行存在但值不可用 → 424 BOT_CREDENTIAL_INVALID，**不得**落全局 bot / 用户身份；
  5. 无 bot 行 → devops 先归属者（host_user_id），再会话用户；chat 反过来（会话优先）；
  6. 全空 → 424 CREDENTIAL_NOT_CONFIGURED（配置问题不再伪装成 502）；
  7. 归属者查询抛错不打断整条链。
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes import repos as repos_routes
from src.domains.mgmt.routes.repos import _repo_platform_token

SESSION_USER = {"auth_type": "session_token", "id": "0", "username": "", "agent_type": "coding"}
CHAT_USER = {"auth_type": "session_token", "id": "42", "username": "alice", "agent_type": "chat"}


@pytest.fixture()
def resolver_spy(monkeypatch):
    """替换 TokenResolver：记录是否被调用、bypass 取值、返回值可控。"""
    import src.kernel.token_resolver as tr

    class _Spy:
        bypass_seen: list = []
        token = ""

        def __init__(self, provider, *, bypass_path_b=False):
            _Spy.bypass_seen.append(bool(bypass_path_b))

        def get_token(self, repo_key):
            return _Spy.token

    _Spy.bypass_seen = []
    _Spy.token = ""
    monkeypatch.setattr(tr, "TokenResolver", _Spy)
    return _Spy


def _set_bot_row(monkeypatch, value: str | None, row_exists: bool = True):
    """value=None → 无行；value='' → 行存在但值被清空。"""
    import src.domains.mgmt.user.db as udb

    def _meta(repo_key, cred_type):
        if not row_exists:
            return None
        return {"value": value or "", "id": "cred-1"}

    monkeypatch.setattr(udb, "get_repo_credential_meta", _meta)


def _set_host(monkeypatch, host_user_id: str, host_login: str = "owner-login"):
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "get_host_user_id", lambda repo_key: host_user_id)
    monkeypatch.setattr(udb, "get_host_vcs_username", lambda repo_key: host_login)


def _set_user_token(monkeypatch, value: str, calls: list | None = None):
    import src.api.deps as deps

    def _get(username, user_id="", provider="gitcode"):
        if calls is not None:
            calls.append({"username": username, "user_id": user_id, "provider": provider})
        return value

    monkeypatch.setattr(deps, "get_user_token_for", _get)


def test_source_never_calls_read_stored_token():
    """源码锁：不得用 read_stored_token；bypass 不得裸传（必须走显式 bot 值直取）。"""
    src = inspect.getsource(repos_routes._repo_platform_token)
    assert ".read_stored_token(" not in src, (
        "git-credential 的 _bot() 仍在调 read_stored_token：逐策略 _fetch_stored 只读 OAuth、"
        "且带 path-B 守卫 → gitee（只有 PAT）这条腿恒空")
    assert "_has_bot_row" in src and "_gate_bot" in src, "闸必须区分「行存在」与「值可用」"
    assert "bypass_path_b=True" not in src, "不得裸传 bypass：会把全局 bot 一并解锁"


def test_explicit_bot_value_returned_without_strategy_chain(monkeypatch, resolver_spy):
    """显式 bot 值可用 → 直取 rc 值，不碰策略链（避免顺带解锁全局 bot）。"""
    _set_bot_row(monkeypatch, "bot-token-value")
    user_calls: list = []
    _set_user_token(monkeypatch, "user-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "bot-token-value"
    assert resolver_spy.bypass_seen == [], "显式 bot 直取，不应构造 TokenResolver"
    assert user_calls == [], "有 bot 时不该再解析用户凭据"


def test_bot_row_with_empty_value_raises_424_and_no_global_leak(monkeypatch, resolver_spy):
    """行存在但 value 被清空 → 424，绝不落全局 bot（否则身份错、403 难查）。"""
    _set_bot_row(monkeypatch, "")
    resolver_spy.token = "global-bot-should-not-be-used"
    user_calls: list = []
    _set_user_token(monkeypatch, "user-pat", user_calls)

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "BOT_CREDENTIAL_INVALID"
    assert ei.value.detail["stage"] == "empty", "值被清空要能区分于「无行」"
    assert resolver_spy.bypass_seen == [], "不得为拿全局 bot 构造 resolver"
    assert user_calls == [], "bot 失效时不静默降级"


def test_no_bot_row_devops_prefers_owner(monkeypatch, resolver_spy):
    """无 bot 行 + devops：先归属者（host_user_id）凭据 —— 路径 B 原始语义。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""
    _set_host(monkeypatch, "5", "owner-login")
    user_calls: list = []
    _set_user_token(monkeypatch, "owner-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "owner-pat"
    assert user_calls and user_calls[0]["user_id"] == "5"
    assert user_calls[0]["username"] == "owner-login"
    assert user_calls[0]["provider"] == "gitee"


def test_no_bot_row_chat_prefers_session_user(monkeypatch, resolver_spy):
    """无 bot 行 + chat：会话用户优先 —— 不能替仓库 owner 代签（否则权限放大）。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""
    _set_host(monkeypatch, "5", "owner-login")
    user_calls: list = []
    _set_user_token(monkeypatch, "session-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(CHAT_USER), "gitee")

    assert tok == "session-pat"
    assert user_calls and user_calls[0]["user_id"] == "42"
    assert user_calls[0]["username"] == "alice"


def test_no_bot_row_path_a_uses_global_bot(monkeypatch, resolver_spy):
    """无 bot 行 + path-A（host_user_id 空）：策略链正常给出全局 bot，12 个存量仓照旧活。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = "global-bot-token"
    _set_host(monkeypatch, "")

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "global-bot-token"
    assert resolver_spy.bypass_seen == [False], "无显式 bot 时不得开 bypass"


def test_no_bot_no_owner_falls_back_to_session_user(monkeypatch, resolver_spy):
    """无 bot、无归属者：落当前会话用户。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""
    _set_host(monkeypatch, "")
    user_calls: list = []
    _set_user_token(monkeypatch, "session-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER, id="42", username="alice"), "gitee")

    assert tok == "session-pat"
    assert user_calls and user_calls[0]["user_id"] == "42"


def _set_bot_expired(monkeypatch, expired: bool):
    """managed_repos.bot_token_status：R18.2.2 过期短路的读侧。"""
    import src.domains.mgmt.public as mp

    monkeypatch.setattr(mp, "repo_bot_token_status_any",
                        lambda repo_key: "expired" if expired else "")


def test_bot_row_expired_raises_424(monkeypatch, resolver_spy):
    """行存在 + 值非空 + bot_token_status='expired' → 424（保留原始过期短路）。"""
    _set_bot_row(monkeypatch, "bot-token-value")
    _set_bot_expired(monkeypatch, True)
    resolver_spy.token = "global-bot-should-not-be-used"
    user_calls: list = []
    _set_user_token(monkeypatch, "user-pat", user_calls)

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "BOT_CREDENTIAL_INVALID"
    assert ei.value.detail["stage"] == "expired", "过期必须可区分，否则运维不知道该轮换"
    assert resolver_spy.bypass_seen == []
    assert user_calls == []


def test_bot_row_not_expired_returns_value(monkeypatch, resolver_spy):
    """状态为空（未标过期）→ 正常返回 bot 值，短路不误伤。"""
    _set_bot_row(monkeypatch, "bot-token-value")
    _set_bot_expired(monkeypatch, False)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "bot-token-value"


def test_all_empty_raises_424_not_502(monkeypatch, resolver_spy):
    """全空 → 424 CREDENTIAL_NOT_CONFIGURED（配置问题不伪装成上游故障）。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""
    _set_host(monkeypatch, "")
    _set_user_token(monkeypatch, "")

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "CREDENTIAL_NOT_CONFIGURED"


def test_owner_username_falls_back_to_repo_owner(monkeypatch, resolver_spy):
    """host_vcs_username 为空时回落仓库 owner 段（OAuth 档按 username 匹配要它）。"""
    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""
    _set_host(monkeypatch, "5", "")
    user_calls: list = []
    _set_user_token(monkeypatch, "owner-pat", user_calls)

    tok = _repo_platform_token("acme", "widget", dict(SESSION_USER), "gitee")

    assert tok == "owner-pat"
    assert user_calls and user_calls[0]["username"] == "acme"


def test_host_owner_lookup_failure_does_not_break_fallback(monkeypatch, resolver_spy):
    """归属者查询抛错时继续落会话用户，不打断整条链。"""
    import src.domains.mgmt.user.db as udb

    _set_bot_row(monkeypatch, None, row_exists=False)
    resolver_spy.token = ""

    def _boom(repo_key):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(udb, "get_host_user_id", _boom)
    _set_user_token(monkeypatch, "session-pat")

    tok = _repo_platform_token("o", "r", dict(SESSION_USER, id="7", username="bob"), "gitee")
    assert tok == "session-pat"

def _set_bot_row_undecryptable(monkeypatch):
    """行存在但密文解不开（密钥不匹配/行被破坏）——与「无行」必须区分。"""
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "get_repo_credential_meta",
                        lambda repo_key, cred_type: {"value": "", "id": "cred-1",
                                                     "undecryptable": True})


def test_bot_row_undecryptable_raises_424_with_stage(monkeypatch, resolver_spy):
    """行存在但解不开 → 424 stage=undecryptable，不得静默降级到归属者/会话用户。

    历史缺陷：get_repo_credential_meta 把解密异常吞成 None → 与「无行」不可区分
    → 走 _user() 静默用归属者 PAT（D90 明令禁止的身份混用）。
    """
    _set_bot_row_undecryptable(monkeypatch)
    resolver_spy.token = "global-bot-should-not-be-used"
    _set_host(monkeypatch, "owner-1")
    user_calls: list = []
    _set_user_token(monkeypatch, "owner-pat-should-not-be-used", user_calls)

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "BOT_CREDENTIAL_INVALID"
    assert ei.value.detail["stage"] == "undecryptable"
    assert user_calls == [], "解不开的 bot 行不得静默降级到用户身份"
    assert resolver_spy.bypass_seen == [], "也不得落到策略链的全局 bot"


def test_meta_distinguishes_undecryptable_from_missing_row(monkeypatch):
    """get_repo_credential_meta 自身的三态：无行=None / 解不开=undecryptable / 可用=value。"""
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "_insight_public", type("X", (), {
        "repo_credential_meta": staticmethod(lambda rk, ct: ("garbage-ciphertext", "id-1"))
    })(), raising=False)

    def _boom(raw):
        raise ValueError("bad padding")

    monkeypatch.setattr(udb, "_decrypt_repo_secret", _boom)
    got = udb.get_repo_credential_meta("o/r", "bot")
    assert got and got["undecryptable"] is True, "解密失败必须与无行区分（不能返回 None）"
    assert got["value"] == "" and got["id"] == "id-1"


# ── P0：gitcode/github 的 _getters 分支也必须做 owner 回落（2026-11-21）──────────
# 此前该分支的 _user() 只看会话身份，而 devops 任务的会话是仓库身份（user_id=0）
# → getter("", user_id=0) 恒空；叠加 bot 档被 db 层 path-B 守卫按设计挡掉，
# gitcode/github 的 path-B 仓整条 git-credential 链断（502/424）。

class _FakeGetterModule:
    """模拟 repo_token_getters 引用的模块（bot/user getter）。"""

    bot_token = ""
    user_tokens: dict = {}

    @staticmethod
    def bot_fn(repo_key=""):
        return _FakeGetterModule.bot_token

    @staticmethod
    def user_fn(username="", user_id=0):
        return _FakeGetterModule.user_tokens.get(str(user_id), "")


def _set_getters(monkeypatch, bot_token: str = "", user_tokens: dict | None = None):
    import importlib

    import src.domains.mgmt.user.repo_manager as rm

    _FakeGetterModule.bot_token = bot_token
    _FakeGetterModule.user_tokens = user_tokens or {}
    monkeypatch.setattr(
        rm, "repo_token_getters",
        lambda provider: {"bot": (_FakeGetterModule, "bot_fn"),
                          "user": (_FakeGetterModule, "user_fn"),
                          "use_provider_key": True},
        raising=True,
    )
    monkeypatch.setattr(importlib, "import_module", lambda name: _FakeGetterModule, raising=True)


def test_getters_devops_path_b_uses_owner_when_session_uid_zero(monkeypatch):
    """P0：gitcode path-B + devops（uid=0）→ 必须走归属者凭据（原始 R2.1 语义）。"""
    _set_getters(monkeypatch, bot_token="", user_tokens={"5": "owner-pat"})
    _set_host(monkeypatch, "5", "owner-login")

    tok = _repo_platform_token("acme", "widget", dict(SESSION_USER, id="0"), "gitcode")

    assert tok == "owner-pat", "devops 会话 uid=0 时不得直接空手，要回落归属者"


def test_getters_devops_no_owner_falls_back_to_session(monkeypatch):
    """无归属者 → 仍走会话用户（不凭空造 token）。"""
    _set_getters(monkeypatch, bot_token="", user_tokens={"42": "session-pat"})
    _set_host(monkeypatch, "", "")

    tok = _repo_platform_token("acme", "widget", dict(SESSION_USER, id="42", username="alice"), "gitcode")

    assert tok == "session-pat"


def test_getters_chat_never_uses_owner(monkeypatch):
    """chat 会话即便仓库有归属者，也不得代签 owner 的 PAT（权限放大）。"""
    _set_getters(monkeypatch, bot_token="", user_tokens={"42": "session-pat", "5": "owner-pat"})
    _set_host(monkeypatch, "5", "owner-login")

    tok = _repo_platform_token("acme", "widget", dict(CHAT_USER), "gitcode")

    assert tok == "session-pat", "chat 必须会话优先"


def test_getters_all_empty_reports_424(monkeypatch):
    """bot 空 + 归属者空 + 会话空 → 424 CREDENTIAL_NOT_CONFIGURED（不再是 502）。"""
    _set_getters(monkeypatch, bot_token="", user_tokens={})
    _set_host(monkeypatch, "", "")

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("acme", "widget", dict(SESSION_USER), "gitcode")

    assert ei.value.status_code == 424
