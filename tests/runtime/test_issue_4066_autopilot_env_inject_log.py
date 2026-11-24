"""Issue #4066：autopilot 执行侧注入日志收敛 + 白名单变量注入生效。

契约：
1. 一次执行只输出**一条** info 汇总（`injected=N skipped=M names=...`），不再逐条 WARNING；
2. 白名单变量（`GRAPE_ANALYSIS_MODE` / `GRAPE_ORG_ID` / `GRAPE_MANAGED_BY`）进入 env_extra；
3. 敏感/非法名只进 skipped，env 不变，且无逐条 WARNING；
4. 平台已注入的键不被 custom_fields 覆盖（平台优先，含场景注入路径）。
"""
import json
import logging

import pytest

from src.domains.runtime.engine import scheduler

REPO_KEY = "gitcode:octo/issue4066"
REPOSITORY = "octo/issue4066"
EXEC_ID = 994066


def _patch_autopilot_deps(monkeypatch, tmp_path, *, server_env=None):
    """屏蔽引擎/工作区/凭据依赖，捕获分流函数收到的 env_extra。"""
    import src.domains.runtime.engine.service as service

    captured = {}
    monkeypatch.setattr(service, "runtime_execution_kind", lambda _id: "direct")
    monkeypatch.setattr(scheduler, "_build_autopilot_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr("src.domains.runtime.engine.engine_select.pick_engine", lambda *a, **k: 1)
    monkeypatch.setattr("src.domains.runtime.engine.engine_select.engine_name", lambda _id: "engine-1")
    monkeypatch.setattr("src.domains.runtime.engine.instances.runtime_instance_name", lambda _id: "inst-1")
    monkeypatch.setattr("src.api.event_pipeline.get_agent_workdir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr("src.api.event_pipeline.get_agent_display_name", lambda _at: "Issue Agent")
    monkeypatch.setattr("src.api.event_pipeline._resolve_bot_username", lambda _rk: "grape_bot")
    monkeypatch.setattr("src.api.event_pipeline._materialize_direct_workdir", lambda *a, **k: "prompt")
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_task_server_env",
                        lambda *a, **k: dict(server_env or {}))
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: "")

    def _capture(*args, **kwargs):
        captured["env_extra"] = args[12]

    monkeypatch.setattr(scheduler, "_execute_autopilot_direct", _capture)
    monkeypatch.setattr(scheduler, "_execute_autopilot_remote", _capture)
    return captured


def _run(monkeypatch, tmp_path, custom_fields, *, server_env=None):
    captured = _patch_autopilot_deps(monkeypatch, tmp_path, server_env=server_env)
    task = {"id": 4066, "skill_name": "some-skill",
            "custom_fields": json.dumps(custom_fields, ensure_ascii=False)}
    scheduler._execute_autopilot_agent(REPO_KEY, task, EXEC_ID)
    return captured


def _summary_lines(caplog):
    return [r for r in caplog.records
            if "custom env injected=" in r.getMessage() and r.levelno == logging.INFO]


