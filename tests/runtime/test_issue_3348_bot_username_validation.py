"""Issue #3348: BOT_USERNAME 注入值收敛 + 平台有效性校验。

背景：任务 t_c2a24ae42f99 注入 BOT_USERNAME=my_grape_bot（来自 host_user
external_login 兜底），GitCode 上不存在该用户，issues update assignee 返回
404 USER_NOT_FOUND，labels+body+assignee 单请求整体失败。

验收（Issue 评论 Spec）：
1. 取值仅两级：managed_repos.bot_username → 全局 bot username；host_user
   external_login 不再是来源。
2. 注入前平台校验：用户存在、类型 User（Organization 拒绝）、可被分配；
   无效 → 注入空串 + WARN，不透传下游。
3. agent_logs 写入 BOT_USERNAME_SOURCE=managed_repo|global|none。
4. 覆盖：两级命中、host_user 不再作为来源、组织名拒绝、不存在用户拒绝、
   网络异常 fail-open、60s TTL 缓存生效、无效值不触发下游 assignee update。
"""
import logging

import pytest

import src.domains.runtime.pipeline.event_pipeline as ep
from src.domains.runtime.pipeline.event_pipeline import (
    _inject_bot_agent_env,
    _resolve_bot_username_info,
    _validate_bot_username,
)

REPO_KEY = "gitcode:octo/issue3348"


@pytest.fixture(autouse=True)
def _clear_validation_cache():
    ep._bot_username_validation_cache.clear()
    yield
    ep._bot_username_validation_cache.clear()


def _patch_platform(monkeypatch, *, data=None, exc=None, token="bot-token"):
    """替换 bot token 与平台客户端，返回调用计数。"""
    calls = {"user": 0}

    monkeypatch.setattr(
        "src.kernel.token_service.get_bot_identity_token",
        lambda _rk: token)

    class _Client:
        def get_user(self, username, timeout=None):
            calls["user"] += 1
            if exc is not None:
                raise exc
            return data

    monkeypatch.setattr(
        "src.domains.runtime.vcs.api_client.create_client",
        lambda _rk, _tok: _Client())
    return calls


# ─────────────────────────── 取值收敛：两级来源 ───────────────────────────

class TestResolveBotUsernameSource:
    def test_managed_repo_level_hit(self, monkeypatch):
        from src.kernel.session_manager import db, db_lock

        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            db.cursor().execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
                "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, "
                "bot_username, github_webhook_token, bot_github_token) "
                "VALUES (?,'octo','issue3348','octo/issue3348',"
                "0,'','','','managed_bot','','')", (REPO_KEY,))
            db.commit()
        try:
            assert _resolve_bot_username_info(REPO_KEY) == ("managed_bot", "managed_repo")
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
                db.commit()

    def test_global_level_hit(self, monkeypatch):
        import src.domains.mgmt.user.db as udb
        from src.kernel.session_manager import db, db_lock

        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            db.commit()
        monkeypatch.setattr(udb, "get_global_bot_usernames_for",
                            lambda _p: ["grape_bot", "other"])
        assert _resolve_bot_username_info(REPO_KEY) == ("grape_bot", "global")

    def test_host_user_external_login_not_source(self, monkeypatch):
        """Issue #3348 核心回归：全局缺失时不再回落 host_user external_login。"""
        import src.domains.mgmt.auth.grape_account_db as gdb
        import src.domains.mgmt.user.db as udb
        from src.kernel.session_manager import db, db_lock

        calls = {"host": 0, "links": 0}
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            db.commit()
        monkeypatch.setattr(udb, "get_global_bot_usernames_for", lambda _p: [])
        monkeypatch.setattr(udb, "get_host_user_id",
                            lambda _rk: calls.__setitem__("host", calls["host"] + 1) or 42)
        monkeypatch.setattr(gdb, "list_platform_links",
                            lambda _hid: calls.__setitem__("links", calls["links"] + 1)
                            or [{"provider": "gitcode", "external_login": "my_grape_bot"}])

        assert _resolve_bot_username_info(REPO_KEY) == ("", "none")
        assert calls == {"host": 0, "links": 0}


# ─────────────────────────── 平台校验 ───────────────────────────

