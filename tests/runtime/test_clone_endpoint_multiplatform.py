"""clone 端点单点派生（多平台兼容）验收用例。

覆盖设计稿 §8 的 A~E：
  A 地址派生（四平台 + 无前缀默认平台）
  B 用户名按平台卡 clone_auth 派生（不再常量）
  C 无卡 fail-fast（不静默回退 gitcode.com；repogit 侧异常类型保持不变）
  D 宿主一致性（六入口同源：可执行断言 + 源码断言）
  E 凭据不回显（clone_params 结构里没有 token）

背景：#3632 修好了"引擎取平台卡"，但 clone 地址在沙箱 executor / sandbox_context /
takeover 三处仍写死 https://gitcode.com/...，用户名在 CLI 券与沙箱 askpass 里也有两套
常量（x-access-token / bot），与 gitee 卡声明的 clone_auth=basic_password 不符。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.domains.runtime.vcs import clone_endpoint as ce  # noqa: E402

_CARDS = {
    # host / clone_auth / bot_username —— 与 configs/platforms/*.json 一致
    "gitcode": {"host": "gitcode.com", "clone_auth": "x_access_token", "bot_username": ""},
    "github": {"host": "github.com", "clone_auth": "x_access_token", "bot_username": ""},
    "gitlab": {"host": "gitlab.com", "clone_auth": "oauth2", "bot_username": ""},
    "gitee": {"host": "gitee.com", "clone_auth": "basic_password", "bot_username": ""},
}


def _fake_card(provider: str, bot_username: str = ""):
    cfg = dict(_CARDS[provider])
    if bot_username:
        cfg["bot_username"] = bot_username
    return types.SimpleNamespace(**cfg)


@pytest.fixture()
def cards(monkeypatch):
    """把平台卡替换成内存卡（本机无 DB，不连表）。"""
    def _get(provider: str):
        if provider not in _CARDS:
            return None
        return _fake_card(provider)

    import src.domains.runtime.vcs.platform_card as pc

    monkeypatch.setattr(pc, "get_platform_card", _get)
    monkeypatch.setattr(ce, "_warned_providers", set())
    return _get


# ---------------------------------------------------------------- A 地址派生
@pytest.mark.parametrize("repo_key,expected", [
    ("gitcode:foo/bar", "https://gitcode.com/foo/bar.git"),
    ("github:foo/bar", "https://github.com/foo/bar.git"),
    ("gitee:foo/bar", "https://gitee.com/foo/bar.git"),
    ("gitlab:foo/bar", "https://gitlab.com/foo/bar.git"),
    ("foo/bar", "https://gitcode.com/foo/bar.git"),  # 无前缀 = 默认平台（既有语义）
])
def test_clone_url_per_platform(cards, repo_key, expected):
    assert ce.clone_url(repo_key) == expected


# ---------------------------------------------------------------- B 用户名派生
def test_clone_username_x_access_token(cards):
    assert ce.clone_username("gitcode", "gitcode:foo/bar") == "x-access-token"
    assert ce.clone_username("github", "github:foo/bar") == "x-access-token"


def test_clone_username_oauth2(cards):
    assert ce.clone_username("gitlab", "gitlab:foo/bar") == "oauth2"


def test_clone_username_basic_password_falls_back_to_owner(cards):
    """gitee：卡 bot_username 为空 → 仓库 owner（不再给 x-access-token / 空串）。"""
    assert ce.clone_username("gitee", "gitee:alice/repo") == "alice"


def test_clone_username_basic_password_prefers_card(cards):
    """gitee：卡声明了 bot_username → 用卡值（部署里令牌归属账号可配）。"""
    cards_gitee = lambda p: _fake_card(p, bot_username="gitee-bot") if p == "gitee" else cards(p)
    import src.domains.runtime.vcs.platform_card as pc

    orig = pc.get_platform_card
    pc.get_platform_card = cards_gitee
    try:
        assert ce.clone_username("gitee", "gitee:alice/repo") == "gitee-bot"
    finally:
        pc.get_platform_card = orig


def test_basic_password_never_returns_empty(cards):
    """空用户名会让 Basic 头变成 ":token"，上游只能给 401 —— 至少回退到 owner。"""
    assert ce.clone_username("gitee", "gitee:alice/repo") != ""


# ---------------------------------------------------------------- C fail-fast
def test_unknown_platform_raises_clone_endpoint_error(cards):
    with pytest.raises(ce.CloneEndpointError) as ei:
        ce.clone_url("unknownhost:foo/bar")
    assert ei.value.code == "platform_not_registered"


def test_repogit_keeps_platform_card_missing_error(cards):
    """agent 侧按 PlatformCardMissingError 显式阻断任务；异常类型变了会退回"warning 就放行"。"""
    from src.domains.runtime.engine import repogit
    from src.domains.runtime.vcs.platform_card import PlatformCardMissingError

    with pytest.raises(PlatformCardMissingError):
        repogit.clone_url_for("unknownhost:foo/bar")
    assert repogit.clone_url_for("gitee:foo/bar") == "https://gitee.com/foo/bar.git"


# ---------------------------------------------------------------- D 宿主一致性
def test_clone_params_shape_and_no_token(cards):
    params = ce.clone_params("gitee:foo/bar")
    assert set(params) == {"provider", "owner", "repo", "clone_url", "clone_auth", "username"}
    assert not any(k in params for k in ("token", "password", "secret"))


def test_git_credential_uses_same_derivation(cards):
    """CLI 一次性券的 username / clone_url 与 clone_params 同源（过去 username 恒 x-access-token）。"""
    from src.domains.mgmt.routes import repos as mrepos

    orig = mrepos._repo_platform_token
    mrepos._repo_platform_token = lambda owner, repo, user, p: "fake-token"
    try:
        cred = mrepos.get_repo_git_credential("foo", "bar", user={"id": 0}, p="gitee")
    finally:
        mrepos._repo_platform_token = orig
    assert cred["clone_url"] == ce.clone_url("gitee:foo/bar")
    assert cred["username"] == ce.clone_username("gitee", "gitee:foo/bar")
    assert cred["username"] != "x-access-token"


@pytest.mark.parametrize("rel", [
    "src/domains/runtime/agents/executor.py",
    "src/domains/runtime/agents/sandbox/sandbox_context.py",
    "src/domains/runtime/engine/takeover.py",
])
def test_no_hardcoded_gitcode_clone_url(rel):
    """三处曾经的硬编码必须消失，且地址改由 clone_endpoint 派生（源码断言）。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "https://gitcode.com/{_repo_name}" not in src
    assert "https://gitcode.com/{repo_short}" not in src
    assert "clone_endpoint" in src, f"{rel} 未接入 clone_endpoint 派生"


