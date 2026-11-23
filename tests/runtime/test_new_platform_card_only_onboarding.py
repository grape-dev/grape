"""新增平台「纯配卡接入」验收：只加一张 configs/platforms/<p>.json，clone 链路即可用。

这是「多平台可扩展」的判据：
  · clone 三要素（host / clone_auth / username）全部来自卡，代码里不需要任何平台分支；
  · token env 名由卡的 token_env 派生（缺省 BOT_<P>_TOKEN）；
  · 没有 provider 模块的新平台，clone 链路照样成立；API 翻译面落「方言族代理」
    （FamilyProviderModule：属性委托同族模块，API_BASE 取卡上 host）。

本用例不写任何 Python 平台分支，只用临时目录里的一张卡（示例平台 gitea）。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

GITEA_CARD = {
    "name": "Gitea",
    "dialect": "github",
    "host": "gitea.example.com",
    "api_base": "https://gitea.example.com/api/v1",
    "api_auth": "token_header",
    "webhook_path": "/webhook/gitea",
    "webhook_verify": "hmac",
    "webhook_dialect": "github_web",
    "clone_auth": "basic_password",
    "bot_username": "gitea-bot",
    "token_env": "BOT_GITEA_TOKEN",
    "issue_write_form": "repo_path",
    "branch_create_form": "form",
    "pr_head_filter": "owner_ref",
}


@pytest.fixture()
def new_platform(monkeypatch, tmp_path):
    """往临时 configs 目录只放一张卡，不碰任何代码。"""
    d = tmp_path / "platforms"
    d.mkdir()
    (d / "gitea.json").write_text(json.dumps(GITEA_CARD), encoding="utf-8")

    from src.domains.runtime.vcs import platform_card as pc

    cards = pc.builtin_cards_from_dir(str(d))
    assert "gitea" in cards, "卡加载失败"

    def _get(provider):
        return cards.get(provider)

    monkeypatch.setattr(pc, "get_platform_card", _get)
    pc.invalidate_platform_card_cache() if hasattr(pc, "invalidate_platform_card_cache") else None
    from src.domains.runtime.vcs import clone_endpoint as ce

    monkeypatch.setattr(ce, "_warned_providers", set())
    return ce, cards


def test_clone_url_comes_from_the_card(new_platform):
    ce, _cards = new_platform
    assert ce.clone_url("gitea:acme/widget") == "https://gitea.example.com/acme/widget.git"


def test_clone_auth_comes_from_the_card(new_platform):
    ce, _cards = new_platform
    assert ce.clone_auth("gitea") == "basic_password"


def test_clone_username_prefers_card_bot_username(new_platform):
    """basic_password 形态下，用户名取卡 bot_username（不是 owner、不是占位常量）。"""
    ce, _cards = new_platform
    assert ce.clone_username("gitea", "gitea:acme/widget") == "gitea-bot"


def test_token_env_name_comes_from_the_card(new_platform):
    ce, _cards = new_platform
    assert ce.bot_token_env_name("gitea:acme/widget") == "BOT_GITEA_TOKEN"
    assert ce.bot_token_from_env({"BOT_GITEA_TOKEN": "t0ken"}, "gitea:acme/widget") == "t0ken"


def test_clone_params_chain_needs_no_python_module(new_platform):
    """纯配卡平台不写任何 Python 模块也能拿到消费面：方言族代理，API_BASE 取卡。

    Issue #3580 复审后口径变了（自建实例仓不再 502）：无代码模块、无 resources
    段的卡不再落 None，而是按 `card.dialect` 套同族代码模块的 FamilyProviderModule
    代理。本用例守的是「零新增平台字面量」——代理的 API_BASE 必须来自卡上的 host，
    不能回落内置默认域名（否则凭据会打到 gitlab.com 一类公网实例）。
    """
    ce, _cards = new_platform
    from src.domains.runtime.vcs.providers import module_for
    from src.domains.runtime.vcs.providers.family_module import FamilyProviderModule

    mod = module_for("gitea")
    assert mod is not None, "纯配卡平台必须落方言族代理，不能返回 None（否则实例仓全量 502）"
    assert isinstance(mod, FamilyProviderModule)
    assert mod.API_BASE == "https://gitea.example.com/api/v1", "API_BASE 必须取卡，不得回落内置域名"
    params = ce.clone_params("gitea:acme/widget")
    assert params["provider"] == "gitea"
    assert params["clone_url"] == "https://gitea.example.com/acme/widget.git"
    assert params["clone_auth"] == "basic_password"
    assert params["username"] == "gitea-bot"
    assert "token" not in params and "password" not in params


def test_unknown_platform_still_fails_fast(new_platform):
    ce, _cards = new_platform
    with pytest.raises(ce.CloneEndpointError):
        ce.clone_url("nosuchplatform:acme/widget")