class TestValidateBotUsername:
    def test_user_exists_and_is_user_type(self, monkeypatch):
        _patch_platform(monkeypatch, data={"login": "grape_bot", "type": "User"})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")

    def test_organization_rejected(self, monkeypatch):
        _patch_platform(monkeypatch, data={"login": "grape-dev", "type": "Organization"})
        assert _validate_bot_username(REPO_KEY, "grape-dev") == (False, "organization")

    def test_nonexistent_user_rejected(self, monkeypatch):
        _patch_platform(monkeypatch,
                        data={"error": "HTTP 404", "status_code": 404})
        assert _validate_bot_username(REPO_KEY, "my_grape_bot") == (False, "user_not_found")

    def test_blocked_user_not_assignable(self, monkeypatch):
        _patch_platform(monkeypatch,
                        data={"login": "grape_bot", "type": "User", "state": "blocked"})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (False, "not_assignable")

    def test_platform_assignable_flag_false_rejected(self, monkeypatch):
        """复审 Spec②：平台返回可指派标记时按标记判定，而非只看 blocked/state。"""
        _patch_platform(monkeypatch,
                        data={"login": "grape_bot", "type": "User", "assignable": False})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (False, "not_assignable")

    def test_typeless_user_response_not_rejected(self, monkeypatch):
        """Gitee/GitCode 部分响应无 type 字段时不应误判为组织。"""
        _patch_platform(monkeypatch, data={"login": "grape_bot"})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")

    def test_network_error_fail_open(self, monkeypatch):
        _patch_platform(monkeypatch, exc=RuntimeError("connection reset"))
        valid, reason = _validate_bot_username(REPO_KEY, "grape_bot")
        assert valid is True
        assert reason.startswith("fail_open:")

    def test_no_token_fail_open_without_network(self, monkeypatch):
        calls = _patch_platform(monkeypatch, token="")
        valid, reason = _validate_bot_username(REPO_KEY, "grape_bot")
        assert valid is True
        assert reason == "fail_open:no_token"
        assert calls["user"] == 0

    def test_blocked_string_false_not_rejected(self, monkeypatch):
        """复审 Spec②：blocked 为字符串 "false" 时不得误判为不可指派。"""
        _patch_platform(monkeypatch,
                        data={"login": "grape_bot", "type": "User", "blocked": "false"})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")

    def test_blocked_string_true_rejected(self, monkeypatch):
        _patch_platform(monkeypatch,
                        data={"login": "grape_bot", "type": "User", "blocked": "true"})
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (False, "not_assignable")

    def test_network_error_fail_open_not_cached(self, monkeypatch):
        """复审 Spec④：fail-open 结果不缓存，瞬时故障后下一次立即重试。"""
        calls = _patch_platform(monkeypatch, exc=RuntimeError("connection reset"))
        valid, reason = _validate_bot_username(REPO_KEY, "grape_bot")
        assert valid is True and reason.startswith("fail_open:")
        _validate_bot_username(REPO_KEY, "grape_bot")
        assert calls["user"] == 2, "fail-open 不应命中缓存"
        assert (REPO_KEY, "grape_bot") not in ep._bot_username_validation_cache

    def test_cache_ttl_60s(self, monkeypatch):
        calls = _patch_platform(monkeypatch, data={"login": "grape_bot", "type": "User"})
        clock = {"now": 1000.0}
        monkeypatch.setattr(ep.time, "time", lambda: clock["now"])

        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")
        assert calls["user"] == 1

        clock["now"] += 30
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")
        assert calls["user"] == 1, "60s 内命中缓存，不应再次打平台"

        clock["now"] += 31
        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")
        assert calls["user"] == 2, "TTL 过期后应重新校验"


# ─────────────────────────── 注入 + 可观测 ───────────────────────────

