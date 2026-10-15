# -*- coding: utf-8 -*-
"""多场景 PR 订阅 — 人类用户主路由补派剩余场景（#318 回归修复）全拟真验证。

设计文档: docs/internal-review/pr-subscription-multiscene-mainroute-design.md
不 mock 派发函数，事件真实落库；只挡 pick_engine（无引擎时事件标 failed，不影响计数）。

T1 人类开 PR 双场景 → 2 条(A主 + B a2a)，无重复
T2 人类 PR update 带新 commit → 双场景都触发
T3 bot 开 PR → 2 条 a2a_event，主路由无事件（#318 不回归）
T4 单场景人类 PR → 恰 1 条 pull_request_opened，a2a_event=0（双重触发禁令）
T5 同 skill 双场景 → 1 条（_filter 去重设计行为）
T6 #318 原有 5 用例（另文件）不回归
"""
import pytest
import json
import uuid
from unittest.mock import patch

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/simms-repo"
_BOT_TOKEN = "simms-bot-token"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped", "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:XOcto/simms-repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-simms-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%simms-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'simms-%'")
        db.commit()


def _seed(scenes):
    """scenes: [(scene_id, qa_skill), ...] 全部订阅 work_branch=branch-1"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'simms-repo', 'XOcto/simms-repo', 0, 'default', ?, 'wt-simms', 'grape_bot')",
            (_REPO, _BOT_TOKEN))
        for scene_id, qa_skill in scenes:
            nodes = [{"tag": "qa", "skill": qa_skill, "enabled": True}]
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
                "VALUES (?,?,?,?,1,?,?)",
                (scene_id, scene_id, _REPO, json.dumps(nodes), 1,
                 json.dumps({"work_branch": "branch-1"})))
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("hsimms" + uuid.uuid4().hex, "simms-cred", "simms-dev"))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, ?, ?, 'idle', 1, ?)",
            ("sess-simms-qa", _REPO, "qa", rid, _BOT_TOKEN))
        db.commit()


def _run_hook(actor, action="open", oldrev=None):
    import src.api.event_pipeline as ep
    attrs = {"action": action, "target_branch": "branch-1", "iid": 88,
             "title": "simms", "source_branch": "dev"}
    if oldrev:
        attrs["oldrev"] = oldrev
    raw = json.dumps({
        "object_attributes": attrs,
        "project": {"path_with_namespace": "XOcto/simms-repo"},
        "user": {"username": actor},
        "commits": ([{"id": "c1", "message": "new"}] if oldrev else []),
    }).encode()
    with patch("src.runtime.engine_select.pick_engine", return_value=None), \
         patch("src.user.db.get_agent_bot_token", return_value=""), \
         patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        ep.run_pipeline(raw, {"X-GitCode-Event": "Merge Request Hook"},
                        account="XOcto/simms-repo")


def _events():
    with db_lock:
        return db.cursor().execute(
            "SELECT event_type, agent_type, scene_id, issue_id FROM events WHERE repo_key=? ORDER BY seq",
            (_REPO,)).fetchall()


class TestMultiScene:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_T1_human_pr_two_scenes(self):
        _seed([("scene-A", "qa-cli"), ("scene-B", "pr-reviewer")])
        _run_hook("songspace", "open")
        rows = _events()
        print(f"\n[T1] 人类PR双场景 → {len(rows)} 条: {rows}")
        assert len(rows) == 2, f"应 2 条, 实际 {rows}"
        scenes = sorted(r[2] for r in rows)
        assert scenes == ["scene-A", "scene-B"]
        types = sorted(r[0] for r in rows)
        assert "pull_request_opened" in types and "a2a_event" in types

    def test_T2_human_pr_update_with_commit(self):
        _seed([("scene-A", "qa-cli"), ("scene-B", "pr-reviewer")])
        _run_hook("songspace", "update", oldrev="abc123")
        rows = _events()
        print(f"\n[T2] 人类PR update+commit → {len(rows)} 条: {rows}")
        assert len(rows) == 2, f"应 2 条, 实际 {rows}"
        scenes = sorted(r[2] for r in rows)
        assert scenes == ["scene-A", "scene-B"]

    def test_T3_bot_pr_no_main_route(self):
        _seed([("scene-A", "qa-cli"), ("scene-B", "pr-reviewer")])
        _run_hook("grape_bot", "open")
        rows = _events()
        print(f"\n[T3] bot PR → {len(rows)} 条: {rows}")
        assert len(rows) == 2, f"应 2 条 a2a, 实际 {rows}"
        assert all(r[0] == "a2a_event" for r in rows), "bot 只应有 a2a_event(1.5预派发)"
        scenes = sorted(r[2] for r in rows)
        assert scenes == ["scene-A", "scene-B"]

    def test_T4_single_scene_no_double(self):
        _seed([("scene-A", "qa-cli")])
        _run_hook("songspace", "open")
        rows = _events()
        a2a = [r for r in rows if r[0] == "a2a_event"]
        print(f"\n[T4] 单场景人类PR → {len(rows)} 条: {rows}")
        assert len(rows) == 1, f"应恰 1 条, 实际 {rows}"
        assert rows[0][0] == "pull_request_opened", f"应为 pull_request_opened, 实际 {rows[0][0]}"
        assert len(a2a) == 0, f"双重触发禁令违反: 出现 {len(a2a)} 条 a2a_event"

    def test_T5_same_skill_dedup(self):
        _seed([("scene-A", "qa-cli"), ("scene-B", "qa-cli")])
        _run_hook("songspace", "open")
        rows = _events()
        print(f"\n[T5] 同skill双场景 → {len(rows)} 条: {rows}")
        assert len(rows) == 1, f"同 skill 去重应 1 条, 实际 {rows}"

    def test_acceptance_three_scenes(self):
        """验收标准2: 三场景(A/B/C 不同skill) 人类PR → 恰 3 条, scene 集合={A,B,C}"""
        _seed([("scene-A", "qa-cli"), ("scene-B", "pr-reviewer"),
               ("scene-C", "openspec-reviewer")])
        _run_hook("songspace", "open")
        rows = _events()
        print(f"\n[验收] 三场景人类PR → {len(rows)} 条: {rows}")
        assert len(rows) == 3
        assert sorted(r[2] for r in rows) == ["scene-A", "scene-B", "scene-C"]
        assert sum(1 for r in rows if r[0] == "pull_request_opened") == 1
        assert sum(1 for r in rows if r[0] == "a2a_event") == 2

    def test_acceptance_single_scene_lockdown(self):
        """验收标准3: 单场景 → 恰 1 条 pull_request_opened, a2a_event=0"""
        _seed([("scene-A", "qa-cli")])
        _run_hook("songspace", "open")
        _run_hook("songspace", "update", oldrev="def456")  # 连续两种事件形态都验
        rows = _events()
        a2a = [r for r in rows if r[0] == "a2a_event"]
        print(f"\n[验收] 单场景(open+update) → {len(rows)} 条: {rows}")
        assert len(rows) == 2, "open + update 各 1 条主事件"
        assert all(r[0] != "a2a_event" for r in rows), f"a2a_event 数应为 0, 实际 {len(a2a)}"
