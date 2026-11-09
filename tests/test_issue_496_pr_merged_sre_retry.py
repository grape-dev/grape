# -*- coding: utf-8 -*-
"""Issue #496：「PR merged → sre 收尾」链路引擎离线派发级自动重试 — 回归测试。

背景（日志证据，grape events detail）：
- seq 28353/14741/13993/13159：pull_request_merged → sre 派发时命中 #259 方案A
  （场景绑定引擎全部离线）→ 事件立即终态 failed，PR merge webhook 一次性投递，
  离线窗口内的合并收尾事件永久飘红（系统性批量 failed）。
- seq 29249：SRE 本地部署合并冲突中止（业务性失败，保留 failed 语义，不属本修复范围）。
- seq 15625：执行级引擎空输出（由 #403 transient_retry 覆盖，不属本修复范围）。

验证点：
T1 引擎持续离线 → 重试耗尽后 events.result 追加终止说明，事件保持 failed（不僵尸 running）
T2 引擎恢复 → reset_event_terminal 复位事件为 running，并重新入队（_enqueue_pipeline_task 收到 seq/scene 参数）
T3 仅 pull_request_merged + sre 触发调度（issue_comment / qa 等不调度）
T4 事件已被接管（非 failed / 已有活跃任务）→ 跳过自动重派，不重复派发
T5 run_pipeline 集成：PR merged → sre 且场景引擎全灭 → 事件 failed + 安排自动重试（result 带标记）
T6 重复调度幂等（同 seq 只调度一次）
"""
import json
import uuid

import pytest

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/i496-repo"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "issue_state", "issue", "devops_agent_session",
                  "devops_pipeline_scene", "managed_repos", "event_skipped",
                  "event_active_guard"):
            c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:XOcto/i496-repo%'")
        db.commit()


def _mk_event(status="failed", result="场景绑定的引擎全部离线（scene=scene-496, agent=sre）"):
    from src.api.event_store import append_event
    seq = append_event(_REPO, "pull_request_merged", title="fix: #496 t", issue_id="#496",
                       agent_type="sre", summary="PR #496 已合并", status=status,
                       result=result, scene_id="scene-496", entity_type="pull_request")
    assert seq > 0
    return seq


def _evt_merged():
    return {"type": "pull_request_merged", "repository": "XOcto/i496-repo",
            "issue_id": "#496", "payload": {}}


def _params(seq, evt=None, agent="sre"):
    return dict(repo_key=_REPO, seq=seq, agent_type=agent, backend="opencode",
                skill_name="sre-cli", skill_id=1, skill_path="/skills/sre",
                evt=evt or _evt_merged(), issue_id="#496", runtime_id=0,
                scene_id="scene-496", scene_uid=0)


def _event_row(seq):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()


