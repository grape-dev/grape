"""git-credential 的 token 平面：唯一选择器 + 归属者兜底 + 可判定错误码。

背景（三层漂移，证据见 docs/weekly-11/git-credential-token-plane-revision.md）：
  · 初始设计 R2.1 是 `repo bot → global bot → host_user_id 用户 token`；
  · 策略层给 RepoBot/GlobalBot 加了 path-B 守卫（host_user_id 非空即跳过），
    于是建仓时写了 host_user_id 的仓库，之后配 bot 也用不到；
  · 2026-11-13 git-credential 的 `_bot()` 又用 `read_stored_token`（逐策略 _fetch_stored：
    不刷新、不看过期，UserToken 只读 OAuth），gitee（只有 PAT）这条腿恒空；
    `_user()` 只看会话用户（agent 的 tk_ 会话 user_id 常 0）→ PAT 分支被跳过 → 整条链断，
    且失败被伪装成 502。

2026-11-22（Issue #3713）第二层修订：`_bot()` 曾自己直读 rc 行并做三段判定
（empty/undecryptable/expired），与策略链是两套实现 → 同一份 rc 行两条链两种答案
（私有 path-B 仓换券 200 / 建树 token 空）。三段判定已上收到
`TokenResolver.resolve_exec_token`，本路由只做**渲染**。用例因此改成
「替换共享选择器 + 锁路由侧的档位/错误码」：

  1. 源码锁：`_bot()` 必须调选择器，不得直读 rc 行/过期列，不得出现 bypass；
  2. 选择器给出 token → 直接用它，不再解析用户凭据；
  3. 选择器给出 stage（empty/undecryptable/expired）→ 424 BOT_CREDENTIAL_INVALID，
     **不得**落全局 bot / 用户身份；
  4. 选择器全空 → devops 先归属者（host_user_id），chat 反过来（会话优先）；
  5. 全空且无归属者 → 424 CREDENTIAL_NOT_CONFIGURED（配置问题不再伪装成 502）；
  6. 归属者查询抛错不打断整条链。
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
def selector(monkeypatch):
    """替换**共享选择器**（`TokenResolver.resolve_exec_token`）：记录调用、结果可控。

    `stage` 的取值域与 kernel 的 `CREDENTIAL_STAGES` 一致（分类只在那里定义）。
    """
    import src.kernel.token_resolver as tr

    state = {"token": "", "stage": "", "calls": []}

    def _resolve(self, repo_key):
        state["calls"].append(repo_key)
        level = "rc_bot" if (state["token"] or state["stage"]) else ""
        return tr.TokenResult(token=state["token"], level=level,
                              provider=self.provider, repo_key=repo_key,
                              stage=state["stage"])

    monkeypatch.setattr(tr.TokenResolver, "resolve_exec_token", _resolve)
    return state


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


# ── 1. 源码锁：三段判定只在选择器里 ─────────────────────────────────────
def test_source_delegates_to_the_shared_selector():
    """`_bot()` 必须调共享选择器；不得再自己读 rc 行/过期列，也不得开 bypass。"""
    src = inspect.getsource(repos_routes._repo_platform_token)
    assert "resolve_exec_token(" in src, (
        "换券路由必须与建树链共用同一个选择器（Issue #3713 的唯一决定点）")
    assert "get_repo_credential_meta(" not in src, (
        "三段判定已上收到选择器，路由里不得再直读 rc 行（否则又是两套实现）")
    assert "repo_bot_token_status_any(" not in src, (
        "过期判定只保留在选择器（RepoBotToken/ExplicitRepoBotToken._is_expired）")
    assert "bypass_path_b=True" not in src, (
        "不得裸传 bypass：会同时解锁 RepoBot 与 GlobalBot 两级（身份越界）")


# ── 2. 选择器给出 token → 直取 ─────────────────────────────────────────
def test_selector_token_is_returned_without_user_resolution(selector, monkeypatch):
    selector["token"] = "selector-bot-token"
    user_calls: list = []
    _set_user_token(monkeypatch, "user-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "selector-bot-token"
    assert selector["calls"] == ["gitee:o/r"], "选择器必须收到带平台前缀的 repo_key"
    assert user_calls == [], "有可用 bot 时不该再解析用户凭据"


# ── 3. stage → 424，不落全局 bot / 用户身份 ────────────────────────────
@pytest.mark.parametrize("stage,why", [
    ("empty", "值为空或被撤销"),
    ("undecryptable", "密文无法解密"),
    ("expired", "已标记过期"),
])
def test_stage_raises_424_with_the_same_classification(selector, monkeypatch, stage, why):
    selector["stage"] = stage
    user_calls: list = []
    _set_user_token(monkeypatch, "user-pat", user_calls)
    _set_host(monkeypatch, "5", "owner-login")

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "BOT_CREDENTIAL_INVALID"
    assert ei.value.detail["stage"] == stage, (
        "换券面只渲染选择器给的分类——不得自己再判一遍")
    assert why in ei.value.detail["message"]
    assert user_calls == [], "显式 bot 行不可用时不得静默降级到用户身份"


# ── 4. 选择器全空 → 用户档（devops 归属者优先 / chat 会话优先） ─────────
def test_no_explicit_row_devops_prefers_owner(selector, monkeypatch):
    """无显式配置 + devops：先归属者（host_user_id）凭据 —— 路径 B 原始语义。"""
    _set_host(monkeypatch, "5", "owner-login")
    user_calls: list = []
    _set_user_token(monkeypatch, "owner-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert tok == "owner-pat"
    assert user_calls and user_calls[0]["user_id"] == "5"
    assert user_calls[0]["username"] == "owner-login"
    assert user_calls[0]["provider"] == "gitee"


def test_no_explicit_row_chat_prefers_session_user(selector, monkeypatch):
    """无显式配置 + chat：会话用户优先 —— 不能替仓库 owner 代签（否则权限放大）。"""
    _set_host(monkeypatch, "5", "owner-login")
    user_calls: list = []
    _set_user_token(monkeypatch, "session-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(CHAT_USER), "gitee")

    assert tok == "session-pat"
    assert user_calls and user_calls[0]["user_id"] == "42"
    assert user_calls[0]["username"] == "alice"


def test_no_explicit_row_no_owner_falls_back_to_session_user(selector, monkeypatch):
    """无显式配置、无归属者：落当前会话用户。"""
    _set_host(monkeypatch, "")
    user_calls: list = []
    _set_user_token(monkeypatch, "session-pat", user_calls)

    tok = _repo_platform_token("o", "r", dict(SESSION_USER, id="42", username="alice"), "gitee")

    assert tok == "session-pat"
    assert user_calls and user_calls[0]["user_id"] == "42"


# ── 5. 全空 → 424 CREDENTIAL_NOT_CONFIGURED ────────────────────────────
def test_all_empty_raises_424_not_502(selector, monkeypatch):
    """全空 → 424 CREDENTIAL_NOT_CONFIGURED（配置问题不伪装成上游故障）。"""
    _set_host(monkeypatch, "")
    _set_user_token(monkeypatch, "")

    with pytest.raises(HTTPException) as ei:
        _repo_platform_token("o", "r", dict(SESSION_USER), "gitee")

    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "CREDENTIAL_NOT_CONFIGURED"


# ── 6. 归属者查询异常不打断整条链 ──────────────────────────────────────
def test_owner_username_falls_back_to_repo_owner(selector, monkeypatch):
    """host_vcs_username 为空时回落仓库 owner 段（OAuth 档按 username 匹配要它）。"""
    _set_host(monkeypatch, "5", "")
    user_calls: list = []
    _set_user_token(monkeypatch, "owner-pat", user_calls)

    tok = _repo_platform_token("acme", "widget", dict(SESSION_USER), "gitee")

    assert tok == "owner-pat"
    assert user_calls and user_calls[0]["username"] == "acme"


def test_host_owner_lookup_failure_does_not_break_fallback(selector, monkeypatch):
    """归属者查询抛错时继续落会话用户，不打断整条链。"""
    import src.domains.mgmt.user.db as udb

    def _boom(repo_key):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(udb, "get_host_user_id", _boom)
    _set_user_token(monkeypatch, "session-pat")

    tok = _repo_platform_token("o", "r", dict(SESSION_USER, id="7", username="bob"), "gitee")
    assert tok == "session-pat"


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