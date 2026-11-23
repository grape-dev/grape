"""Issue #21 第 2 步：派发身份（领域模型 + ORM + 缓存 + 开关）场景用例 S1–S18。

契约：
  · 判定顺序 task.user_id → account → actor → 空；
  · GRAPE_DISPATCH_USER_ID 默认 shadow：解析出 id 但 applies=False（不写库/不取令牌），只记日志；
  · on 才生效；非法值回 shadow；off 完全不查库；
  · 账号读取走 ORM（AccountRepository），同账号短 TTL 内只查一次；
  · 认人失败（库不可达/字段脏）不得阻断派发与取令牌；
  · 平台边界：只有 github/gitcode 有「按 id 取个人令牌」能力，其它平台不猜、回落原链。
"""
from __future__ import annotations

import os

import pytest

from src.domains.mgmt.auth.dispatch_identity import (
    DispatchMode, IdentitySource, current_mode, dispatch_identity, resolve_identity,
    user_id_is_valid)
from src.domains.mgmt.auth.dispatch_identity import service as svc
from src.domains.mgmt import public as mgmt_public
from src.domains.runtime.pipeline import event_pipeline
from src.domains.runtime.pipeline import dispatch_identity as rt_adapter

UID = "629a7971-8ce8-4508-97be-f9f4e3731c6f"


class _FakeLog:
    def __init__(self):
        self.infos: list[str] = []

    def info(self, msg, *a):
        self.infos.append(msg % a if a else msg)

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Account:
    def __init__(self, uid):
        self.id = uid


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """每个用例：清缓存 + 清环境变量 + 隔离日志。"""
    svc._cache.clear()
    monkeypatch.delenv("GRAPE_DISPATCH_USER_ID", raising=False)
    monkeypatch.setattr(svc, "log", _FakeLog())
    yield
    svc._cache.clear()
    os.environ.pop("GRAPE_DISPATCH_USER_ID", None)


def _patch_accounts(monkeypatch, table: dict) -> dict:
    """把 ORM 账号读取换成内存表，并记录查询次数。"""
    calls = {"n": 0, "names": []}

    def _get_by_username(self, session, username):
        calls["n"] += 1
        calls["names"].append(username)
        uid = table.get(str(username or "").strip().lower())
        return _Account(uid) if uid else None

    from src.kernel.persistence.repositories import AccountRepository
    monkeypatch.setattr(AccountRepository, "get_by_username", _get_by_username)
    return calls


# ── S0：默认值 ─────────────────────────────────────────────────────────────
def test_s0_unset_defaults_to_on_and_applies(monkeypatch):
    """未设置开关 = ON：按 id 认人必须默认生效，否则 #21 依旧推不了分支。"""
    _patch_accounts(monkeypatch, {"alice": UID})
    ident = dispatch_identity(account_name="alice")
    assert ident.mode is DispatchMode.ON and ident.applies and ident.user_id == UID
    assert svc.log.infos and "bound user_id=" in svc.log.infos[0]


# ── S1–S4：开关语义 ────────────────────────────────────────────────────────
def test_s1_off_returns_no_candidate_and_never_queries(monkeypatch):
    calls = _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "off")
    ident = dispatch_identity(account_name="alice")
    assert ident.mode is DispatchMode.OFF and ident.user_id == "" and not ident.applies
    assert calls["n"] == 0, "off 不得查库"


def test_s2_shadow_resolves_but_does_not_apply(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "shadow")
    ident = dispatch_identity(account_name="alice")
    assert ident.mode is DispatchMode.SHADOW
    assert ident.user_id == UID and ident.source is IdentitySource.ACCOUNT
    assert ident.applies is False, "shadow 不得生效"
    assert svc.log.infos and "would bind user_id=" in svc.log.infos[0]


def test_s3_on_applies(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    ident = dispatch_identity(account_name="alice")
    assert ident.applies and ident.user_id == UID


def test_s3b_invalid_mode_falls_back_to_shadow(monkeypatch):
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "please-enable")
    assert current_mode() is DispatchMode.SHADOW


# ── S4–S7：解析顺序与哨兵 ──────────────────────────────────────────────────
def test_s4_task_user_id_wins(monkeypatch):
    calls = _patch_accounts(monkeypatch, {"alice": UID})
    ident = resolve_identity(task_user_id="t-42", account_name="alice")
    assert (ident.user_id, ident.source) == ("t-42", IdentitySource.TASK)
    assert calls["n"] == 0


def test_s5_invalid_task_id_falls_to_account_then_actor(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID, "bob": "bob-id"})
    assert resolve_identity(task_user_id=0, account_name="alice").source is IdentitySource.ACCOUNT
    ident = resolve_identity(task_user_id="0", account_name="", actor_name="bob")
    assert (ident.user_id, ident.source) == ("bob-id", IdentitySource.ACTOR)


def test_s6_nothing_resolvable(monkeypatch):
    _patch_accounts(monkeypatch, {})
    ident = resolve_identity(account_name="ghost")
    assert ident.user_id == "" and ident.source is IdentitySource.NONE


def test_s6b_sentinels_are_invalid():
    for bad in ("", "0", None, "None", "null", "undefined", "  "):
        assert not user_id_is_valid(bad)
    assert user_id_is_valid("abc-123")