class TestInjectAndObservability:
    def test_invalid_username_injects_empty_and_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(ep, "_resolve_bot_username_info",
                            lambda _rk: ("my_grape_bot", "global"))
        monkeypatch.setattr(ep, "_validate_bot_username",
                            lambda _rk, _u: (False, "user_not_found"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        env = {}
        with caplog.at_level(logging.WARNING):
            _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME"] == ""
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("my_grape_bot" in m and "user_not_found" in m for m in messages), messages

    def test_invalid_username_contract_degrades_to_empty(self, monkeypatch):
        """无效候选值 → 注入空串（issue-cli 对空值降级，不再发起 assignee update）。

        复审：不再用本地复刻的下游实现做自证式断言，只断言注入契约本身——
        空 BOT_USERNAME 即下游技能文档化的降级输入。
        """
        monkeypatch.setattr(ep, "_resolve_bot_username_info",
                            lambda _rk: ("my_grape_bot", "global"))
        monkeypatch.setattr(ep, "_validate_bot_username",
                            lambda _rk, _u: (False, "user_not_found"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        env = {}
        source = _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME"] == ""
        assert env["BOT_USERNAME_SOURCE"] == "global"
        assert source == "global"

    def test_valid_username_injected_with_confirmed_value(self, monkeypatch):
        monkeypatch.setattr(ep, "_resolve_bot_username_info",
                            lambda _rk: ("grape_bot", "global"))
        monkeypatch.setattr(ep, "_validate_bot_username",
                            lambda _rk, _u: (True, "ok"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        env = {}
        _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME"] == "grape_bot"
        assert env["BOT_USERNAME_SOURCE"] == "global"

    @pytest.mark.parametrize("source", ["managed_repo", "global"])
    def test_source_env_recorded_without_agent_logs(self, monkeypatch, source):
        """复审 Spec③：worker/autopilot 注入点不传 agent_logs，也须能从 env 快照查到来源。"""
        monkeypatch.setattr(ep, "_resolve_bot_username_info",
                            lambda _rk: ("grape_bot", source))
        monkeypatch.setattr(ep, "_validate_bot_username", lambda _rk, _u: (True, "ok"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        env = {}
        _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME_SOURCE"] == source

    @pytest.mark.parametrize("source,expected", [
        ("managed_repo", "managed_repo"),
        ("global", "global"),
        ("none", "none"),
    ])
    def test_agent_logs_records_bot_username_source(self, monkeypatch, source, expected):
        if source == "none":
            monkeypatch.setattr(ep, "_resolve_bot_username_info", lambda _rk: ("", "none"))
        else:
            monkeypatch.setattr(ep, "_resolve_bot_username_info",
                                lambda _rk: ("grape_bot", source))
            monkeypatch.setattr(ep, "_validate_bot_username", lambda _rk, _u: (True, "ok"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        logs = []
        _inject_bot_agent_env({}, REPO_KEY, "issue", agent_logs=logs)
        assert any(f"BOT_USERNAME_SOURCE={expected}" in (e.get("msg") or "") for e in logs), logs

    def test_explicit_env_not_overridden_and_logged(self, monkeypatch):
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")
        logs = []
        env = {"BOT_USERNAME": "manual_bot"}
        _inject_bot_agent_env(env, REPO_KEY, "issue", agent_logs=logs)
        assert env["BOT_USERNAME"] == "manual_bot"
        assert env["BOT_USERNAME_SOURCE"] == "explicit"
        assert any("BOT_USERNAME_SOURCE=explicit" in (e.get("msg") or "") for e in logs), logs

    def test_empty_explicit_env_not_marked_explicit(self, monkeypatch):
        """复审：复用 env 里残留的空 BOT_USERNAME 应为 none，而非 explicit。"""
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")
        env = {"BOT_USERNAME": ""}
        source = _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME"] == ""
        assert env["BOT_USERNAME_SOURCE"] == "none"
        assert source == "none"

    def test_stale_source_refreshed(self, monkeypatch):
        """复审：env 残留旧来源时按本次结果刷新（不再 setdefault 保留旧值）。"""
        monkeypatch.setattr(ep, "_resolve_bot_username_info",
                            lambda _rk: ("grape_bot", "global"))
        monkeypatch.setattr(ep, "_validate_bot_username", lambda _rk, _u: (True, "ok"))
        monkeypatch.setattr(ep, "get_agent_display_name", lambda _at: "Issue Agent")

        env = {"BOT_USERNAME_SOURCE": "none"}
        _inject_bot_agent_env(env, REPO_KEY, "issue")
        assert env["BOT_USERNAME_SOURCE"] == "global"

    def test_cache_prunes_expired_entries(self, monkeypatch):
        """复审：过期缓存项需回删，避免 repo×username 组合无限增长。"""
        calls = _patch_platform(monkeypatch, data={"login": "grape_bot", "type": "User"})
        clock = {"now": 1000.0}
        monkeypatch.setattr(ep.time, "time", lambda: clock["now"])

        assert _validate_bot_username(REPO_KEY, "grape_bot") == (True, "ok")
        assert len(ep._bot_username_validation_cache) == 1

        clock["now"] += 61
        assert _validate_bot_username("gitcode:octo/other", "grape_bot") == (True, "ok")
        assert calls["user"] == 2
        assert (REPO_KEY, "grape_bot") not in ep._bot_username_validation_cache