class TestPRMergedSRERetry:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        import src.api.pr_merged_sre_retry as mod
        mod._reset_registry()
        yield
        _clean()
        mod._reset_registry()

    def test_T1_exhausted_marks_result_keeps_failed(self, monkeypatch):
        """引擎持续离线 → 耗尽后 result 追加终止说明，状态保持 failed。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        seq = _mk_event()
        monkeypatch.setattr(es, "get_scene_pool_engine_ids",
                            lambda *a, **kw: None)
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
        p = mod._normalize_params(**_params(seq))
        for n in range(1, 4):
            mod._run_attempt(n, p, attempts=3, base_delay=0)
        row = _event_row(seq)
        assert row[0] == "failed", f"耗尽后应保持 failed，实际 {row[0]}"
        assert "[Issue #496]" in (row[1] or ""), f"result 应含终止说明，实际 {row[1]}"
        assert "自动重试" in (row[1] or "")

    def test_T2_recovery_redispatches(self, monkeypatch):
        """引擎恢复 → 事件复位 running 并重新入队。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        import src.runtime.service as svc
        import src.api.event_pipeline as ep
        seq = _mk_event()
        monkeypatch.setattr(es, "get_scene_pool_engine_ids",
                            lambda *a, **kw: json.dumps([7]))
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 7)
        monkeypatch.setattr(es, "engine_name", lambda eid: "opencode")
        monkeypatch.setattr(svc, "runtime_execution_kind", lambda eid: "remote")
        calls = []
        monkeypatch.setattr(ep, "_enqueue_pipeline_task",
                            lambda *a, **kw: calls.append((a, kw)))
        p = mod._normalize_params(**_params(seq))
        mod._run_attempt(1, p, attempts=3, base_delay=0)
        assert calls, "引擎恢复后应重新入队"
        a, kw = calls[0]
        assert a[0] == _REPO and a[1] == seq and a[2] == "sre", f"入队参数错: {a}"
        assert kw.get("pool_engine_ids") == json.dumps([7])
        row = _event_row(seq)
        assert row[0] == "running", f"重派后事件应复位 running，实际 {row[0]}"

    def test_T3_only_pr_merged_sre_scheduled(self):
        """非 pull_request_merged 或非 sre → 不调度。"""
        import src.api.pr_merged_sre_retry as mod
        assert mod.schedule_pr_merged_sre_retry(**_params(0)) is False
        p = _params(1, evt={"type": "issue_comment"})
        assert mod.schedule_pr_merged_sre_retry(**p) is False
        p = _params(1, evt=_evt_merged(), agent="qa")
        assert mod.schedule_pr_merged_sre_retry(**p) is False
        p = _params(1, evt=_evt_merged())
        p["scene_id"] = ""
        assert mod.schedule_pr_merged_sre_retry(**p) is False

    def test_T4_taken_over_skips_redispatch(self, monkeypatch):
        """事件已复位 running（被人工重派等）→ 不重复派发。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        import src.api.event_pipeline as ep
        seq = _mk_event(status="running", result="")
        monkeypatch.setattr(es, "get_scene_pool_engine_ids",
                            lambda *a, **kw: json.dumps([7]))
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 7)
        calls = []
        monkeypatch.setattr(ep, "_enqueue_pipeline_task",
                            lambda *a, **kw: calls.append((a, kw)))
        p = mod._normalize_params(**_params(seq))
        mod._run_attempt(1, p, attempts=3, base_delay=0)
        assert not calls, "事件非 failed 状态不应重复派发"

    def test_T5_run_pipeline_offline_schedules_retry(self, monkeypatch):
        """集成：PR merged → sre、场景引擎全灭 → failed + result 带自动重试标记。"""
        import src.api.event_pipeline as ep
        import src.runtime.engine_select as es
        scheduled = []

        _seed_stage_qa()

        def _fake_schedule(*a, **kw):
            keys = ("repo_key", "seq", "agent_type", "backend", "skill_name", "skill_id",
                    "skill_path", "evt", "issue_id", "runtime_id", "scene_id", "scene_uid")
            merged = dict(zip(keys, a))
            merged.update(kw)
            scheduled.append(merged)
            return True

        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
        monkeypatch.setattr(es, "get_scene_pool_engine_ids",
                            lambda *a, **kw: None)
        import src.api.pr_merged_sre_retry as mod
        monkeypatch.setattr(mod, "schedule_pr_merged_sre_retry", _fake_schedule)

        attrs = {"action": "merge", "target_branch": "branch-496", "iid": 91,
                 "title": "fix for #496", "source_branch": "dev",
                 "state": "merged"}
        raw = json.dumps({
            "object_attributes": attrs,
            "project": {"path_with_namespace": "XOcto/i496-repo"},
            "user": {"username": "human_user"},
        }).encode()
        from unittest.mock import patch as _patch
        with _patch("src.user.db.get_agent_bot_token", return_value=""), \
             _patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            ep.run_pipeline(raw, {"X-GitCode-Event": "Merge Request Hook"},
                            account="XOcto/i496-repo")

        with db_lock:
            rows = db.cursor().execute(
                "SELECT seq, status, result FROM events WHERE repo_key=? "
                "AND event_type='pull_request_merged' AND agent_type='sre'",
                (_REPO,)).fetchall()
        assert rows, "应产生 pull_request_merged 的 sre 事件"
        seq, status, result = rows[-1]
        assert status == "failed", f"离线时事件应 failed（保留 #259 语义），实际 {status}"
        assert "已安排自动重试" in (result or ""), f"result 应带自动重试标记，实际 {result}"
        assert scheduled, "应调用自动重试调度器"
        assert scheduled[0]["seq"] == seq and scheduled[0]["scene_id"] == "scene-496"

    def test_T6_schedule_idempotent_per_seq(self, monkeypatch):
        """同 seq 重复调度幂等（只调度一次）。

        monkeypatch _schedule_attempt 为 no-op：仅验证去重语义，
        不挂真实 threading.Timer（避免测试进程残留长定时器）。
        """
        import src.api.pr_merged_sre_retry as mod
        monkeypatch.setattr(mod, "_schedule_attempt", lambda *a, **kw: None)
        p = _params(12345)
        assert mod.schedule_pr_merged_sre_retry(**p) is True
        assert mod.schedule_pr_merged_sre_retry(**p) is False

    def test_T7_broadcast_backend_fallback_opencode(self, monkeypatch):
        """T7 审查修复回归：plugin 广播 + engine_id=0 + backend 为空 →
        兜底 "opencode" 入队，不 raise（与 run_pipeline 广播分支语义一致）。"""
        import src.api.pr_merged_sre_retry as mod
        import src.runtime.engine_select as es
        import src.api.event_pipeline as ep
        seq = _mk_event()
        monkeypatch.setattr(es, "get_scene_pool_engine_ids",
                            lambda *a, **kw: json.dumps([]))
        monkeypatch.setattr(es, "pick_engine", lambda *a, **kw: 0)
        monkeypatch.setattr(es, "engine_name", lambda eid: "")
        monkeypatch.setattr(ep, "is_plugin_mode", lambda: True)
        calls = []
        monkeypatch.setattr(ep, "_enqueue_pipeline_task",
                            lambda *a, **kw: calls.append((a, kw)))
        p = mod._normalize_params(**_params(seq))
        p["backend"] = ""
        mod._run_attempt(1, p, attempts=3, base_delay=0)
        assert calls, "广播分支 backend 为空应兜底 opencode 继续入队，不应 raise"
        a, kw = calls[0]
        assert a[3] == "opencode", f"广播分支 backend 应兜底 opencode，实际 {a[3]}"
        assert kw.get("pool_engine_source") == "scene"


def _seed_stage_qa():
    """建 managed_repos + stage=qa 的 issue_state + sre 节点场景。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'XOcto', 'i496-repo', 'XOcto/i496-repo', 0, 'scene-496', '', 'wt-496', 'grape_bot')",
            (_REPO,))
        nodes = [{"tag": "sre", "id": "sre", "skill": "sre-cli", "enabled": True,
                  "engine_runtime_ids": [77]}]
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,0,?)",
            ("scene-496", "scene-496", _REPO, json.dumps(nodes),
             json.dumps({"work_branch": "branch-496"})))
        c.execute(
            "INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent, scene_id) "
            "VALUES (?, '#496', 'qa', 'qa', 'scene-496')",
            (_REPO,))
        db.commit()