# ── S7–S9：派发侧接线（runtime 适配器） ────────────────────────────────────
def test_s7_adapter_on_returns_writable_id(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    assert rt_adapter.identity_for_actor("alice", "") == (UID, "account")


def test_s8_adapter_shadow_keeps_row_zero(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "shadow")
    uid, source = rt_adapter.identity_for_actor("alice", "")
    assert uid == "" and source == "account"


def test_s8b_adapter_default_on_returns_id(monkeypatch):
    """默认 ON：派发侧直接拿到可写 id（#21 的真实生效路径）。"""
    _patch_accounts(monkeypatch, {"alice": UID})
    assert rt_adapter.identity_for_actor("alice", "") == (UID, "account")


def test_s9_event_pipeline_delegates(monkeypatch):
    monkeypatch.setattr(event_pipeline, "resolve_actor_account", lambda evt, rk="": "alice")
    monkeypatch.setattr(rt_adapter, "identity_for_actor", lambda a, b: (UID, "account"))
    assert event_pipeline._dispatch_ident({"type": "a2a_event"}, "github:o/r") == (UID, "account", "alice")
    assert event_pipeline._dispatch_ident("not-a-dict", "github:o/r") == ("", "", "")


def test_s9b_adapter_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("contract down")

    monkeypatch.setattr(mgmt_public, "dispatch_identity", _boom)
    assert rt_adapter.identity_for_actor("alice", "") == ("", "")


# ── S10–S12：取令牌侧 ──────────────────────────────────────────────────────
@pytest.fixture()
def token_wiring(monkeypatch):
    import src.domains.mgmt.user.db as udb

    seen = {"by_id": [], "repo": 0}

    def _user_by_id(uid):
        seen["by_id"].append(uid)
        return "user-token" if str(uid) == UID else ""

    def _repo_chain(repo_key):
        seen["repo"] += 1
        return "repo-bot-token"

    monkeypatch.setattr(udb, "get_user_github_token_by_id", _user_by_id)
    monkeypatch.setattr(udb, "get_user_gitcode_token", lambda uid: "gitcode-user-token")
    monkeypatch.setattr(udb, "get_github_token", lambda repo_key="": _repo_chain(repo_key))
    monkeypatch.setattr(udb, "get_repo_token_with_refresh", lambda repo_key: _repo_chain(repo_key))
    return seen


def test_s10_shadow_does_not_use_user_token(monkeypatch, token_wiring):
    import src.domains.mgmt.user.db as udb
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "shadow")
    tok = udb.get_task_token({"repo_key": "github:o/r", "user_id": UID})
    assert tok == "repo-bot-token" and token_wiring["by_id"] == []


def test_s10b_default_on_uses_user_token(monkeypatch, token_wiring):
    """默认 ON：get_task_token 直接走个人令牌（不用再配环境变量）。"""
    import src.domains.mgmt.user.db as udb
    tok = udb.get_task_token({"repo_key": "github:o/r", "user_id": UID})
    assert tok == "user-token" and token_wiring["by_id"] == [UID]


def test_s11_on_uses_user_token_first(monkeypatch, token_wiring):
    import src.domains.mgmt.user.db as udb
    _patch_accounts(monkeypatch, {})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    tok = udb.get_task_token({"repo_key": "github:o/r", "user_id": UID})
    assert tok == "user-token" and token_wiring["by_id"] == [UID]


def test_s12_on_without_user_token_falls_back(monkeypatch, token_wiring):
    import src.domains.mgmt.user.db as udb
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    tok = udb.get_task_token({"repo_key": "github:o/r", "user_id": "nobody"})
    assert tok == "repo-bot-token" and token_wiring["repo"] == 1


def test_s12b_platform_without_by_id_getter_falls_back(monkeypatch, token_wiring):
    import src.domains.mgmt.user.db as udb
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    tok = udb.get_task_token({"repo_key": "gitee:o/r", "user_id": UID})
    assert tok == "repo-bot-token" and token_wiring["by_id"] == []


# ── S13–S15：性能与契约面 ─────────────────────────────────────────────────
def test_s13_account_lookup_is_cached(monkeypatch):
    calls = _patch_accounts(monkeypatch, {"alice": UID})
    assert resolve_identity(account_name="alice").user_id == UID
    assert resolve_identity(account_name="Alice").user_id == UID
    assert calls["n"] == 1, "同一账号在 TTL 内只查一次库"


def test_s13b_cache_ttl_zero_disables_cache(monkeypatch):
    calls = _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_IDENTITY_CACHE_TTL", "0")
    resolve_identity(account_name="alice")
    resolve_identity(account_name="alice")
    assert calls["n"] == 2


def test_s14_lookup_failure_degrades_to_empty(monkeypatch):
    from src.kernel.persistence.repositories import AccountRepository

    def _boom(self, session, username):
        raise RuntimeError("db down")

    monkeypatch.setattr(AccountRepository, "get_by_username", _boom)
    ident = resolve_identity(account_name="alice")
    assert ident.user_id == "" and ident.source is IdentitySource.NONE


def test_s15_public_contract_exposes_value_object(monkeypatch):
    _patch_accounts(monkeypatch, {"alice": UID})
    monkeypatch.setenv("GRAPE_DISPATCH_USER_ID", "on")
    assert mgmt_public.dispatch_user_id_mode() == "on"
    ident = mgmt_public.dispatch_identity(account_name="alice")
    assert ident.applies and ident.user_id == UID
    assert mgmt_public.resolve_actor_user_id(task_user_id="t-1").source is IdentitySource.TASK