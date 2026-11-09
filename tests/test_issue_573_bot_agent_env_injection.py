"""Issue #573: BOT_USERNAME / AGENT_NAME 环境变量注入（全部 devops agent）

根因：event_pipeline 三个注入点（正常入队 / direct 直连 / build_task_env 重派）
原先仅 `agent_type == "coding"` 注入 BOT_USERNAME，issue agent（issue-runbook
Step 4 机器人接管 assignee）等其余 agent 拿不到值；AGENT_NAME 仅 direct 模式经
stream_devops_agent 参数注入，worker 模式 env 取自 env_extra 快照从未写入。

验收：
1. _resolve_bot_username 三级兜底：repo 级 → 全局 bot（provider 分流）→ host_user；
   全空返回空串（下游按空值降级，不注入占位值）。
2. _inject_bot_agent_env：issue/qa/coding 等 devops agent 均注入两变量，
   且幂等（已显式设置不覆盖）。
3. build_task_env（重派路径）同样注入。
4. 三个注入点统一走 helper（防第四处旁路 coding 门控回归）。
"""
import inspect

import pytest

import src.api.event_pipeline as ep
from src.api.event_pipeline import (
    _inject_bot_agent_env,
    _resolve_bot_username,
    build_task_env,
)


# ─────────────────────────── _resolve_bot_username ───────────────────────────

class TestResolveBotUsername:
    def test_repo_level_bot_username_wins(self, monkeypatch):
        """repo 级 managed_repos.bot_username 优先（GitCodeRepoConfig 来源）"""
        import sqlite3
        from src.session_manager import db

        with db:  # noqa: SIM115（会话级共享连接）
            db.execute("DELETE FROM managed_repos WHERE repo_key=?", ("gitcode:octo/issue573",))
            db.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
                "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, "
                "bot_username, github_webhook_token, bot_github_token) "
                "VALUES ('gitcode:octo/issue573','octo','issue573','octo/issue573',"
                "0,'','','','repo_bot','','')")

        import src.user.db as udb
        monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: ["global_bot"])
        try:
            assert _resolve_bot_username("gitcode:octo/issue573") == "repo_bot"
        finally:
            with db:  # noqa: SIM115
                db.execute("DELETE FROM managed_repos WHERE repo_key=?", ("gitcode:octo/issue573",))

    def test_global_gitcode_fallback(self, monkeypatch):
        """repo 级缺省 → 全局 bot username（gitcode 分流取第一个）"""
        import src.user.db as udb
        monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: ["global_bot", "other"])
        monkeypatch.setattr(udb, "get_global_github_bot_username", lambda: "gh_bot")
        assert _resolve_bot_username("gitcode:octo/issue573b") == "global_bot"

    def test_global_github_fallback(self, monkeypatch):
        """github 仓库 repo 级缺省 → get_global_github_bot_username"""
        import src.user.db as udb
        monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: ["global_bot"])
        monkeypatch.setattr(udb, "get_global_github_bot_username", lambda: "gh_bot")
        assert _resolve_bot_username("github:octo/issue573") == "gh_bot"

    def test_host_user_fallback(self, monkeypatch):
        """全局为空 → host_user 平台用户名（Issue #372 路径 B）"""
        import src.user.db as udb
        import src.auth.grape_account_db as gdb
        monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: [])
        monkeypatch.setattr(udb, "get_global_github_bot_username", lambda: "")
        monkeypatch.setattr(udb, "get_host_user_id", lambda rk: 42)
        monkeypatch.setattr(
            gdb, "get_user_identities",
            lambda hid: [
                {"provider": "gitcode", "external_login": "host_login"},
                {"provider": "github", "external_login": "gh_login"},
            ])
        assert _resolve_bot_username("gitcode:octo/issue573c") == "host_login"

    def test_all_empty_returns_empty_string(self, monkeypatch):
        """三级兜底全空 → 空串（不注入占位值，skill 按空值降级）"""
        import src.user.db as udb
        monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: [])
        monkeypatch.setattr(udb, "get_global_github_bot_username", lambda: "")
        monkeypatch.setattr(udb, "get_host_user_id", lambda rk: 0)
        assert _resolve_bot_username("gitcode:octo/issue573d") == ""


