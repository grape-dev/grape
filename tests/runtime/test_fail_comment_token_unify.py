"""fail_task 回帖令牌统一入口用例 S1–S4（D37/S57 收口）。

原状：401 分支按 repo_key 前缀二分取全局 bot token，且用了 mgmt 私有名
`_get_global_bot_token`；守卫 `test_s57_fail_task_uses_unified_global_token`
长期为红。现在收敛为函数内一处闭包 `_global_comment()` → 统一入口
`get_global_bot_token_for(provider)`。
"""
from __future__ import annotations

import inspect

from src.domains.mgmt import public as mgmt_public
from src.domains.mgmt.user import db as user_db
from src.domains.runtime.engine import service as svc


def _fail_task_code() -> str:
    src = inspect.getsource(svc.fail_task)
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def test_s1_single_unified_entry_no_prefix_branch():
    """S1：只有一处统一入口；不再出现 github 专用函数/私有名/前缀二分。"""
    code = _fail_task_code()
    assert code.count("get_global_bot_token_for(") == 1, code.count("get_global_bot_token_for(")
    assert "get_global_github_bot_token" not in code
    assert "_get_global_bot_token" not in code
    assert 'startswith("github:")' not in code


def test_s2_task_token_stays_last_resort():
    """S2：异常分支仍保留「全局为空 → 任务令牌」最后一档。"""
    assert "_get_task_token(task)" in _fail_task_code()


def test_s3_provider_can_be_anything_meta_derived(monkeypatch):
    """S4：未知/大小写 provider 走 meta key 派生（统一入口不按平台硬编码）。"""
    seen = {}

    def _meta(key, file_fallback_fn=None):
        seen["key"] = key
        return f"m:{key}"

    monkeypatch.setattr(user_db, "_cached_get_meta", _meta)
    monkeypatch.setattr(user_db, "_platform_bot_token", lambda provider: "")
    assert user_db.get_global_bot_token_for("gitee") == "m:global_bot_gitee_token"
    # token 值等于 meta 派生结果即证明未走平台硬编码分支
    assert mgmt_public.get_global_bot_token_for("gitlab") == "m:global_bot_gitlab_token"
    assert seen["key"] == "global_bot_gitlab_token"