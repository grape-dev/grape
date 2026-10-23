# -*- coding: utf-8 -*-
"""Issue #426: PR 订阅场景在 PR 与 Issue 绑定关系时彻底失效 — 回归测试。

验证 PR 订阅与 stage matrix 路由并行共存（bound_issue 口径：绑定了 Issue 即过滤
同场景/同 skill 订阅场景——绑 Issue = 问题已被他人解决）：
T1 PR 关联 Issue（stage=coding）Issue 场景自身订阅 → 主路由 coding 事件，订阅不派发（不再双重审查）
T2 PR 关联 Issue（stage=qa）同场景订阅 → 同场景跳过（避免重复审查），无订阅事件
T3 PR 关联 Issue（stage=qa）异场景订阅（不同 skill）→ stage matrix QA + 订阅场景并行 QA
T4 PR 关联 Issue（stage=coding）bot actor → Issue 场景订阅预派发同样被过滤
T5 同 qa_skill 双场景绑定 PR → 订阅全部过滤（同 skill + 同场景双重命中）
T6 无绑定 PR 订阅不回归（对照 #427 验证要点）
T7 PR merged（绑定 Issue）→ 不派发订阅 QA（已合并审查无意义）
"""
import pytest
import json
import uuid
from unittest.mock import patch

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/bound-repo"
_BOT_TOKEN = "bound-bot-token"
_ISSUE_NUM = 426


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped", "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:XOcto/bound-repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-bound-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%bound-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'bound-%'")
        db.commit()


