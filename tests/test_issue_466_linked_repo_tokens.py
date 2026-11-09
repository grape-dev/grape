"""Issue #466 断点二服务端侧：关联仓 token 随 env 分发（_inject_linked_repo_tokens）。

覆盖：
- 正常：按 GRAPE_LINKED_REPOS 逐仓解析注入 GRAPE_LINKED_REPO_TOKENS（跨平台混合仓）
- 无 token 的仓不入 map（worker 端回退本地解析）
- 空/坏 JSON / 非列表 → 不注入不抛异常
"""
import json
import os
import sys

# 强制使用独立测试库
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from unittest.mock import patch

import pytest

from src.api.event_pipeline import _inject_linked_repo_tokens


_LINKED = [
    {"repo_key": "gitcode:OPCTeam/HDBlogService", "branch": "main"},
    {"repo_key": "github:huaweicloud-mate/toolkit", "branch": "main"},
    {"repo_key": "gitcode:none/repo", "branch": "main"},
]


def _tok(rk):
    return {"gitcode:OPCTeam/HDBlogService": "tok_gc",
            "github:huaweicloud-mate/toolkit": "tok_gh"}.get(rk, "")


class TestInjectLinkedRepoTokens:

    def test_injects_tokens_for_linked_repos(self):
        env = {}
        with patch("src.token_service.get_repo_bot_token", _tok):
            _inject_linked_repo_tokens(env, json.dumps(_LINKED))
        m = json.loads(env["GRAPE_LINKED_REPO_TOKENS"])
        assert m == {"gitcode:OPCTeam/HDBlogService": "tok_gc",
                     "github:huaweicloud-mate/toolkit": "tok_gh"}, \
            "跨平台混合关联仓各取各的平台 token，无 token 的仓不入 map"

    def test_token_resolver_failure_skips_repo(self):
        """解析抛异常 → 该仓跳过（不抛出、不注入空值）。"""
        def _boom(rk):
            raise RuntimeError("db down")
        env = {}
        with patch("src.token_service.get_repo_bot_token", _boom):
            _inject_linked_repo_tokens(env, json.dumps(_LINKED))
        assert "GRAPE_LINKED_REPO_TOKENS" not in env, "全部失败时不应注入空 map"

    def test_empty_json_no_inject(self):
        env = {}
        _inject_linked_repo_tokens(env, "")
        assert "GRAPE_LINKED_REPO_TOKENS" not in env

    def test_broken_json_no_inject(self):
        env = {}
        _inject_linked_repo_tokens(env, "{broken")
        assert "GRAPE_LINKED_REPO_TOKENS" not in env

    def test_non_list_json_no_inject(self):
        env = {}
        _inject_linked_repo_tokens(env, '{"gitcode:a/b": "tok"}')
        assert "GRAPE_LINKED_REPO_TOKENS" not in env, "非列表结构应忽略"

    def test_no_token_repos_only_no_inject(self):
        """全部关联仓都解析不到 token → 不注入（worker 端回退本地口径）。"""
        env = {}
        with patch("src.token_service.get_repo_bot_token", lambda rk: ""):
            _inject_linked_repo_tokens(env, json.dumps(_LINKED))
        assert "GRAPE_LINKED_REPO_TOKENS" not in env
