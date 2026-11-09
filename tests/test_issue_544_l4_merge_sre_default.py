# -*- coding: utf-8 -*-
"""Issue #544 L4：merge 订阅兜底推广 + 断链 Issue 提示评论 — 测试。

验证点：
T1 pull_request_merged 无任何路由命中（无关联 Issue/stage、无订阅）→ 默认派发 sre
   （所有启用 A2A 的仓库覆盖 merge 订阅兜底）；
T2 sre agent 被禁用 → 不默认派发（skipped: agent_disabled）；
T3 自动重试耗尽（断链收口）→ 向关联 Issue 发可见提示评论；
T4 伪 issue（#pr_... 无真实 Issue 号）→ 不发评论。
"""
import json

import pytest

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/i544-repo"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped",
                  "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:XOcto/i544-repo%'")
        db.commit()


def _seed_repo(with_sre_disabled=False):
    """managed_repos + 含 sre 节点的场景；不建 issue_state（无关联 Issue/stage）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'i544-repo', 'XOcto/i544-repo', 0, 'scene-544', '', 'wt-544', 'grape_bot')",
            (_REPO,))
        nodes = [{"tag": "sre", "id": "sre", "skill": "sre-cli", "enabled": True,
                  "engine_runtime_ids": [77]}]
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,0,?)",
            ("scene-544", "scene-544", _REPO, json.dumps(nodes),
             json.dumps({"work_branch": "branch-544"})))
        if with_sre_disabled:
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, enable) "
                "VALUES ('sess-544-sre', ?, 'sre', 0)", (_REPO,))
        db.commit()


def _merged_payload(iid=92):
    attrs = {"action": "merge", "target_branch": "branch-544", "iid": iid,
             "title": "no issue reference", "source_branch": "dev", "state": "merged"}
    return json.dumps({
        "object_attributes": attrs,
        "project": {"path_with_namespace": "XOcto/i544-repo"},
        "user": {"username": "human_user"},
    }).encode()


def _run(monkeypatch):
    import src.api.event_pipeline as ep
    import src.runtime.engine_select as es
    import src.api.pr_merged_sre_retry as retry_mod
    monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
    monkeypatch.setattr(es, "get_scene_pool_engine_ids", lambda *a, **kw: None)
    monkeypatch.setattr(retry_mod, "schedule_pr_merged_sre_retry", lambda *a, **kw: True)
    from unittest.mock import patch as _patch
    with _patch("src.user.db.get_agent_bot_token", return_value=""), \
         _patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        ep.run_pipeline(_merged_payload(), {"X-GitCode-Event": "Merge Request Hook"},
                        account="XOcto/i544-repo")


def _sre_events():
    with db_lock:
        return db.cursor().execute(
            "SELECT seq, status FROM events WHERE repo_key=? "
            "AND event_type='pull_request_merged' AND agent_type='sre'",
            (_REPO,)).fetchall()


class TestMergeSREDefault:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        import src.api.pr_merged_sre_retry as mod
        mod._reset_registry()
        yield
        _clean()
        mod._reset_registry()

    def test_T1_merged_defaults_to_sre(self, monkeypatch):
        """无路由命中的 merged PR → 默认派发 sre（Issue #544 L4 兜底推广）。"""
        _seed_repo()
        _run(monkeypatch)
        rows = _sre_events()
        assert rows, "merged PR 无路由时应默认派发 sre（所有启用 A2A 的仓库）"

    def test_T2_sre_disabled_skips(self, monkeypatch):
        """sre 被禁用 → 不默认派发，留 skipped 记录。"""
        _seed_repo(with_sre_disabled=True)
        _run(monkeypatch)
        assert not _sre_events(), "sre 禁用时不应默认派发"
        with db_lock:
            skipped = db.cursor().execute(
                "SELECT reason FROM event_skipped WHERE repo_key=? "
                "AND event_type='pull_request_merged'", (_REPO,)).fetchall()
        # sre 禁用 → 不默认派发：skipped 记录存在（issue_done=PR 事件无路由的既有语义）
        assert skipped, skipped

    def test_T3_exhausted_retry_posts_issue_comment(self, monkeypatch):
        """重试耗尽（断链收口）→ 向关联 Issue 发提示评论。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        import src.api.event_pipeline as ep
        from src.api.event_store import append_event
        _seed_repo()
        seq = append_event(_REPO, "pull_request_merged", title="fix: #544 t",
                           issue_id="#544", agent_type="sre",
                           summary="PR #544 已合并", status="failed",
                           result="场景绑定的引擎全部离线", scene_id="scene-544",
                           entity_type="pull_request")
        monkeypatch.setattr(es, "get_scene_pool_engine_ids", lambda *a, **kw: None)
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
        import src.user.db as udb
        monkeypatch.setattr(udb, "get_repo_token_with_refresh", lambda rk: "tok-544")
        posted = []
        monkeypatch.setattr(ep, "_post_result_to_issue",
                            lambda repo, num, body, token, agent, rk="": posted.append((repo, num, body, agent)))
        p = mod._normalize_params(
            repo_key=_REPO, seq=seq, agent_type="sre", backend="opencode",
            skill_name="sre-cli", skill_id=1, skill_path="/skills/sre",
            evt={"type": "pull_request_merged", "repository": "XOcto/i544-repo",
                 "issue_id": "#544", "payload": {}},
            issue_id="#544", runtime_id=0, scene_id="scene-544", scene_uid=0)
        for n in range(1, 4):
            mod._run_attempt(n, p, attempts=3, base_delay=0)
        assert posted, "重试耗尽应向关联 Issue 发断链提示评论（Issue #544 L4）"
        repo, num, body, agent = posted[0]
        assert num == "544" and agent == "sre", posted[0]
        assert "断链" in body or "自动重试" in body, body

    def test_T4_pseudo_issue_no_comment(self, monkeypatch):
        """伪 issue（#pr_...）→ 不发 Issue 评论。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        import src.api.event_pipeline as ep
        from src.api.event_store import append_event
        _seed_repo()
        seq = append_event(_REPO, "pull_request_merged", title="PR 订阅",
                           issue_id="#pr_92_scene-544", agent_type="sre",
                           summary="PR 已合并", status="failed",
                           result="场景绑定的引擎全部离线", scene_id="scene-544",
                           entity_type="pull_request")
        monkeypatch.setattr(es, "get_scene_pool_engine_ids", lambda *a, **kw: None)
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
        posted = []
        monkeypatch.setattr(ep, "_post_result_to_issue",
                            lambda repo, num, body, token, agent, rk="": posted.append((repo, num, body, agent)))
        p = mod._normalize_params(
            repo_key=_REPO, seq=seq, agent_type="sre", backend="opencode",
            skill_name="sre-cli", skill_id=1, skill_path="/skills/sre",
            evt={"type": "pull_request_merged", "repository": "XOcto/i544-repo",
                 "issue_id": "#pr_92_scene-544", "payload": {}},
            issue_id="#pr_92_scene-544", runtime_id=0, scene_id="scene-544", scene_uid=0)
        for n in range(1, 4):
            mod._run_attempt(n, p, attempts=3, base_delay=0)
        assert not posted, "伪 issue 不应发 Issue 评论"