def test_sandbox_askpass_username_is_not_hardcoded_bot():
    """沙箱 askpass 的用户名必须来自环境变量（gitee 需要真实账号名）。"""
    src = (ROOT / "src/domains/runtime/agents/sandbox/backends/devbox.py").read_text(encoding="utf-8")
    assert 'Username*) echo "bot"' not in src
    assert "$GIT_BOT_USERNAME" in src
    assert "GIT_BOT_USERNAME" in src

# ---------------------------------------------------------------- F 沙箱 token 键派生
def test_bot_token_env_name_per_platform(cards):
    """bot token 的环境变量名按平台派生（沙箱过去只认 BOT_GITCODE_TOKEN）。"""
    assert ce.bot_token_env_name("gitcode:foo/bar") == "BOT_GITCODE_TOKEN"
    assert ce.bot_token_env_name("gitee:foo/bar") == "BOT_GITEE_TOKEN"
    assert ce.bot_token_env_name("github:foo/bar") == "BOT_GITHUB_TOKEN"
    assert ce.bot_token_env_name("gitlab:foo/bar") == "BOT_GITLAB_TOKEN"


def test_bot_token_from_env_prefers_derived_key_and_keeps_backcompat(cards):
    env = {"BOT_GITEE_TOKEN": "gitee-token", "BOT_GITCODE_TOKEN": "gitcode-token"}
    assert ce.bot_token_from_env(env, "gitee:foo/bar") == "gitee-token"
    # 升级期：旧派发载荷只写 gitcode 键 → 仍能取到，不断链
    assert ce.bot_token_from_env({"BOT_GITCODE_TOKEN": "gc"}, "gitee:foo/bar") == "gc"
    assert ce.bot_token_from_env({}, "gitee:foo/bar") == ""

def test_repogit_basic_auth_never_emits_empty_username(cards):
    """真机回归：basic_password 的用户名派生必须带 repo_key，否则退化成空用户名。

    gitee 私有仓实测（2026-11）：x-access-token / 空用户名均返回
    "The token username invalid"，只有"令牌所属账号名"能过。因此 repogit 生成的
    Basic 头里用户名段必须非空（缺 repo_key 会退化成 ""）。
    """
    import base64 as _b64

    from src.domains.runtime.engine import repogit

    hdr = repogit._basic_auth("tok", "basic_password", "gitee", repo_key="gitee:alice/repo")
    user = _b64.b64decode(hdr).decode().split(":", 1)[0]
    assert user == "alice", "basic_password 必须落 owner 兜底，不能是空串"

    hdr2 = repogit._basic_auth("tok", "basic_password", "gitee", username="real-login")
    assert _b64.b64decode(hdr2).decode().split(":", 1)[0] == "real-login", \
        "调用方已派生好的用户名必须优先（组织仓靠卡 bot_username）"


def test_clone_username_source_is_observable():
    """P2：用户名来源必须可区分（卡上配置 vs 回落的 owner vs 常量）。

    basic_password 平台只接受**令牌所属账号名**：owner_fallback 只在「个人仓且
    owner 恰等于令牌账号」时正确，组织仓/他人仓会被上游拒。调用方需要这个信号，
    否则排障会去查凭据而不会怀疑用户名派生。
    """
    from src.domains.runtime.vcs import clone_endpoint as ce

    # 卡上有 bot_username → card_bot_username（本机 gitee 的实际情况）
    card = type("C", (), {"host": "gitee.com", "clone_auth": "basic_password",
                          "bot_username": "yang-aiwu-new", "token_env": ""})()
    orig = ce._card
    ce._card = lambda p: card
    try:
        user, src = ce.clone_username_source("gitee", "gitee:some-org/repo")
        assert user == "yang-aiwu-new" and src == "card_bot_username"

        # 卡上无 bot_username → owner_fallback（可观测，不静默）
        card.bot_username = ""
        user, src = ce.clone_username_source("gitee", "gitee:some-org/repo")
        assert user == "some-org" and src == "owner_fallback"

        # 卡上无值 + repo_key 缺失 → missing（不返回猜测值）
        user, src = ce.clone_username_source("gitee", "")
        assert user == "" and src == "missing"

        # 非 basic_password → 常量
        card.clone_auth = "x_access_token"
        user, src = ce.clone_username_source("gitee", "gitee:o/r")
        assert user == "x-access-token" and src == "scheme_const"

        # 兼容：clone_username 仍返回字符串
        assert ce.clone_username("gitee", "gitee:o/r") == "x-access-token"
    finally:
        ce._card = orig
        ce._warned_providers.discard("gitee")
