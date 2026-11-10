"""Issue #1660：Agent 任务 env 注入中 REPO_OWNER/REPO_NAME 必须由 REPO_FULL_NAME 派生。

覆盖多路径一致性：
- derive_repo_identity 单一派生入口（REPO_FULL_NAME 权威、缺失回退 repo_key 并告警）；
- build_task_env（事件重派）；
- direct 执行分支（_execute_agent_inner 不再用触发人账号充当 REPO_OWNER）；
- chat/IM/VOD 共用 chat_repo_env（真实仓库注入，伪仓库不注入）。
"""
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC, "data", "test_sessions.db"))

from src.repo_key import derive_repo_identity  # noqa: E402


class TestDeriveRepoIdentity:
    def test_repository_is_authoritative_over_trigger_account(self):
        """反馈 2874 场景：REPO_FULL_NAME=acme-org/demo-portal，触发人≠owner → 取 REPO_FULL_NAME。"""
        full, owner, name = derive_repo_identity("acme-org/demo-portal", "gitcode:alice/demo-portal")
        assert (full, owner, name) == ("acme-org/demo-portal", "acme-org", "demo-portal")

    def test_repository_with_provider_prefix_normalized(self):
        full, owner, name = derive_repo_identity("gitcode:Owner/Repo", "")
        assert (full, owner, name) == ("Owner/Repo", "Owner", "Repo")

    def test_missing_repository_falls_back_to_repo_key(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            full, owner, name = derive_repo_identity("", "gitcode:Fallback/Repo")
        assert (full, owner, name) == ("Fallback/Repo", "Fallback", "Repo")
        assert any("fallback" in r.message.lower() for r in caplog.records)

    def test_repository_without_slash_uses_repo_key_fallback(self):
        full, owner, name = derive_repo_identity("OwnerOnly", "gitcode:Fallback/Repo")
        assert (full, owner, name) == ("Fallback/Repo", "Fallback", "Repo")

    def test_pseudo_or_empty_returns_empty(self):
        for repository, rk in (("", ""), ("", "chat:1"), ("chat:1", "chat:1")):
            assert derive_repo_identity(repository, rk) == ("", "", "")


class TestBuildTaskEnvDerivesFromFullName:
    def test_redispatch_env_owner_name_match_full_name(self):
        from src.api.event_pipeline import build_task_env
        env = build_task_env("gitcode:acme-org/demo-portal", "coding", "",
                             runtime_id=0, account="alice")
        assert env["REPO_FULL_NAME"] == "acme-org/demo-portal"
        assert env["REPO_OWNER"] == "acme-org"
        assert env["REPO_NAME"] == "demo-portal"
        # ACCOUNT 语义仍是触发人，不得污染 REPO_OWNER
        assert env["ACCOUNT"] == "alice"
        assert env["REPO_OWNER"] != env["ACCOUNT"]

    def test_repo_name_never_empty_when_full_name_present(self):
        from src.api.event_pipeline import build_task_env
        env = build_task_env("github:Org/Repo", "qa", "")
        assert env["REPO_FULL_NAME"] == "Org/Repo"
        assert env["REPO_OWNER"] == "Org"
        assert env["REPO_NAME"] == "Repo"


class TestDirectExecutorDerivesFromRepository:
    def test_direct_branch_source_no_longer_uses_exec_account_for_owner(self):
        import inspect
        from src.runtime import direct_executor
        source = inspect.getsource(direct_executor._execute_agent_inner)
        assert "derive_repo_identity" in source
        assert 'REPO_OWNER": (exec_account' not in source
        assert "split_owner_repo as _sor_env" not in source

    def test_chat_repo_env_real_repo(self):
        from src.api.chat_session_env import chat_repo_env
        env = chat_repo_env("gitcode:acme-org/demo-portal")
        assert env == {"REPO_FULL_NAME": "acme-org/demo-portal",
                       "REPO_OWNER": "acme-org", "REPO_NAME": "demo-portal"}

    def test_chat_repo_env_pseudo_repo_skipped(self):
        from src.api.chat_session_env import chat_repo_env
        for rk in ("", None, "chat:1"):
            assert chat_repo_env(rk) == {}


class TestChatSessionEnvIncludesRepoIdentity:
    def test_chat_session_env_injects_repo_identity(self):
        from unittest.mock import patch
        with patch("src.api.auth_token.issue_session_token", side_effect=Exception("skip token")):
            from src.api.chat_session_env import chat_session_env_extra
            env = chat_session_env_extra(99991, "conv-1660", "gitcode:Owner/Repo")
        assert env["REPO_FULL_NAME"] == "Owner/Repo"
        assert env["REPO_OWNER"] == "Owner"
        assert env["REPO_NAME"] == "Repo"