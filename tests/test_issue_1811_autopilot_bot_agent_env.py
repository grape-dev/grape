"""Issue #1811: Autopilot 定时调度链路注入 BOT_USERNAME / AGENT_NAME。

根因：`_execute_autopilot_agent` 组装的 env_extra 只有 REPO_* / ACCOUNT /
custom_fields / GRAPE_WORK_BRANCH，未注入 bot 身份，导致依赖评论作者的
幂等查重退化为形态判定；且 BOT_USERNAME 属于保留变量，custom_fields 无法兜底。

验收：
1. DIRECT / REMOTE 两条分支的 env_extra 均含非空 BOT_USERNAME + AGENT_NAME。
2. custom_fields 中的 BOT_USERNAME / AGENT_NAME 被保留集拒绝，最终值来自平台。
3. 多 bot 场景取值与 event_pipeline 一致：repo 级优先，缺失回落全局 bot。
4. BOT_USERNAME 解析为空时输出含 exec id + repo 的 WARNING。
"""
import json
import logging

import pytest

from src.runtime import scheduler

REPO_KEY = "gitcode:octo/issue1811"
REPOSITORY = "octo/issue1811"
EXEC_ID = 998181


def _patch_autopilot_deps(monkeypatch, tmp_path, mode, *, bot="grape_bot",
                          display_name="Issue Agent"):
    """屏蔽引擎/工作区/凭据依赖，捕获分流函数收到的 env_extra。"""
    import src.runtime.service as service

    captured = {}
    monkeypatch.setattr(service, "runtime_execution_kind", lambda _id: mode)
    monkeypatch.setattr(scheduler, "_build_autopilot_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr("src.runtime.engine_select.pick_engine", lambda *a, **k: 1)
    monkeypatch.setattr("src.runtime.engine_select.engine_name", lambda _id: "engine-1")
    monkeypatch.setattr("src.runtime.instances.runtime_instance_name", lambda _id: "inst-1")
    monkeypatch.setattr("src.api.event_pipeline.get_agent_workdir",
                        lambda *a, **k: str(tmp_path))
    monkeypatch.setattr("src.api.event_pipeline.get_agent_display_name",
                        lambda _at: display_name)
    if bot is not None:
        monkeypatch.setattr("src.api.event_pipeline._resolve_bot_username",
                            lambda _rk: bot)
    monkeypatch.setattr("src.api.event_pipeline._materialize_direct_workdir",
                        lambda *a, **k: "prompt")
    monkeypatch.setattr("src.prompt_builder.resolve_task_server_env",
                        lambda *a, **k: {})
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")

    def _capture(*args, **kwargs):
        captured["env_extra"] = args[12]
        captured["agent_display_name"] = args[11]
        captured["task"] = args[1]

    monkeypatch.setattr(scheduler, "_execute_autopilot_direct", _capture)
    monkeypatch.setattr(scheduler, "_execute_autopilot_remote", _capture)
    return captured


def _run_autopilot(monkeypatch, tmp_path, mode, *, task=None, bot="grape_bot",
                   display_name="Issue Agent"):
    captured = _patch_autopilot_deps(
        monkeypatch, tmp_path, mode, bot=bot, display_name=display_name)
    scheduler._execute_autopilot_agent(
        REPO_KEY, task or {"id": 1811, "skill_name": "some-skill"}, EXEC_ID)
    return captured


@pytest.mark.parametrize("mode", ["direct", "queue_remote"])
def test_direct_and_remote_branches_inject_bot_and_agent(monkeypatch, tmp_path, mode):
    """单点注入覆盖 DIRECT 与 REMOTE：两分支 env_extra 均有非空两变量。"""
    captured = _run_autopilot(monkeypatch, tmp_path, mode)

    env = captured["env_extra"]
    assert env["BOT_USERNAME"] == "grape_bot"
    assert env["AGENT_NAME"] == "Issue Agent"
    # AGENT_NAME 与传给分流函数的 agent_display_name 同源
    assert captured["agent_display_name"] == env["AGENT_NAME"]


def test_custom_fields_cannot_override_reserved_bot_vars(monkeypatch, tmp_path):
    """custom_fields 写 BOT_USERNAME/AGENT_NAME 仍被保留集拒绝；普通变量正常注入。"""
    task = {
        "id": 1811,
        "skill_name": "some-skill",
        "custom_fields": json.dumps([
            {"name": "BOT_USERNAME", "value": "attacker"},
            {"name": "AGENT_NAME", "value": "fake-agent"},
            {"name": "CUSTOM_FLAG", "value": "on"},
        ]),
    }
    captured = _run_autopilot(monkeypatch, tmp_path, "direct", task=task)

    env = captured["env_extra"]
    assert env["BOT_USERNAME"] == "grape_bot"
    assert env["AGENT_NAME"] == "Issue Agent"
    assert env["CUSTOM_FLAG"] == "on"


def test_repo_level_bot_username_wins(monkeypatch, tmp_path):
    """多 bot 场景：repo 级 managed_repos.bot_username 优先于全局 bot。"""
    import src.user.db as udb
    from src.session_manager import db, db_lock

    monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: ["global_bot"])
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        db.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, "
            "bot_username, github_webhook_token, bot_github_token) "
            "VALUES (?,'octo','issue1811','octo/issue1811',"
            "0,'','','','repo_bot','','')", (REPO_KEY,))
        db.commit()
    try:
        captured = _run_autopilot(monkeypatch, tmp_path, "direct", bot=None)
        # bot=None 时不覆盖 _resolve_bot_username，这里需要还原真实解析
        assert captured["env_extra"]["BOT_USERNAME"] == "repo_bot"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            db.commit()


def test_global_bot_fallback_when_repo_level_missing(monkeypatch, tmp_path):
    """repo 级缺失时回落全局 bot（gitcode 取第一个），与 event_pipeline 一致。"""
    import src.user.db as udb
    from src.session_manager import db, db_lock

    monkeypatch.setattr(udb, "get_global_bot_usernames", lambda: ["global_bot", "other"])
    monkeypatch.setattr(udb, "get_global_github_bot_username", lambda: "gh_bot")
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        db.commit()
    captured = _run_autopilot(monkeypatch, tmp_path, "direct", bot=None)
    assert captured["env_extra"]["BOT_USERNAME"] == "global_bot"


def test_empty_bot_username_emits_warning(monkeypatch, tmp_path, caplog):
    """BOT_USERNAME 解析为空 → 显式 WARNING，含 exec id + repo，不静默退化。"""
    with caplog.at_level(logging.WARNING):
        captured = _run_autopilot(monkeypatch, tmp_path, "direct", bot="")

    assert captured["env_extra"]["BOT_USERNAME"] == ""
    messages = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any(str(EXEC_ID) in m and REPOSITORY in m for m in messages), messages