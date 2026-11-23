"""VCS 凭据环境变量的通用判定（2026-11-22）

回归背景：hermes ACP 的 env 转发白名单与两处日志脱敏列表把平台写死成
BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN。后加的 gitee/gitlab 即使派发侧注入了
BOT_GITEE_TOKEN / BOT_GITLAB_TOKEN，也会在这里被裁掉：

  · 转发被裁 → 沙箱 askpass 回声为空 → git push 401（远端 worker 无法推代码）
  · 脱敏被漏 → 这些 token 原样落进日志

用例锁定：判定按谓词（任意 BOT_/USER_ + *_TOKEN），新增平台零改动；
同时保证非 VCS 的密钥（OPENAI_API_KEY 等）不进入凭据集合。
"""
import pytest

from src.domains.runtime.vcs.clone_endpoint import (
    is_vcs_token_env,
    vcs_token_env_names,
    vcs_token_values,
)
from src.domains.runtime.agents.executor import _build_acp_envs


@pytest.mark.parametrize("name", [
    "BOT_GITLAB_TOKEN", "USER_GITLAB_TOKEN",
    "BOT_GITEE_TOKEN", "USER_GITEE_TOKEN",
    "BOT_GITCODE_TOKEN", "BOT_GITHUB_TOKEN",
    "BOT_TOKEN",            # askpass 末级兜底
    "BOT_MYOWN_VCS_TOKEN",  # 未来平台/自建实例
])
def test_vcs_token_env_recognised(name):
    assert is_vcs_token_env(name)


@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "HERMES_CUSTOM_API_KEY", "JWT_SECRET",
    "BOT_USERNAME", "USER_NAME", "GRAPE_SESSION_TOKEN", "",
])
def test_non_vcs_env_not_recognised(name):
    assert not is_vcs_token_env(name)


def test_env_names_derived_per_platform():
    assert vcs_token_env_names("gitlab:grp/sub/repo") == ("BOT_GITLAB_TOKEN", "USER_GITLAB_TOKEN")
    assert vcs_token_env_names("gitee:owner/repo") == ("BOT_GITEE_TOKEN", "USER_GITEE_TOKEN")


def test_token_values_cover_every_platform():
    env = {
        "BOT_GITLAB_TOKEN": "gl", "USER_GITEE_TOKEN": "ge",
        "BOT_GITCODE_TOKEN": "gc", "BOT_TOKEN": "bt",
        "OPENAI_API_KEY": "sk-should-not-leak-into-list",
        "HERMES_CUSTOM_API_KEY": "x",
    }
    assert sorted(vcs_token_values(env)) == sorted(["gl", "ge", "gc", "bt"])


def test_acp_env_forwards_provider_tokens_and_context():
    """ACP 子进程必须拿到 gitee/gitlab 的 token —— 这正是此前被裁掉的部分。"""
    sandbox_envs = {"_HERMES_BIN": "/opt/hermes/bin/hermes"}
    proc_env = {
        "BOT_GITLAB_TOKEN": "gl-token",
        "USER_GITLAB_TOKEN": "gl-user",
        "BOT_GITEE_TOKEN": "ge-token",
        "BOT_GITCODE_TOKEN": "gc-token",
        "GRAPE_SERVER_URL": "http://127.0.0.1",
        "REPO_FULL_NAME": "account-a/demo-repo",
        "NO_COLOR": "1",
        "OPENAI_API_KEY": "sk-must-not-be-forwarded",
    }
    env = _build_acp_envs(sandbox_envs, proc_env)
    assert env["BOT_GITLAB_TOKEN"] == "gl-token"
    assert env["BOT_GITEE_TOKEN"] == "ge-token"
    assert env["USER_GITLAB_TOKEN"] == "gl-user"
    assert env["BOT_GITCODE_TOKEN"] == "gc-token"
    assert env["GRAPE_SERVER_URL"] == "http://127.0.0.1"
    assert env["NO_COLOR"] == "1"
    assert env["_HERMES_BIN"] == "/opt/hermes/bin/hermes"
    assert "OPENAI_API_KEY" not in env, "非 VCS 密钥不进 ACP 白名单"

def test_legacy_gitcode_key_still_works_for_other_platforms():
    """向前兼容：老 worker/老派发只注入 BOT_GITCODE_TOKEN，也要能取到 token。"""
    from src.domains.runtime.vcs.clone_endpoint import bot_token_from_env

    env = {"BOT_GITCODE_TOKEN": "legacy-token"}
    assert bot_token_from_env(env, "gitlab:grp/repo") == "legacy-token"
    assert bot_token_from_env(env, "gitee:owner/repo") == "legacy-token"


def test_provider_key_preferred_over_legacy():
    from src.domains.runtime.vcs.clone_endpoint import bot_token_from_env

    env = {"BOT_GITLAB_TOKEN": "new-token", "BOT_GITCODE_TOKEN": "legacy-token"}
    assert bot_token_from_env(env, "gitlab:grp/repo") == "new-token"


def test_askpass_script_falls_back_to_legacy_for_gitlab(tmp_path):
    """gitlab 的 askpass 链必须是 BOT_GITLAB → USER_GITLAB → BOT_TOKEN → BOT_GITCODE。"""
    from src.domains.runtime.engine.repogit import _write_askpass

    script = _write_askpass(str(tmp_path), "gitlab:grp/repo")
    text = open(script, encoding="utf-8").read()
    assert "BOT_GITLAB_TOKEN" in text and "USER_GITLAB_TOKEN" in text
    assert "BOT_GITCODE_TOKEN" in text, "老派发只写 gitcode 键时仍需兜底"
