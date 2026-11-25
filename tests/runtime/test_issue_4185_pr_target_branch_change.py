# -*- coding: utf-8 -*-
"""Issue #4185: PR 目标分支变更后 PR 订阅 QA 流水线未触发 — 回归测试。

覆盖四类场景：
T1 目标分支切换（无新 commit）→ 按新目标分支触发订阅
T2 目标分支未变且无新 commit → 不重复触发
T3 merged → 不触发订阅
T4 跨流水线移交（不同场景各自 work_branch）→ 同一匹配规则一致生效

附加：bot actor 走 1.5 预匹配同样识别目标分支变化；出站台账对目标分支变化放行。
"""
import json
import uuid
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock

_REPO = "gitcode:XOcto/issue4185-repo"
_BOT_TOKEN = "issue4185-bot-token"
_OLD_BRANCH = "arch-weekly-11-zj"
_NEW_BRANCH = "arch-weekly-11"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped", "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:XOcto/issue4185-repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-i4185-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%i4185-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'i4185-%'")
        db.commit()


def _seed(scenes):
    """scenes: [(scene_id, work_branch, qa_skill), ...]"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'issue4185-repo', 'XOcto/issue4185-repo', 0, 'default', ?, 'wt-i4185', 'grape_bot')",
            (_REPO, _BOT_TOKEN))
        for scene_id, work_branch, qa_skill in scenes:
            nodes = [{"tag": "qa", "skill": qa_skill, "enabled": True}]
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
                "VALUES (?,?,?,?,1,?,?)",
                (scene_id, scene_id, _REPO, json.dumps(nodes), 1,
                 json.dumps({"work_branch": work_branch})))
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("hi4185" + uuid.uuid4().hex, "i4185-cred", "i4185-dev"))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (cid,))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
            "VALUES (?, ?, 'qa', ?, 'idle', 1, ?)",
            ("sess-i4185-qa", _REPO, rid, _BOT_TOKEN))
        db.commit()


def _run_hook(actor, action="update", target_branch=_NEW_BRANCH,
              changes=None, oldrev=None, commits=None, event="Merge Request Hook"):
    import src.domains.runtime.pipeline.event_pipeline as ep
    attrs = {"action": action, "target_branch": target_branch, "iid": 4185,
             "title": "target branch switch", "source_branch": "dev"}
    if oldrev:
        attrs["oldrev"] = oldrev
    payload = {
        "object_attributes": attrs,
        "project": {"path_with_namespace": "XOcto/issue4185-repo"},
        "user": {"username": actor},
    }
    if changes is not None:
        payload["changes"] = changes
    if commits is not None:
        payload["commits"] = commits
    raw = json.dumps(payload).encode()
    with patch("src.domains.runtime.engine.engine_select.pick_engine", return_value=None), \
         patch("src.domains.mgmt.user.db.get_agent_bot_token", return_value=""), \
         patch("src.domains.mgmt.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        ep.run_pipeline(raw, {"X-GitCode-Event": event}, account="XOcto/issue4185-repo")


def _events():
    with db_lock:
        return db.cursor().execute(
            "SELECT event_type, agent_type, scene_id FROM events WHERE repo_key=? ORDER BY seq",
            (_REPO,)).fetchall()


def _skipped_reasons():
    with db_lock:
        return [r[0] for r in db.cursor().execute(
            "SELECT reason FROM event_skipped WHERE repo_key=?", (_REPO,)).fetchall()]


_TARGET_SWITCH_CHANGES = {
    "target_branch": {"previous": _OLD_BRANCH, "current": _NEW_BRANCH},
}


class TestPRTargetBranchChange:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_T1_target_branch_switch_without_commit_triggers(self):
        """目标分支切换、无新 commit：按新目标分支触发订阅，旧分支不触发。"""
        _seed([("scene-old", _OLD_BRANCH, "qa-old"),
               ("scene-new", _NEW_BRANCH, "qa-cli")])
        _run_hook("human_user", action="update", target_branch=_NEW_BRANCH,
                  changes=_TARGET_SWITCH_CHANGES)
        rows = _events()
        assert len(rows) == 1, f"应仅触发新目标分支 1 条订阅，实际 {rows}"
        assert rows[0] == ("pull_request_updated", "qa", "scene-new"), rows

    def test_T1b_bot_actor_target_branch_switch_triggers_early_dispatch(self):
        """bot actor 路径（1.5 预匹配）：目标分支切换无 commit 同样触发。"""
        _seed([("scene-old", _OLD_BRANCH, "qa-old"),
               ("scene-new", _NEW_BRANCH, "qa-cli")])
        _run_hook("grape_bot", action="update", target_branch=_NEW_BRANCH,
                  changes=_TARGET_SWITCH_CHANGES)
        rows = _events()
        assert len(rows) == 1, f"bot 预派发应仅触发新目标分支，实际 {rows}"
        assert rows[0] == ("a2a_event", "qa", "scene-new"), rows

    def test_T2_branch_unchanged_without_commit_skips(self):
        """目标分支未变且无新 commit：不重复触发。"""
        _seed([("scene-new", _NEW_BRANCH, "qa-cli")])
        _run_hook("human_user", action="update", target_branch=_NEW_BRANCH,
                  changes={"target_branch": {"previous": _NEW_BRANCH, "current": _NEW_BRANCH}})
        assert _events() == [], f"分支未变且无 commit 不应触发，实际 {_events()}"

    def test_T2b_no_change_fields_without_commit_skips(self):
        """payload 无 changes 且无新 commit：保持旧跳过行为。"""
        _seed([("scene-new", _NEW_BRANCH, "qa-cli")])
        _run_hook("human_user", action="update", target_branch=_NEW_BRANCH)
        assert _events() == [], f"无 commit 无分支变化不应触发，实际 {_events()}"

    def test_T3_merged_not_subscribed(self):
        """merged：即使目标分支字段存在也不触发 PR 订阅。"""
        _seed([("scene-new", _NEW_BRANCH, "qa-cli")])
        _run_hook("human_user", action="merge", target_branch=_NEW_BRANCH,
                  changes=_TARGET_SWITCH_CHANGES)
        rows = _events()
        assert not any(r[0] in ("pull_request_updated", "a2a_event") and r[1] == "qa" for r in rows), \
            f"merged 不应触发 QA 订阅，实际 {rows}"

    def test_T4_cross_pipeline_handoff_matches_new_branch(self):
        """跨流水线移交：不同场景各持 work_branch，目标分支切换后仅承接新分支的场景触发。"""
        _seed([("handoff-source-scene", _OLD_BRANCH, "qa-source"),
               ("handoff-target-scene", _NEW_BRANCH, "qa-target")])
        _run_hook("human_user", action="update", target_branch=_NEW_BRANCH,
                  changes={"target_branch": {"from": _OLD_BRANCH, "to": _NEW_BRANCH}})
        rows = _events()
        assert len(rows) == 1 and rows[0][2] == "handoff-target-scene", \
            f"移交后应按新分支场景一致匹配，实际 {rows}"

    def test_T5_target_branch_change_passes_outbound_ledger(self):
        """出站台账命中时，目标分支变化（人工动作）放行；纯文本编辑仍拦。"""
        _seed([("scene-new", _NEW_BRANCH, "qa-cli")])
        import src.domains.runtime.pipeline.event_pipeline as ep
        with patch("src.kernel.outbound_ledger.is_our_outbound", return_value=True), \
             patch("src.domains.runtime.engine.engine_select.pick_engine", return_value=None), \
             patch("src.domains.mgmt.user.db.get_agent_bot_token", return_value=""), \
             patch("src.domains.mgmt.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            raw = json.dumps({
                "object_attributes": {"action": "update", "target_branch": _NEW_BRANCH,
                                      "iid": 4185, "title": "switch"},
                "changes": _TARGET_SWITCH_CHANGES,
                "project": {"path_with_namespace": "XOcto/issue4185-repo"},
                "user": {"username": "grape_bot"},
            }).encode()
            ep.run_pipeline(raw, {"X-GitCode-Event": "Merge Request Hook"},
                            account="XOcto/issue4185-repo")
        assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()
        assert len(_events()) == 1, _events()


class TestPRTargetBranchChangeDetector:
    def test_changed_shapes(self):
        from src.domains.runtime.pipeline.event_pipeline import _pr_target_branch_changed
        assert _pr_target_branch_changed({"changes": {
            "target_branch": {"previous": "a", "current": "b"}}}) is True
        assert _pr_target_branch_changed({"changes": {
            "target_branch": {"from": "a", "to": "b"}}}) is True
        assert _pr_target_branch_changed({"changes": {
            "target_branch": {"old": "a", "new": "b"}}}) is True

    def test_unchanged_or_missing(self):
        from src.domains.runtime.pipeline.event_pipeline import _pr_target_branch_changed
        assert _pr_target_branch_changed({"changes": {
            "target_branch": {"previous": "a", "current": "a"}}}) is False
        assert _pr_target_branch_changed({"changes": {}}) is False
        assert _pr_target_branch_changed({}) is False
        assert _pr_target_branch_changed({"object_attributes": {"target_branch": "a"}}) is False