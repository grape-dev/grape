"""webhook 鉴权路径 B 的判定必须认方言要求，不能只看"有没有 bot token"。

回归背景：三个托管入口（host / repair-host / create 内联托管）各自写
`_use_path_b = not bot_token`。一旦给 GitLab 配了全局 bot token，内联托管会把
PAT 当 webhook 密钥写进 hook（项目维护者可见），且 DB 侧密钥为空 → 投递全 404。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.user.repo_manager import resolve_use_path_b


@pytest.mark.parametrize(
    "cfg,bot_token,expected",
    [
        # 方言强制路径 B：即便有 bot token 也走 B（PAT 不外泄）
        ({"webhook_path_b": True}, "bot-token-abc", True),
        ({"webhook_path_b": True}, "", True),
        # 方言不强制：有 bot token 走 A
        ({}, "bot-token-abc", False),
        ({"webhook_path_b": False}, "bot-token-abc", False),
        # 无 bot token：无论如何走 B
        ({}, "", True),
        ({"webhook_path_b": False}, None, True),
        # 容错：cfg 为空
        (None, "bot-token-abc", False),
    ],
)
def test_resolve_use_path_b(cfg, bot_token, expected):
    assert resolve_use_path_b(cfg, bot_token) is expected


def test_all_hosting_entries_share_one_decision():
    """三个托管入口不得再各自手写判定（防再次漂移）。"""
    import inspect

    from src.domains.mgmt.routes import repos as repos_mod

    src = inspect.getsource(repos_mod)
    assert src.count("resolve_use_path_b(") >= 3
    # 旧写法（只按 token 有无）不得残留
    assert "bool(_hcfg.get(\"webhook_path_b\")) or not" not in src
    assert "bool(_cfg.get(\"webhook_path_b\")) or not" not in src


def test_create_entry_uses_user_token_on_path_b():
    """路径 B 的 webhook 密钥必须是用户 token，不能用全局 bot token。"""
    import inspect

    from src.domains.mgmt.routes import repos as repos_mod

    src = inspect.getsource(repos_mod.create_new_repo)
    assert "api_token if _use_path_b else (_create_global_bot_token or api_token)" in src