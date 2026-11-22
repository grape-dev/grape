"""settings 存令牌与用户名冲突校验的顺序（评审补强①）。

旧实现：先 update_managed_repo_bot_token 再校验冲突 → 冲突时返回 400 但令牌已落库（半写）。
口径：校验必须写在两次写之前。
"""
from __future__ import annotations

import pathlib


def test_conflict_check_precedes_both_writes():
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/domains/mgmt/routes/settings.py").read_text(encoding="utf-8")
    check = src.index("_assert_bot_username_free((body.bot_username or _verified_login")
    token_write = src.index("update_managed_repo_bot_token(repo_key, token)")
    user_write = src.index("update_managed_repo_bot_username(repo_key, body.bot_username)")
    assert check < token_write < user_write


def test_no_write_before_the_early_check_for_tokenless_updates():
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/domains/mgmt/routes/settings.py").read_text(encoding="utf-8")
    # 只改用户名（无令牌）时也要先校验：校验调用出现在用户名写入之前
    tail = src[src.index("if body.bot_username:") :]
    assert "_assert_bot_username_free" in tail
    assert tail.index("_assert_bot_username_free") < tail.index("update_managed_repo_bot_username")
