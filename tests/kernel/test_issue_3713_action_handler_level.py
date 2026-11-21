"""Issue #3713 收口：`create_action_handler` 必须认识新的 token 级别。

`create_action_handler(result)` 按 `result.level` 分派，只认 `repo_bot` /
`global_bot` / `user`。链首新增 `rc_bot` 后，若不同步映射，落 `None` →
401/403 之后的 `mark_expired` / `clear_expired` 对新级别**静默失效**：
过期短路在「回写面」缺一环，运维下次仍然看到过期行被当可用。
"""
from __future__ import annotations

import pytest

from issue_3713_fakes import CredWorld, install, restore_ports, snapshot_ports
from src.kernel.token_resolver import (
    RepoBotTokenHandler,
    TokenResult,
    create_action_handler,
)

RK = "gitee:acme/demo"


def _result(provider):
    return TokenResult(token="rc-bot-token", level="rc_bot", provider=provider, repo_key=RK)



@pytest.fixture(autouse=True)
def _ports_baseline():
    """端口是进程级绑定：每个用例结束还原到进入时的快照。

    不能用「全清」：装配层绑的 `vcs_cards` 会被抹掉，同进程后续用例造不出 provider。
    """
    snapshot = snapshot_ports()
    yield
    restore_ports(snapshot)

def test_action_handler_maps_explicit_level(monkeypatch):
    provider = install(monkeypatch, CredWorld(rc_value="rc-bot-token"))
    handler = create_action_handler(_result(provider))
    assert handler is not None, "新级别落 None → mark_expired/clear_expired 静默失效"
    assert isinstance(handler, RepoBotTokenHandler), (
        "rc_bot 与 repo_bot 共用 managed_repos.bot_token_status 一列，回写语义相同")


def test_mark_expired_writes_the_shared_status_column(monkeypatch):
    world = CredWorld(rc_value="rc-bot-token")
    provider = install(monkeypatch, world)
    handler = create_action_handler(_result(provider))

    code = handler.mark_expired(_result(provider))
    assert code == "REPO_TOKEN_EXPIRED"
    assert world.bot_status == "expired", "过期标记必须落到 bot_token_status（唯一来源）"


def test_clear_expired_releases_the_shared_status_column(monkeypatch):
    world = CredWorld(rc_value="rc-bot-token", bot_status="expired")
    provider = install(monkeypatch, world)
    handler = create_action_handler(_result(provider))

    handler.clear_expired(_result(provider))
    assert world.bot_status == "", "重新配置凭据后必须能清除过期标记"


def test_known_levels_still_map(monkeypatch):
    provider = install(monkeypatch, CredWorld(rc_value="rc-bot-token"))
    for level in ("repo_bot", "global_bot", "user"):
        assert create_action_handler(TokenResult(token="t", level=level,
                                                 provider=provider, repo_key=RK)) is not None
    assert create_action_handler(TokenResult(token="t", level="",
                                            provider=provider, repo_key=RK)) is None