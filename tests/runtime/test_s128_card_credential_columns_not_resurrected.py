"""S128/D106 终态守卫：gitee/gitlab 的凭据列不得被「空串归一化」重新派生回来。

回归背景（架构守护 2026-11-23 / E1）：`card_projection` 与契约通道把**所有**空串列
当「未配置」省略，其中 `token_storage_field` / `webhook_token_field` 的空串本义是
**声明**（该平台无 managed_repos 物理列，凭据只落 grape_t_repo_credentials）。键被丢
掉后 Builder 回退派生列名（`bot_gitee_token` / `gitee_webhook_token`），于是：

- `_ensure_platform_storage_columns` 每次冷启动把 S128 已退役的四列重新建出来；
- `_update_repo_bot_token` 又按派生列名把凭据写回退役列，绕过「写 rc 后置空旧列」；
- 表驱动部署（`grape_t_repo_platforms` 的凭据列列值全为 NULL）是主命中面。

本文件锁三条不变量：语义空串存活、其余空串仍省略、装配后 gitee/gitlab 无列而
gitcode/github 仍派生存量列。
"""
from __future__ import annotations

import json

from src.domains.runtime.vcs import card_contract, platform_card
from src.domains.runtime.vcs.card_projection import (
    SEMANTIC_EMPTY_KEYS,
    row_to_card_raw,
)
from src.domains.runtime.vcs.card_sources import PlatformCardAssembler
from src.domains.runtime.vcs.platform_card import (
    _PLATFORM_CARD_COLUMNS,
    _TABLE_SKIP,
    build_platform_card,
)


class _Src:
    """最小 PlatformCardSource 替身（本文件不碰 DB / HTTP）。"""

    name = "fake"

    def __init__(self, raws, degraded=""):
        self._raws = raws
        self.degraded = degraded

    def raws(self):
        return dict(self._raws)


class _File:
    def __init__(self, raws):
        self._raws = raws

    def available(self):
        return True

    def raws(self):
        return dict(self._raws)


class _FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _contract_cards(cards: dict) -> dict:
    payload = json.dumps({"cards": cards})
    client = card_contract.PlatformCardContractClient(
        base_url="http://127.0.0.1:8080", token="rta_fake_test_token", timeout=1.0,
        opener=lambda req, timeout=None: _FakeResp(payload))
    return client.fetch_cards()


def _table_row(provider: str, dialect: str, **over):
    """模拟 mgmt 表行：NULL 列被跨域读原语统一成 ""（见 card_projection docstring），
    再经 `row_to_card_raw` 投影成卡 raw（表源的真实取数链）。"""
    row = {
        "provider": provider, "dialect": dialect,
        "name": provider.title(), "host": f"{provider}.example.com",
        "api_base": f"https://{provider}.example.com/api",
        "webhook_verify": "", "branch_create_form": "", "issue_write_form": "",
        "token_storage_field": "", "webhook_token_field": "",
    }
    row.update(over)
    return row


def _table_card(provider: str, dialect: str, **over) -> dict:
    """表源的真实链：表行 → row_to_card_raw 投影 → 卡 raw。"""
    return row_to_card_raw(_table_row(provider, dialect, **over),
                           _PLATFORM_CARD_COLUMNS, _TABLE_SKIP)


# ------------------------------------------------ 投影：语义空串必须存活

def test_projection_keeps_semantic_empty_credential_columns():
    raw = _table_card("gitee", "github")
    assert set(SEMANTIC_EMPTY_KEYS) <= set(raw), (
        f"凭据列空串被投影丢掉，Builder 会回退派生列名: {sorted(raw)}")
    assert raw["token_storage_field"] == ""
    assert raw["webhook_token_field"] == ""


def test_projection_still_drops_other_empty_columns():
    raw = _table_card("gitee", "github")
    assert "webhook_verify" not in raw
    assert "branch_create_form" not in raw
    assert raw["dialect"] == "github"


# ------------------------------------------------ 契约通道：同一口径