# ─────────────────────────── _inject_bot_agent_env ───────────────────────────

class TestInjectBotAgentEnv:
    @pytest.mark.parametrize("agent_type", ["issue", "coding", "qa", "sre", "vod"])
    def test_all_devops_agents_get_both_vars(self, monkeypatch, agent_type):
        """全部 devops agent 注入 BOT_USERNAME + AGENT_NAME（原仅 coding 有 BOT_USERNAME）"""
        monkeypatch.setattr(ep, "_resolve_bot_username", lambda rk: "grape_bot")
        monkeypatch.setattr(ep, "get_agent_display_name", lambda at: f"{at}-display")
        env = {}
        _inject_bot_agent_env(env, "gitcode:octo/issue573", agent_type)
        assert env["BOT_USERNAME"] == "grape_bot"
        assert env["AGENT_NAME"] == f"{agent_type}-display"

    def test_idempotent_no_override(self, monkeypatch):
        """上层已显式设置（手工调参）→ 不覆盖（setdefault 语义）"""
        monkeypatch.setattr(ep, "_resolve_bot_username", lambda rk: "auto_bot")
        monkeypatch.setattr(ep, "get_agent_display_name", lambda at: "auto_name")
        env = {"BOT_USERNAME": "manual_bot", "AGENT_NAME": "manual_name"}
        _inject_bot_agent_env(env, "gitcode:octo/issue573", "issue")
        assert env["BOT_USERNAME"] == "manual_bot"
        assert env["AGENT_NAME"] == "manual_name"

    def test_empty_bot_username_still_injected(self, monkeypatch):
        """bot username 解析为空串 → 仍注入空值（与原 coding 行为一致，skill 侧降级）"""
        monkeypatch.setattr(ep, "_resolve_bot_username", lambda rk: "")
        monkeypatch.setattr(ep, "get_agent_display_name", lambda at: "Issue Agent")
        env = {}
        _inject_bot_agent_env(env, "gitcode:octo/issue573", "issue")
        assert env["BOT_USERNAME"] == ""
        assert env["AGENT_NAME"] == "Issue Agent"


# ─────────────────────────── build_task_env（重派路径） ───────────────────────────

class TestBuildTaskEnv:
    def test_reenqueue_path_injects_both(self, monkeypatch):
        """重派路径 build_task_env 同样注入两变量（worker env 快照来源）"""
        monkeypatch.setattr(ep, "_resolve_bot_username", lambda rk: "grape_bot")
        monkeypatch.setattr(ep, "get_agent_display_name", lambda at: "Issue Agent")
        env = build_task_env("gitcode:octo/issue573", "issue", "#573")
        assert env["BOT_USERNAME"] == "grape_bot"
        assert env["AGENT_NAME"] == "Issue Agent"


# ─────────────────────────── 注入点统一走 helper（防旁路回归） ───────────────────────────

class TestSingleHelperPath:
    def test_no_coding_gated_bot_username_blocks_left(self):
        """event_pipeline 源码不再存在 coding 门控的 BOT_USERNAME 注入块"""
        src = inspect.getsource(ep)
        assert 'env_extra["BOT_USERNAME"]' not in src
        assert 'env["BOT_USERNAME"]' not in src
        # helper 是唯一写入点
        assert src.count('_inject_bot_agent_env(env') >= 3  # enqueue/direct/build_task_env

    def test_agent_name_reserved_for_scene_custom_env(self):
        """AGENT_NAME 加入场景自定义变量保留集（防同名冲突覆盖）"""
        from src.scene_custom_env import is_reserved_name
        assert is_reserved_name("AGENT_NAME")
        assert is_reserved_name("BOT_USERNAME")
