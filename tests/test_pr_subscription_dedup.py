"""Issue #318: PR 订阅同一条 Merge Request Hook 双重派发去重测试

验证方案 B：1.5 预匹配仅 bot actor 时预派发，人类用户只走主路由单次派发。
"""
import json
import os
import uuid
from unittest.mock import patch

import pytest

from src.session_manager import db, db_lock

_TEST_REPO = "gitcode:owner/repo"
_BOT_TOKEN = "test-bot-token-318"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR id LIKE 'sess-318-%'")
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%318-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE '318-%'")
        db.commit()


def _seed_managed_repo():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'owner', 'repo', 'owner/repo', 0, 'default', ?, 'wt-318', 'grape_bot')",
            (_TEST_REPO, _BOT_TOKEN))
        db.commit()


def _seed_scene(scene_id, work_branch="mys-local"):
    nodes = [{"tag": "qa", "skill": "qa-cli", "enabled": True}]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,?,?)",
            (scene_id, scene_id, _TEST_REPO, json.dumps(nodes), 1,
             json.dumps({"work_branch": work_branch})))
        db.commit()


def _seed_qa_session():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("h318" + uuid.uuid4().hex, "318-cred", "318-dev"))
        _cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
        _rid = c.lastrowid
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, ?, ?, 'idle', 1, ?)",
            ("sess-318-qa", _TEST_REPO, "qa", _rid, _BOT_TOKEN))
        db.commit()


def _make_pr_opened_raw(actor_username, target_branch="mys-local", pr_num=42):
    return json.dumps({
        "object_attributes": {
            "action": "open",
            "target_branch": target_branch,
            "iid": pr_num,
            "title": "feature update",
        },
        "project": {"path_with_namespace": "owner/repo"},
        "user": {"username": actor_username},
    }).encode()


class TestPRSubscriptionDedup:
    """Issue #318: PR 订阅双重派发去重（方案 B）"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        _seed_managed_repo()
        _seed_scene("scene-318-a")
        _seed_qa_session()
        yield
        _clean()

    def test_non_bot_actor_no_early_dispatch(self):
        """人类用户 PR：1.5 预匹配不预派发"""
        import src.api.event_pipeline as ep
        raw = _make_pr_opened_raw("human_user", target_branch="mys-local")
        headers = {"X-GitCode-Event": "Merge Request Hook"}
        with patch("src.api.event_pipeline._dispatch_pr_subscription_extra") as mock_dispatch, \
             patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch("src.user.db.get_agent_bot_token", return_value=""), \
             patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, headers, account="owner/repo")
        assert mock_dispatch.call_count == 0, \
            "非 bot actor 不应走 1.5 预派发（应延迟到主路由单次派发）"

    def test_bot_actor_early_dispatch(self):
        """bot 用户 PR：1.5 预匹配仍预派发（不回归 f06732b14）"""
        import src.api.event_pipeline as ep
        raw = _make_pr_opened_raw("grape_bot", target_branch="mys-local")
        headers = {"X-GitCode-Event": "Merge Request Hook"}
        with patch("src.api.event_pipeline._dispatch_pr_subscription_extra") as mock_dispatch, \
             patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, headers, account="owner/repo")
        assert mock_dispatch.call_count >= 1, \
            "bot actor 应走 1.5 预派发（绕过自循环检测触发 QA）"

    def test_non_bot_actor_single_event(self):
        """人类用户 PR：仅产生一条事件（主路由单次派发，无双重）"""
        import src.api.event_pipeline as ep
        raw = _make_pr_opened_raw("human_user", target_branch="mys-local")
        headers = {"X-GitCode-Event": "Merge Request Hook"}
        with patch("src.api.event_pipeline._dispatch_pr_subscription_extra") as mock_dispatch, \
             patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch("src.user.db.get_agent_bot_token", return_value=""), \
             patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            count = db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND event_type='pull_request_opened' AND agent_type='qa'",
                (_TEST_REPO,)).fetchone()[0]
        assert count == 1, f"非 bot actor 应仅产生 1 条 PR 订阅事件，实际 {count}"
        assert mock_dispatch.call_count == 0

    def test_bot_actor_no_main_route_event(self):
        """bot 用户 PR：主路由不执行（自循环检测 return），无 pull_request_opened 主事件"""
        import src.api.event_pipeline as ep
        raw = _make_pr_opened_raw("grape_bot", target_branch="mys-local")
        headers = {"X-GitCode-Event": "Merge Request Hook"}
        with patch("src.api.event_pipeline._dispatch_pr_subscription_extra") as mock_dispatch, \
             patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            count = db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND event_type='pull_request_opened'",
                (_TEST_REPO,)).fetchone()[0]
        assert count == 0, f"bot actor 主路由不应创建事件，实际 {count}"
        assert mock_dispatch.call_count >= 1

    def test_non_bot_actor_pr_updated_with_commit(self):
        """人类用户 PR updated（有新 commit）：同样仅主路由单次派发"""
        import src.api.event_pipeline as ep
        raw = json.dumps({
            "object_attributes": {
                "action": "update",
                "target_branch": "mys-local",
                "iid": 42,
                "title": "feature update",
                "oldrev": "abc123",
            },
            "project": {"path_with_namespace": "owner/repo"},
            "user": {"username": "human_user"},
        }).encode()
        headers = {"X-GitCode-Event": "Merge Request Hook"}
        with patch("src.api.event_pipeline._dispatch_pr_subscription_extra") as mock_dispatch, \
             patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch("src.user.db.get_agent_bot_token", return_value=""), \
             patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, headers, account="owner/repo")
        assert mock_dispatch.call_count == 0, \
            "非 bot actor PR updated 也不应走 1.5 预派发"