def test_contract_keeps_semantic_empty_credential_columns():
    got = _contract_cards({"gitee": {
        "dialect": "github", "host": "gitee.example.com",
        "token_storage_field": "", "webhook_token_field": "",
        "webhook_verify": "", "branch_create_form": "",
    }})
    assert got["gitee"]["token_storage_field"] == ""
    assert got["gitee"]["webhook_token_field"] == ""
    assert "webhook_verify" not in got["gitee"]
    assert "branch_create_form" not in got["gitee"]


def test_contract_card_builds_without_resurrecting_columns():
    got = _contract_cards({"gitee": {
        "dialect": "github", "host": "gitee.example.com",
        "token_storage_field": "", "webhook_token_field": "",
    }})
    card = build_platform_card("gitee", got["gitee"])
    assert card.token_storage_field == ""
    assert card.webhook_token_field == ""


def test_contract_non_empty_column_still_wins():
    got = _contract_cards({"gitlab-corp": {
        "dialect": "gitlab", "host": "gl.example.com",
        "token_storage_field": "bot_gitlab_corp_token",
        "webhook_token_field": "gitlab_corp_webhook_token",
    }})
    card = build_platform_card("gitlab-corp", got["gitlab-corp"])
    assert card.token_storage_field == "bot_gitlab_corp_token"
    assert card.webhook_token_field == "gitlab_corp_webhook_token"


# ------------------------------------------------ 装配：终态 vs 存量列

def _assembled(table_raws: dict, file_raws: dict) -> dict:
    a = PlatformCardAssembler(
        meta=_Src({}),
        file_source=_File(file_raws),
        builtin=_Src({}),
        fallback_sources=[_Src(table_raws)],
    )
    return a.assemble_raws()


def test_table_row_does_not_resurrect_gitee_gitlab_columns():
    """表行凭据列为空串（NULL 归一）→ 文件卡的显式终态 "" 必须赢下来。"""
    raws = _assembled(
        table_raws={"gitee": _table_card("gitee", "github"),
                    "gitlab": _table_card("gitlab", "gitlab")},
        file_raws={
            "gitee": {"dialect": "github", "host": "gitee.example.com",
                      "token_storage_field": "", "webhook_token_field": ""},
            "gitlab": {"dialect": "gitlab", "host": "gitlab.example.com",
                       "token_storage_field": "", "webhook_token_field": ""},
        },
    )
    for provider in ("gitee", "gitlab"):
        card = build_platform_card(provider, raws[provider])
        assert card.token_storage_field == "", f"{provider} 退役列被重新派生"
        assert card.webhook_token_field == "", f"{provider} 退役 webhook 列被重新派生"


def test_table_row_keeps_legacy_columns_for_gitcode_github():
    """文件卡没声明列名的平台（gitcode/github）仍派生存量列，加卡即接入链不断。"""
    raws = _assembled(
        table_raws={"gitcode": _table_card("gitcode", "github"),
                    "github": _table_card("github", "github")},
        file_raws={
            "gitcode": {"dialect": "github", "host": "gitcode.com"},
            "github": {"dialect": "github", "host": "github.com"},
        },
    )
    gc = build_platform_card("gitcode", raws["gitcode"])
    gh = build_platform_card("github", raws["github"])
    assert gc.token_storage_field == "bot_gitcode_token"
    assert gc.webhook_token_field == "gitcode_webhook_token"
    assert gh.token_storage_field == "bot_github_token"
    assert gh.webhook_token_field == "github_webhook_token"


def test_config_only_platform_without_file_card_keeps_derivation():
    """仅在表里存在的平台（无文件卡）行为不变：键缺席 → 派生列名。"""
    raws = _assembled(
        table_raws={"gitlab-alt": _table_card("gitlab-alt", "gitlab")},
        file_raws={},
    )
    card = build_platform_card("gitlab-alt", raws["gitlab-alt"])
    assert card.token_storage_field == "bot_gitlab-alt_token"
    assert card.webhook_token_field == "gitlab-alt_webhook_token"