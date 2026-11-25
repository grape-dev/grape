"""Issue #4363：派发路径仓库三变量（REPO_FULL_NAME/REPO_OWNER/REPO_NAME）必须齐备。

覆盖：
1. 任务级契约清单纳入仓库三变量（缺失可感知、可补齐）；
2. 「REPO_OWNER 已存在、REPO_NAME 缺失」场景逐变量补齐（旧闸门整块跳过）；
3. chat 兜底块同源 derive_repo_identity、仅补缺失键不覆盖显式值；
4. direct 执行分支不得再用 exec_account 二次 split 覆盖派生结果；
5. 无仓库上下文的通用 chat 任务不刷 contract missing 噪声。
"""
import os
import sys
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.agents import executor as ex  # noqa: E402

REPO_KEYS = ("REPO_FULL_NAME", "REPO_OWNER", "REPO_NAME")


def _complete_other_contract(env):
    env.setdefault("DEVOPS_EVENT_TYPE", "issue_opened")
    env.setdefault("ACCOUNT", "alice")
    env.setdefault("GRAPE_ISSUE_ID", "13465")
    env.setdefault("GRAPE_WORK_BRANCH", "weekly-mys")
    return env


class TestContractIncludesRepoIdentity:
    def test_repo_identity_in_runtime_contract(self):
        for name in REPO_KEYS:
            assert name in ex._RUNTIME_CONTRACT_ENV


class TestApplyContractFillsOnlyMissingRepoName:
    def test_owner_present_name_missing_is_filled(self):
        env = _complete_other_contract({
            "REPO_FULL_NAME": "acme-org/demo-portal",
            "REPO_OWNER": "acme-org",
        })
        with mock.patch.object(ex.log, "warning") as warn:
            ex._apply_runtime_env_contract(
                env, event_type="issue_opened", account="alice",
                repo_key="gitcode:acme-org/demo-portal",
                label="[Agent:coding] ", backend_name="opencode",
            )
        assert env["REPO_NAME"] == "demo-portal"
        assert env["REPO_NAME"] == env["REPO_FULL_NAME"].split("/")[-1]
        assert env["REPO_OWNER"] == "acme-org"
        text = " ".join(str(c.args[0]) for c in warn.call_args_list if c.args)
        assert "contract missing" not in text

    def test_missing_full_name_derives_from_repo_key(self):
        env = _complete_other_contract({"REPO_OWNER": "acme-org", "REPO_NAME": ""})
        ex._apply_runtime_env_contract(
            env, event_type="issue_opened", repo_key="gitcode:acme-org/demo-portal",
        )
        assert env["REPO_FULL_NAME"] == "acme-org/demo-portal"
        assert env["REPO_NAME"] == "demo-portal"


class TestChatAssembleFillsPerVariable:
    def test_chat_gate_no_longer_skips_when_owner_present(self):
        env = {"REPO_FULL_NAME": "acme-org/demo-portal", "REPO_OWNER": "acme-org"}
        ex._assemble_env(env, kind="chat", label="[Agent:chat] ",
                         repo_key="gitcode:acme-org/demo-portal")
        assert env["REPO_NAME"] == "demo-portal"
        assert env["REPO_OWNER"] == "acme-org"

    def test_chat_explicit_name_not_overridden(self):
        env = {"REPO_FULL_NAME": "acme-org/demo-portal",
               "REPO_OWNER": "acme-org", "REPO_NAME": "custom-name"}
        ex._assemble_env(env, kind="chat", label="[Agent:chat] ",
                         repo_key="gitcode:acme-org/demo-portal")
        assert env["REPO_NAME"] == "custom-name"

    def test_chat_pseudo_repo_not_injected(self):
        env = {}
        ex._assemble_env(env, kind="chat", label="[Agent:chat] ", repo_key="chat:42")
        for name in REPO_KEYS:
            assert name not in env


class TestNoRepoContextDoesNotWarnRepoMissing:
    def test_generic_chat_without_repo_skips_repo_missing_warning(self):
        env = _complete_other_contract({})
        with mock.patch.object(ex.log, "warning") as warn:
            ex._apply_runtime_env_contract(
                env, event_type="chat", repo_key="",
                label="[Agent:chat] ", backend_name="opencode",
            )
        text = " ".join(str(c.args[0]) for c in warn.call_args_list if c.args)
        assert "contract missing" not in text


class TestDirectExecutorDoesNotUseExecAccountSplit:
    def test_direct_branch_source_derives_identity_only(self):
        import inspect
        from src.domains.runtime.engine import direct_executor
        source = inspect.getsource(direct_executor._execute_agent_inner)
        assert "derive_repo_identity" in source
        assert "split_owner_repo as _sor_env" not in source
        assert "exec_account or repository" not in source