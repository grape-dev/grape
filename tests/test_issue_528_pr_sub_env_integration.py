"""Issue #528 集成回归：PR 订阅事件全链路 → agent_tasks.env_extra 注入校验

走 run_pipeline（webhook 入口）→ 事件创建 → 任务入队，断言最终落入
agent_tasks.env_extra 的 ISSUE_NUMBER/PR_NUMBER：
1. 无关联 Issue 的 PR 订阅事件 → 无非数字 ISSUE_NUMBER，PR_NUMBER 为纯数字。
2. 关联 Issue 的 PR 事件（issue_id 解析为真实编号）→ ISSUE_NUMBER 为真实数字（不回归）。
"""
import json
import uuid
from unittest.mock import patch

import pytest

from src.session_manager import db, db_lock

_TEST_REPO = "gitcode:owner/repo"
_BOT_TOKEN = "test-bot-token-528"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR id LIKE 'sess-528-%'")
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%528-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE '528-%'")
        db.commit()


def _seed_managed_repo():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'owner', 'repo', 'owner/repo', 0, 'default', ?, 'wt-528', 'grape_bot')",
            (_TEST_REPO, _BOT_TOKEN))
        db.commit()


def _seed_scene(scene_id, work_branch="mys-local"):
    nodes = [{"tag": "qa", "skill": "qa-cli", "enabled": True}]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,1,?)",
            (scene_id, scene_id, _TEST_REPO, json.dumps(nodes),
             json.dumps({"work_branch": work_branch})))
        db.commit()


def _seed_qa_session():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("h528" + uuid.uuid4().hex, "528-cred", "528-dev"))
        _cid = c.lastrowid
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                  "VALUES ('opencode-528','opencode-528',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
        _rid = c.lastrowid
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
                  "VALUES (?, ?, ?, ?, 'idle', 1, ?)",
                  ("sess-528-qa", _TEST_REPO, "qa", _rid, _BOT_TOKEN))
        db.commit()


def _make_pr_opened_raw(actor_username, pr_num=257, target_branch="mys-local", title="feature update"):
    return json.dumps({
        "object_attributes": {
            "action": "open",
            "target_branch": target_branch,
            "iid": pr_num,
            "title": title,
        },
        "project": {"path_with_namespace": "owner/repo"},
        "user": {"username": actor_username},
    }).encode()


def _run_hook(actor, pr_num=257, title="feature update", resolve_pr_issue=None):
    """走 run_pipeline 主路由 → _enqueue_pipeline_task，捕获 enqueue_task 的 env_extra。"""
    import src.api.event_pipeline as ep
    captured = []

    def _fake_enqueue(**kwargs):
        captured.append(kwargs.get("env_extra") or {})
        return "t_" + uuid.uuid4().hex[:8]

    raw = _make_pr_opened_raw(actor, pr_num=pr_num, title=title)
    _patches = [
        patch("src.runtime.engine_select.pick_engine", return_value=0),
        patch("src.runtime.engine_select.engine_name", return_value="opencode"),
        patch("src.runtime.engine_select.get_scene_pool_engine_ids", return_value=""),
        patch("src.api.event_pipeline.is_plugin_mode", return_value=True),
        patch("src.runtime.service.get_execution_mode", return_value="remote"),
        patch("src.runtime.service.enqueue_task", side_effect=_fake_enqueue),
        patch("src.user.db.get_agent_bot_token", return_value=""),
        patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]),
    ]
    if resolve_pr_issue is not None:
        _patches.append(patch("src.api.event_pipeline.resolve_pr_issue_id", return_value=resolve_pr_issue))
    raw = _make_pr_opened_raw(actor, pr_num=pr_num, title=title)
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _patches:
            stack.enter_context(p)
        ep.run_pipeline(raw, {"X-GitCode-Event": "Merge Request Hook"},
                        account="owner/repo")
    return captured


def _task_envs():
    with db_lock:
        rows = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE repo_key=? AND agent_type='qa'",
            (_TEST_REPO,)).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


class TestPRSubEnvIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        _seed_managed_repo()
        _seed_scene("scene-528")
        _seed_qa_session()
        yield
        _clean()

    def test_pr_sub_without_linked_issue_env(self):
        """无关联 Issue 的 PR 订阅事件：env 无非数字 ISSUE_NUMBER，PR_NUMBER 纯数字"""
        envs = _run_hook("human_user", pr_num=257)
        assert envs, "应产生 qa 任务（PR 订阅派发）"
        for env in envs:
            issue_num = env.get("ISSUE_NUMBER", "")
            assert issue_num == "" or issue_num.isdigit(), \
                f"ISSUE_NUMBER 不得为非数字占位值: {issue_num!r}"
            assert "GRAPE_ISSUE_ID" not in env or env["GRAPE_ISSUE_ID"] == ""
            pr_num = env.get("PR_NUMBER", "")
            assert pr_num == "257", f"PR_NUMBER 应为纯数字 PR 号 257, 实际 {pr_num!r}"

    def test_pr_with_linked_issue_env_not_regressed(self):
        """关联 Issue 的 PR 事件（resolve_pr_issue_id 解析出真实编号）：ISSUE_NUMBER 仍为数字"""
        envs = _run_hook("human_user", pr_num=258, resolve_pr_issue="#66")
        assert envs, "应产生 qa 任务"
        linked = [e for e in envs if e.get("ISSUE_NUMBER", "").isdigit()]
        assert linked, f"关联 Issue 场景应注入数字 ISSUE_NUMBER: {envs}"
        for env in linked:
            assert env["ISSUE_NUMBER"] == "66"