def test_whitelisted_fields_injected_and_single_info_summary(monkeypatch, tmp_path, caplog):
    fields = [
        {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
        {"name": "GRAPE_ORG_ID", "value": "42"},
        {"name": "GRAPE_MANAGED_BY", "value": "org-analysis"},
        {"name": "TEAM_FLAG", "value": "on"},
    ]
    with caplog.at_level(logging.INFO):
        captured = _run(monkeypatch, tmp_path, fields)

    env = captured["env_extra"]
    assert env["GRAPE_ANALYSIS_MODE"] == "all"
    assert env["GRAPE_ORG_ID"] == "42"
    assert env["GRAPE_MANAGED_BY"] == "org-analysis"
    assert env["TEAM_FLAG"] == "on"

    lines = _summary_lines(caplog)
    assert len(lines) == 1, f"注入日志必须单条汇总，实际 {len(lines)} 条"
    msg = lines[0].getMessage()
    assert "injected=4" in msg and "skipped=0" in msg, msg


def test_rejected_fields_only_in_summary_no_per_item_warning(monkeypatch, tmp_path, caplog):
    fields = [
        {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
        {"name": "GRAPE_API_TOKEN", "value": "secret-value"},
        {"name": "GRAPE_SESSION_TOKEN", "value": "tk_x"},
        {"name": "GRAPE_WORK_BRANCH", "value": "dev"},
        {"name": "team_flag", "value": "lower"},
        {"name": "", "value": "empty"},
    ]
    with caplog.at_level(logging.INFO):
        captured = _run(monkeypatch, tmp_path, fields)

    env = captured["env_extra"]
    assert env["GRAPE_ANALYSIS_MODE"] == "all"
    for bad in ("GRAPE_API_TOKEN", "GRAPE_SESSION_TOKEN", "GRAPE_WORK_BRANCH", "team_flag"):
        assert bad not in env
    # 值不外泄
    assert "secret-value" not in json.dumps(env, ensure_ascii=False)

    lines = _summary_lines(caplog)
    assert len(lines) == 1, f"注入日志必须单条汇总，实际 {len(lines)} 条"
    msg = lines[0].getMessage()
    assert "injected=1" in msg and "skipped=5" in msg, msg
    assert "GRAPE_API_TOKEN" in msg and "GRAPE_WORK_BRANCH" in msg, "汇总需保留 skipped 变量名"

    rejected_warnings = [r for r in caplog.records
                         if r.levelno >= logging.WARNING and "custom env" in r.getMessage()]
    assert rejected_warnings == [], \
        f"不应再有逐条 rejected WARNING: {[r.getMessage() for r in rejected_warnings]}"


def test_platform_injected_key_not_overridden(monkeypatch, tmp_path):
    """平台已注入的键优先：custom_fields 不得改写 GRAPE_ORG_ID。"""
    fields = [
        {"name": "GRAPE_ORG_ID", "value": "attacker-org"},
        {"name": "GRAPE_ANALYSIS_MODE", "value": "single"},
    ]
    captured = _run(monkeypatch, tmp_path, fields,
                    server_env={"GRAPE_ORG_ID": "platform-org"})

    env = captured["env_extra"]
    assert env["GRAPE_ORG_ID"] == "platform-org"
    assert env["GRAPE_ANALYSIS_MODE"] == "single"


def test_empty_custom_fields_emits_no_summary(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        captured = _run(monkeypatch, tmp_path, [])
    assert _summary_lines(caplog) == []
    assert "GRAPE_ANALYSIS_MODE" not in captured["env_extra"]


def test_scene_env_platform_key_not_overridden(monkeypatch):
    """场景注入路径同口径：平台上下文（GRAPE_ORG_ID）不被场景 custom_fields 覆盖。"""
    import src.api.pipeline_config as pc
    import src.api.session_store as store
    from src.domains.runtime.pipeline import event_pipeline as ep

    monkeypatch.setattr(ep, "_resolve_scene_id", lambda *a, **k: "scene-x", raising=False)
    monkeypatch.setattr(pc, "scene_is_org_scoped", lambda _sid: False)
    monkeypatch.setattr(store, "get_scene_advanced_options", lambda *a, **k: {
        "custom_fields": [
            {"name": "GRAPE_ORG_ID", "value": "attacker-org"},
            {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
            {"name": "GRAPE_SESSION_TOKEN", "value": "tk_x"},
        ],
    })

    env = {"GRAPE_ORG_ID": "platform-org"}
    ep._inject_scene_env(env, REPO_KEY, "4066", "coding")
    assert env["GRAPE_ORG_ID"] == "platform-org"
    assert env["GRAPE_ANALYSIS_MODE"] == "all"
    assert "GRAPE_SESSION_TOKEN" not in env