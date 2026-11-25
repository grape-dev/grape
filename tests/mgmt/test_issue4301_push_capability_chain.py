# -*- coding: utf-8 -*-
"""Issue #4301, second batch (owner decision 2026-11-25): workflow precheck/diagnosis extended to
the bot channels and the login channel.

Coverage (one block per implementation point):
  1. ``push_workflows_capability`` 按**真实推送链档位**判定（rc_bot/repo_bot →
     全局 bot → PAT → 登录渠道 scope），未知态一律 None（不阻断）；
  2. ``workflows_capability_blocker`` 的阻断文案随档位变化（同一故障不同补救动作）；
  3. 仓库 Bot 的**设置页快路径**（PUT /settings/gitcode-repo）与
     /repos/bot-token 同口径：已知能力落 rc 单表、未知不落键、响应带 scope_warnings；
  4. 平台（全局）Bot 写入口：能力落 meta ``global_bot_<p>_workflows_write``；
  5. ``config_store.verify_token_scopes`` 复用同一次 /user 响应头（无第二次网络往返）。
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import uuid

import pytest

NS = "i4301b"


def _rk(provider: str = "github") -> str:
    return f"{provider}:{NS}/{uuid.uuid4().hex[:8]}"


def _clean(rk: str):
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        c.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?", (f"{NS}%",))
        db.commit()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?", (f"{NS}%",))
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE ?", (f"github:{NS}/%",))
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE ?", (f"gitcode:{NS}/%",))
        c.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE ?", (f"github:{NS}/%",))
        c.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE ?", (f"gitcode:{NS}/%",))
        db.commit()


def _seed_repo(rk: str, host_user_id: str = "1"):
    """Seed by the explicit repo_key (provider prefix included); resolve_managed_repo_key
    反推，它对不存在的仓库恒回落 gitcode 前缀，会把 github 用例悄悄变成 gitcode。"""
    from src.domains.mgmt.user.db import add_managed_repo

    owner, name = rk.split(":", 1)[1].split("/")
    assert add_managed_repo(rk, owner, name, host_user_id=host_user_id, webhook_id=0)


class _Level:
    """Stand-in for resolve_exec_token (level only)."""

    def __init__(self, level):
        self.level = level


@pytest.fixture
def set_level(monkeypatch):
    import src.kernel.token_resolver as tr

    def _set(level):
        monkeypatch.setattr(tr, "resolve_exec_token", lambda rk: _Level(level))

    return _set


# ── 1. capability verdict per chain level ────────────────────────────

def test_capability_follows_repo_bot_level(set_level):
    """Repo bot level: capability comes from the rc table cred_type='workflows_write'."""
    import src.domains.mgmt.user.repo_manager as rm
    from src.domains.insight.public import upsert_repo_credential_value as up

    rk = _rk()
    _seed_repo(rk)
    up(rk, "workflows_write", "false", "2026-11-25 00:00:00")
    set_level("repo_bot")
    got = rm.push_workflows_capability(rk)
    _clean(rk)
    assert got == {"capability": False, "source": "repo_bot"}, got


def test_capability_follows_global_bot_level(set_level):
    """Global bot level: capability comes from meta global_bot_<p>_workflows_write."""
    import src.domains.mgmt.user.repo_manager as rm
    from src.domains.mgmt.user.db import set_meta_value

    rk = _rk()
    set_meta_value("global_bot_github_workflows_write", "false")
    set_level("global_bot")
    got = rm.push_workflows_capability(rk)
    set_meta_value("global_bot_github_workflows_write", "")
    assert got == {"capability": False, "source": "global_bot"}, got


def test_capability_user_pat_preferred_over_login_channel(set_level):
    """User level: an explicit PAT declaration wins; the login channel scope must not override it."""
    import src.domains.mgmt.user.repo_manager as rm
    from src.domains.mgmt.user.db import set_credential

    rk = _rk()
    _seed_repo(rk, host_user_id=f"{NS}u1")
    set_credential(f"{NS}u1", "github", {"token": "t", "workflows_write": True})
    set_level("user")
    got = rm.push_workflows_capability(rk)
    _clean(rk)
    assert got == {"capability": True, "source": "pat"}, got


def test_capability_login_channel_without_workflow_is_false(set_level, monkeypatch):
    """User level, no PAT declaration: channel scope without workflow -> definite False."""
    import src.domains.mgmt.user.repo_manager as rm

    rk = _rk()
    _seed_repo(rk)
    monkeypatch.setattr(rm, "_login_channel_scope", lambda p: "read:user user:email repo")
    set_level("user")
    got = rm.push_workflows_capability(rk)
    _clean(rk)
    assert got == {"capability": False, "source": "login_channel"}, got


def test_capability_non_github_channel_scope_never_blocks(set_level, monkeypatch):
    """Non-GitHub platforms: coarse channel scopes ("all"/"user_info") are not OAuth
    scope lists, so a missing "workflow" token must stay unknown, never False.

    Live probe caught this: 89 gitcode/gitee repos were reported as workflow-blocked."""
    import src.domains.mgmt.user.repo_manager as rm

    rk = f"gitcode:{NS}/ns-probe"
    _seed_repo(rk)
    monkeypatch.setattr(rm, "_login_channel_scope", lambda p: "all")
    set_level("user")
    got = rm.push_workflows_capability(rk)
    blocker = rm.workflows_capability_blocker(f"{NS}u1", "gitcode", True, rk)
    _clean(rk)
    assert got.get("capability") is None, got
    assert blocker == "", blocker


def test_capability_login_channel_with_workflow_stays_unknown(set_level, monkeypatch):
    """Channel scope has workflow -> still None (old tokens may predate the change; no false positive)."""
    import src.domains.mgmt.user.repo_manager as rm

    rk = _rk()
    _seed_repo(rk)
    monkeypatch.setattr(rm, "_login_channel_scope",
                        lambda p: "read:user user:email repo workflow admin:repo_hook")
    set_level("user")
    got = rm.push_workflows_capability(rk)
    _clean(rk)
    assert got == {"capability": None, "source": "login_channel"}, got


# ── 2. blocker wording per level ─────────────────────────────────────

@pytest.mark.parametrize("source,marker", [
    ("repo_bot", "仓库级 Bot"),
    ("global_bot", "平台全局 Bot"),
    ("login_channel", "登录渠道"),
    ("pat", "GitHub PAT"),
])
def test_blocker_wording_matches_source(source, marker):
    import src.domains.mgmt.user.repo_manager as rm

    text = rm._WORKFLOWS_BLOCKER_TEXTS[source]
    assert marker in text, f"{source} 文案未点明档位: {text}"


def test_blocker_uses_chain_when_repo_key_given(monkeypatch):
    """With repo_key the verdict follows the chain level (repo bot lacking permission here)."""
    import src.domains.mgmt.user.repo_manager as rm

    monkeypatch.setattr(rm, "push_workflows_capability",
                        lambda rk: {"capability": False, "source": "repo_bot"})
    # User PAT is explicitly capable: judging by PAT alone would miss it -> this pins the chain walk
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: True)
    text = rm.workflows_capability_blocker(f"{NS}u1", "github", True, "github:o/r")
    assert "仓库级 Bot" in text, f"未按链判定: {text!r}"


def test_blocker_empty_when_task_unrelated_or_unknown(monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm

    monkeypatch.setattr(rm, "push_workflows_capability",
                        lambda rk: {"capability": None, "source": "login_channel"})
    assert rm.workflows_capability_blocker(f"{NS}u1", "github", True, "github:o/r") == ""
    assert rm.workflows_capability_blocker(f"{NS}u1", "github", False, "github:o/r") == ""


# ── 3. repo bot settings fast path: persist + warn (same as /repos/bot-token) ──

class _FakeProvider:
    """Mirrors the baseline provider API: verify_token_with_headers -> (info, headers)."""

    def __init__(self, scopes):
        self._scopes = scopes
        self.calls = 0

    def verify_token(self, tok):
        self.calls += 1
        return {"id": "1", "login": "botlogin", "name": "Bot"}

    def verify_token_with_headers(self, tok):
        info = self.verify_token(tok)
        headers = {} if self._scopes is None else {"X-OAuth-Scopes": self._scopes}
        return info, headers


def _put_repo_bot(monkeypatch, scopes):
    """Call the real route function (no HTTP): the bot_token branch of PUT /settings/gitcode-repo."""
    from src.domains.mgmt.routes import settings as st

    holder = {"p": _FakeProvider(scopes)}
    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider_from_repo_key",
                        lambda rk: holder["p"])
    body = st.GitCodeRepoConfig(bot_token="tok_bot_1")
    return st.update_gitcode_repo_config(NS, "reponame",
                                        body, user={"id": 1, "role": "admin", "username": "u"})


def test_repo_bot_without_workflow_persists_false_and_warns(monkeypatch):
    from src.domains.insight.public import repo_credential_value

    rk = f"github:{NS}/reponame"
    _seed_repo(rk, host_user_id="")
    try:
        resp = _put_repo_bot(monkeypatch, "repo, admin:repo_hook")
        assert repo_credential_value(rk, "workflows_write") == "false", \
            "a repo bot without workflow must persist the rc row (the diagnosis needs it)"
        warns = resp.get("scope_warnings") or []
        assert any("workflow" in w for w in warns), f"响应缺告警: {resp}"
    finally:
        _clean(rk)


def test_repo_bot_with_workflow_persists_true_no_warning(monkeypatch):
    from src.domains.insight.public import repo_credential_value

    rk = f"github:{NS}/reponame2"
    _seed_repo(rk, host_user_id="")
    try:
        resp = _put_repo_bot_with(monkeypatch, "repo, workflow", "reponame2")
        assert repo_credential_value(rk, "workflows_write") == "true"
        assert not (resp.get("scope_warnings") or []), resp
    finally:
        _clean(rk)


def _put_repo_bot_with(monkeypatch, scopes, repo_name):
    from src.domains.mgmt.routes import settings as st

    holder = {"p": _FakeProvider(scopes)}
    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider_from_repo_key",
                        lambda rk: holder["p"])
    body = st.GitCodeRepoConfig(bot_token="tok_bot_2")
    return st.update_gitcode_repo_config(NS, repo_name, body,
                                        user={"id": 1, "role": "admin", "username": "u"})


def test_repo_bot_finegrained_unknown_no_key_but_warns(monkeypatch):
    """No scope header (fine-grained PAT) -> no rc key, but the checklist warning is returned."""
    from src.domains.insight.public import repo_credential_value

    rk = f"github:{NS}/reponame3"
    _seed_repo(rk, host_user_id="")
    try:
        resp = _put_repo_bot_with(monkeypatch, None, "reponame3")
        assert repo_credential_value(rk, "workflows_write") == "", "未知态不得落键"
        warns = resp.get("scope_warnings") or []
        assert any("未" in w or "无法" in w for w in warns), f"缺未知态提示: {resp}"
    finally:
        _clean(rk)


# ── 4. platform bot: capability persists in meta ─────────────────────

def test_platform_bot_persists_capability_meta(monkeypatch):
    from src.domains.mgmt.user.db import get_meta_value
    from src.domains.mgmt.admin import repo_platform_routes as rpr

    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo as R
    from src.domains.mgmt.admin import config_store as cs

    monkeypatch.setattr(cs, "verify_token_with_scopes",
                        lambda p, t: ({"login": "botx"},
                                      {"repo", "admin:repo_hook"}))
    monkeypatch.setattr(cs, "platform_bot_old_token", lambda p: "")
    monkeypatch.setattr(cs, "trigger_platform_bot_swap", lambda *a, **k: None)
    monkeypatch.setattr(R, "set_bot", staticmethod(lambda s, provider, username=None, token_enc=None: True))
    monkeypatch.setattr(R, "mark_bot_status", staticmethod(lambda s, provider, status: True))
    monkeypatch.setattr(rpr, "uow_write", lambda fn: fn(None))
    set_meta_value_safe = __import__("src.domains.mgmt.user.db", fromlist=["set_meta_value"])
    set_meta_value_safe.set_meta_value("global_bot_github_workflows_write", "")

    resp = rpr.set_platform_bot("github", rpr.BotBody(token="tok_bot_plat"), admin={"id": 1})
    try:
        assert get_meta_value("global_bot_github_workflows_write") == "false", resp
        assert any("workflow" in w for w in (resp.get("scope_warnings") or [])), resp
    finally:
        set_meta_value_safe.set_meta_value("global_bot_github_workflows_write", "")


# ── 5. verify_token_with_scopes reuses the single /user reply ────────

def test_config_store_verify_token_with_scopes_single_roundtrip(monkeypatch):
    from src.domains.mgmt.admin import config_store as cs

    holder = {"p": _FakeProvider("repo, workflow")}
    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda p: holder["p"])
    info, scopes = cs.verify_token_with_scopes("github", "tok")
    assert scopes == {"repo", "workflow"} and info["login"] == "botlogin"
    assert holder["p"].calls == 1, "probe must reuse the single verify round-trip"