"""D37（S57）用例：token 解析二分族收口——用户 token provider 口径 +
fail_task 全局 bot token 统一入口（先测后改）。

功能性破绽（gitee）：脱敏/chat 直连取用户 token 落 get_user_token_with_refresh
（默认 provider=gitcode）→ gitee 仓取空；fail_task 评论 fallback 落
_get_global_bot_token（GitCode 全局）→ gitee 仓无 token。
"""
import inspect
from unittest.mock import patch

import pytest


# ------------------------------------------------------- 1. helper：provider 口径

def test_s57_user_token_for_repo_provider_derivation():
    """新 helper：provider 由 repo_key 前缀派生，委托统一入口。"""
    from src.api.deps import get_user_token_for_repo
    seen = {}

    def _fake(username, user_id=0, provider="gitcode"):
        seen["provider"] = provider
        return f"tok-{provider}"

    with patch("src.api.deps.get_user_token_for", side_effect=_fake):
        assert get_user_token_for_repo("gitee:acme/web", "bob", 7) == "tok-gitee"
        assert seen["provider"] == "gitee"
        assert get_user_token_for_repo("github:octo/app", "bob", 7) == "tok-github"
        assert get_user_token_for_repo("gitcode:acme/web", "bob", 7) == "tok-gitcode"
        # 特征锁：裸 key = 存量默认 gitcode（与 get_user_token_with_refresh 语义一致）
        assert get_user_token_for_repo("acme/web", "bob", 7) == "tok-gitcode"


def test_s57_user_token_for_repo_empty_key():
    from src.api.deps import get_user_token_for_repo
    with patch("src.api.deps.get_user_token_for", return_value="") as gf:
        assert get_user_token_for_repo("", "bob", 7) == ""
    assert gf.call_args.kwargs.get("provider") == "gitcode"


# ------------------------------------------------------- 2. 调用点接线（结构守卫）

def test_s57_redact_and_chat_sites_use_helper():
    """结构守卫：脱敏块与 chat 直连块不再含 provider 二分字面量。"""
    from src.api import event_pipeline as ep
    from src.domains.runtime.im import handler_async as ha

    def _stripped(fn):
        src = inspect.getsource(fn)
        return "\n".join(ln.split("#")[0] for ln in src.splitlines())

    # event_pipeline 脱敏块在 _execute_agent_inner 内：源级检查整个函数
    ep_src = inspect.getsource(ep)
    assert "get_user_token_for_repo" in ep_src, "脱敏块未接 helper"
    # chat 直连
    ha_src = _stripped(ha._process_chat_inner_async)
    assert "get_user_token_for_repo" in ha_src, "chat 直连块未接 helper"


def test_s57_fail_task_uses_unified_global_token():
    """结构守卫：fail_task 不再含 get_global_github_bot_token 二分字面量。"""
    from src.domains.runtime.engine import service as svc
    src = inspect.getsource(svc.fail_task)
    stripped = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "get_global_github_bot_token" not in stripped, "fail_task 仍二分取全局 bot token"
    assert "get_global_bot_token_for" in stripped, "fail_task 未接统一入口"


# ------------------------------------------------------- 3. 语义等价锁（存量行为）

def test_s57_global_token_wrappers_equivalent():
    """特征锁：统一入口对存量平台即原函数（github/gitcode 包装等价）。"""
    from src.domains.mgmt.user.db import (get_global_bot_token_for, get_global_github_bot_token,
                             _get_global_bot_token)
    with patch("src.domains.mgmt.user.db._cached_get_meta", side_effect=lambda key, file_fallback_fn=None: f"m:{key}"):
        assert get_global_bot_token_for("github") == get_global_github_bot_token()
        assert get_global_bot_token_for("gitcode") == _get_global_bot_token()
        # gitee（config-only）：meta key 派生，无文件 fallback
        assert get_global_bot_token_for("gitee") == "m:global_bot_gitee_token"


def test_s57_user_token_gitcode_parity():
    """特征锁：provider='gitcode' 的 get_user_token_for 即 get_user_token_with_refresh
    （D3 薄委托等价——存量 gitcode 行为不变）。"""
    from src.api import deps
    with patch.object(deps, "get_user_token_for", return_value="tok-gc") as gf:
        assert deps.get_user_token_with_refresh("u", user_id=7) == "tok-gc"
    assert gf.call_args.kwargs.get("provider") == "gitcode"