def _seed(scenes, stage=None, scene_of_issue=None):
    """scenes: [(scene_id, qa_skill) 或 (scene_id, qa_skill, tags), ...] 全部订阅 work_branch=branch-1
    （tags 形如 ["qa", "coding"] 用于场景含多节点，默认仅 qa）；
    stage 非空时建关联 Issue 的 issue_state 行（scene_of_issue=Issue 所在场景）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'bound-repo', 'XOcto/bound-repo', 0, 'default', ?, 'wt-bound', 'grape_bot')",
            (_REPO, _BOT_TOKEN))
        for entry in scenes:
            scene_id, qa_skill = entry[0], entry[1]
            tags = entry[2] if len(entry) > 2 else ["qa"]
            nodes = []
            for tag in tags:
                skill = qa_skill if tag == "qa" else f"{tag}-cli"
                nodes.append({"tag": tag, "id": tag, "skill": skill, "enabled": True})
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
                "VALUES (?,?,?,?,1,?,?)",
                (scene_id, scene_id, _REPO, json.dumps(nodes), 1,
                 json.dumps({"work_branch": "branch-1"})))
        if stage:
            c.execute(
                "INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent, scene_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (_REPO, f"#{_ISSUE_NUM}", stage, stage, scene_of_issue or ""))
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("hbound" + uuid.uuid4().hex, "bound-cred", "bound-dev"))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, 'qa', ?, 'idle', 1, ?)",
            ("sess-bound-qa", _REPO, rid, _BOT_TOKEN))
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, 'coding', ?, 'idle', 1, ?)",
            ("sess-bound-coding", _REPO, rid, _BOT_TOKEN))
        db.commit()


def _run_hook(actor, action="open", oldrev=None, bound=True, event="Merge Request Hook"):
    import src.api.event_pipeline as ep
    attrs = {"action": action, "target_branch": "branch-1", "iid": 77,
             "title": f"fix for #{_ISSUE_NUM}" if bound else "standalone change",
             "source_branch": "dev"}
    if oldrev:
        attrs["oldrev"] = oldrev
    raw = json.dumps({
        "object_attributes": attrs,
        "project": {"path_with_namespace": "XOcto/bound-repo"},
        "user": {"username": actor},
        "commits": ([{"id": "c1", "message": "new"}] if oldrev else []),
    }).encode()
    with patch("src.runtime.engine_select.pick_engine", return_value=None), \
         patch("src.user.db.get_agent_bot_token", return_value=""), \
         patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        ep.run_pipeline(raw, {"X-GitCode-Event": event}, account="XOcto/bound-repo")


def _events():
    with db_lock:
        return db.cursor().execute(
            "SELECT event_type, agent_type, scene_id, issue_id FROM events WHERE repo_key=? ORDER BY seq",
            (_REPO,)).fetchall()


class TestPRSubIssueBinding:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_T1_bound_issue_stage_coding_parallel_dispatch(self):
        """PR 关联 Issue（stage=coding）：Issue 场景自身订阅不派发（避免主路由+订阅双重审查）"""
        _seed([("scene-A", "qa-cli", ["qa", "coding"])], stage="coding", scene_of_issue="scene-A")
        _run_hook("human_user", "open")
        rows = _events()
        print(f"\n[T1] 绑定 Issue stage=coding → {rows}")
        main = [r for r in rows if r[0] == "pull_request_opened"]
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert len(main) == 1 and main[0][1] == "coding", f"stage matrix 主路由应为 coding, 实际 {rows}"
        assert len(extra) == 0, f"绑定 Issue 时 Issue 场景订阅不应再派发, 实际 {rows}"

    def test_T2_bound_issue_stage_qa_same_scene_skipped(self):
        """PR 关联 Issue（stage=qa）且同场景订阅：跳过同场景，无重复 QA"""
        _seed([("scene-A", "qa-cli")], stage="qa", scene_of_issue="scene-A")
        _run_hook("human_user", "open")
        rows = _events()
        print(f"\n[T2] 绑定 Issue stage=qa 同场景 → {rows}")
        assert len(rows) == 1 and rows[0][0] == "pull_request_opened" and rows[0][1] == "qa", \
            f"应仅 stage matrix QA 主事件, 实际 {rows}"
        assert not any(r[0] == "a2a_event" for r in rows), f"同场景订阅不应重复派发: {rows}"

    def test_T3_bound_issue_stage_qa_other_scene_dispatched(self):
        """PR 关联 Issue（stage=qa）异场景订阅（不同 skill）：stage matrix QA + 订阅并行 QA"""
        _seed([("scene-A", "qa-cli"), ("scene-B", "pr-reviewer")],
              stage="qa", scene_of_issue="scene-A")
        _run_hook("human_user", "open")
        rows = _events()
        print(f"\n[T3] 绑定 Issue stage=qa 异场景 → {rows}")
        main = [r for r in rows if r[0] == "pull_request_opened"]
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert len(main) == 1 and main[0][1] == "qa" and main[0][2] == "scene-A", \
            f"stage matrix 主事件应为 scene-A qa, 实际 {rows}"
        assert len(extra) == 1 and extra[0][2] == "scene-B", f"异场景订阅应并行派发, 实际 {rows}"

    def test_T4_bound_issue_bot_actor_early_dispatch(self):
        """PR 关联 Issue + bot actor：Issue 场景订阅预派发同样被过滤（bound_issue 口径一致）"""
        _seed([("scene-A", "qa-cli")], stage="coding", scene_of_issue="scene-A")
        _run_hook("grape_bot", "open")
        rows = _events()
        print(f"\n[T4] 绑定 Issue bot actor → {rows}")
        assert len(rows) == 0, f"绑定 Issue 时 bot 预派发应被过滤, 实际 {rows}"

    def test_T5_same_skill_dedup_kept(self):
        """同 qa_skill 双场景 + 绑定 Issue：订阅全部过滤（同场景 + 同 skill 双重命中）"""
        _seed([("scene-A", "qa-cli"), ("scene-B", "qa-cli")],
              stage="coding", scene_of_issue="scene-A")
        _run_hook("human_user", "open")
        rows = _events()
        print(f"\n[T5] 同 skill 双场景绑定 → {rows}")
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert len(extra) == 0, f"绑定 Issue 时同 skill 场景订阅应全部过滤, 实际 {rows}"

    def test_T6_unbound_pr_subscription_no_regression(self):
        """无绑定 PR 订阅：主路由单次派发（不回归）"""
        _seed([("scene-A", "qa-cli")])
        _run_hook("human_user", "open", bound=False)
        rows = _events()
        print(f"\n[T6] 无绑定 PR → {rows}")
        assert len(rows) == 1 and rows[0][0] == "pull_request_opened" and rows[0][1] == "qa", \
            f"无绑定应仅 1 条主事件, 实际 {rows}"

    def test_T7_bound_merged_pr_no_subscription(self):
        """PR merged（绑定 Issue）：不派发订阅 QA（已合并审查无意义）"""
        _seed([("scene-A", "qa-cli")], stage="coding", scene_of_issue="scene-A")
        _run_hook("human_user", action="merge", event="Merge Request Hook")
        rows = _events()
        print(f"\n[T7] merged 绑定 PR → {rows}")
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert len(extra) == 0, f"merged PR 不应派发订阅 QA, 实际 {rows}"

    def test_T8_bound_issue_stage_qa_same_skill_other_scene_skipped(self):
        """PR 关联 Issue（stage=qa）异场景同 skill 订阅：订阅不再重复派发（bound_issue 口径）"""
        _seed([("scene-A", "qa-cli"), ("scene-B", "qa-cli")],
              stage="qa", scene_of_issue="scene-A")
        _run_hook("human_user", "open")
        rows = _events()
        print(f"\n[T8] 绑定 Issue stage=qa 异场景同 skill → {rows}")
        main = [r for r in rows if r[0] == "pull_request_opened"]
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert len(main) == 1 and main[0][1] == "qa" and main[0][2] == "scene-A", \
            f"stage matrix 主事件应为 scene-A qa, 实际 {rows}"
        assert len(extra) == 0, f"同 skill 异场景订阅应被排除（主路由 QA 已覆盖）, 实际 {rows}"

    def test_T9_bound_issue_stage_qa_same_skill_bot_early_skipped(self):
        """bot actor 预匹配同口径：绑定 Issue 时同 skill 异场景订阅预派发也被排除"""
        _seed([("scene-A", "qa-cli"), ("scene-B", "qa-cli")],
              stage="qa", scene_of_issue="scene-A")
        _run_hook("grape_bot", "open")
        rows = _events()
        print(f"\n[T9] 绑定 Issue stage=qa 同 skill bot → {rows}")
        extra = [r for r in rows if r[0] == "a2a_event"]
        assert all(r[2] != "scene-B" for r in rows), \
            f"同 skill 异场景 scene-B 不应被派发（主路由 QA 覆盖）, 实际 {rows}"
