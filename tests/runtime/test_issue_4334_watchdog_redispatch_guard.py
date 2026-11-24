"""Issue #4334：watchdog 重派终态预检 / 频次上限 / context 非空保护。

覆盖：
- 纯判定：qa-passed / 已完成 / external-block 标记 / 正常在途单据放行；
- 综合守卫：终态跳过留痕、重派 context 回填/空 context 拦截、上限拦截留痕；
- 与 A2A 终态门禁集成：coding 重派命中终态即短路。
"""
import json
from datetime import datetime, timezone

from src.api import watchdog_redispatch_guard as guard


class TestEvaluatePipelineTerminal:
    def test_qa_passed_latest_segment_is_terminal(self):
        segments = [(10, "coding", "completed", "", None, ""),
                    (11, "qa", "completed", "结论：通过", None, "")]
        state = guard.evaluate_pipeline_terminal(segments=segments)
        assert state["terminal"] is True
        assert state["reason"] == "qa_passed"
        assert "pipeline.segment.seq=11" in state["criteria_source"]

    def test_running_after_qa_is_not_terminal(self):
        segments = [(11, "qa", "completed", "结论：通过", None, ""),
                    (12, "coding", "running", "", None, "")]
        state = guard.evaluate_pipeline_terminal(segments=segments)
        assert state["terminal"] is False

    def test_issue_status_completed_is_terminal(self):
        state = guard.evaluate_pipeline_terminal(status="completed")
        assert state["terminal"] is True
        assert state["criteria_source"] == "issue.status"

    def test_issue_stage_done_is_terminal(self):
        state = guard.evaluate_pipeline_terminal(stage="done")
        assert state["terminal"] is True
        assert state["criteria_source"] == "issue.stage"

    def test_external_block_marker_is_terminal(self):
        state = guard.evaluate_pipeline_terminal(
            external_block={"hit": True, "source": "issue.label", "marker": "external-block"})
        assert state["terminal"] is True
        assert state["reason"] == "external_block"
        assert state["criteria_source"] == "issue.label"

    def test_in_flight_issue_passes(self):
        segments = [(10, "coding", "completed", "", None, "")]
        state = guard.evaluate_pipeline_terminal(status="processing", stage="qa",
                                                 segments=segments)
        assert state["terminal"] is False

    def test_qa_rejection_text_is_not_terminal(self):
        # QA 打回同样是 completed，但结论「不通过」必须允许 coding 返工重派。
        segments = [(10, "coding", "completed", "", None, ""),
                    (11, "qa", "completed", "审查不通过，需返工", None, "")]
        state = guard.evaluate_pipeline_terminal(segments=segments)
        assert state["terminal"] is False
        assert state["reason"] == ""

    def test_qa_rejection_structured_next_step_coding_is_not_terminal(self):
        segments = [(11, "qa", "completed", "", {"next_step": "a2a_coding"}, "")]
        state = guard.evaluate_pipeline_terminal(segments=segments)
        assert state["terminal"] is False

    def test_qa_unknown_conclusion_is_not_terminal(self):
        segments = [(11, "qa", "completed", "", None, "")]
        state = guard.evaluate_pipeline_terminal(segments=segments)
        assert state["terminal"] is False


class TestSceneConvergence:
    def _patch_inputs(self, monkeypatch, scene):
        monkeypatch.setattr(
            guard, "load_issue_terminal_inputs",
            lambda *a, **k: {"status": "", "stage": ""})
        monkeypatch.setattr(guard, "_resolve_scene_key", lambda *a, **k: scene)

    def test_segment_query_scoped_to_scene(self, monkeypatch):
        self._patch_inputs(monkeypatch, "scene-b")
        captured = {}

        def _fake_segments(repo, num, scene_id=None, default_scene=False):
            captured.update(scene_id=scene_id, default=default_scene)
            return [(11, "qa", "completed", "结论：通过", None, "")]

        monkeypatch.setattr(guard, "_query_segment_rows", _fake_segments)
        out = guard.resolve_pipeline_terminal_state("gitcode:owner/repo", "5")
        assert captured == {"scene_id": "scene-b", "default": False}
        assert out["terminal"] is True

    def test_default_scene_includes_legacy_empty_scene(self, monkeypatch):
        self._patch_inputs(monkeypatch, "default")
        captured = {}

        def _fake_segments(repo, num, scene_id=None, default_scene=False):
            captured.update(scene_id=scene_id, default=default_scene)
            return []

        monkeypatch.setattr(guard, "_query_segment_rows", _fake_segments)
        out = guard.resolve_pipeline_terminal_state("gitcode:owner/repo", "5")
        assert captured == {"scene_id": "default", "default": True}
        assert out["terminal"] is False

    def test_unresolved_scene_fails_open_without_query(self, monkeypatch):
        self._patch_inputs(monkeypatch, None)
        called = []
        monkeypatch.setattr(guard, "_query_segment_rows",
                            lambda *a, **k: called.append(1) or [])
        out = guard.resolve_pipeline_terminal_state("gitcode:owner/repo", "5")
        assert called == []
        assert out["terminal"] is False


class TestExternalBlockDetection:
    def test_label_marker(self):
        got = guard.detect_external_block(labels=["bug", "external-block"])
        assert got["hit"] is True
        assert got["source"] == "issue.label"

    def test_object_labels(self):
        got = guard.detect_external_block(labels=[{"name": "External_Block"}])
        assert got["hit"] is True

    def test_payload_marker(self):
        got = guard.detect_external_block(payload={"external_block": True})
        assert got["hit"] is True
        assert got["source"] == "payload.external_block"

    def test_carried_context_marker(self):
        got = guard.detect_external_block(
            carried_context={"external_status": "blocked"})
        assert got["hit"] is True

    def test_clean_payload(self):
        assert guard.detect_external_block(payload={"context": "x"})["hit"] is False


class TestRedispatchPayload:
    def test_patrol_context_marker(self):
        payload = {"context": "巡检器补偿交棒[compensated_by=patrol:t:1]"}
        assert guard.is_redispatch_payload(payload) is True

    def test_explicit_marker(self):
        assert guard.is_redispatch_payload({"redispatch_mode": "watchdog"}) is True

    def test_normal_handoff(self):
        assert guard.is_redispatch_payload(
            {"context": "Issue #1 开发完成，请审查 PR #2"}) is False


class TestGuardWatchdogRedispatch:
    def _patch(self, monkeypatch, *, terminal=None, original="", limit=None):
        monkeypatch.setattr(guard, "resolve_pipeline_terminal_state",
                            lambda *a, **k: terminal or {"terminal": False})
        monkeypatch.setattr(guard, "resolve_original_context",
                            lambda *a, **k: original)
        monkeypatch.setattr(guard, "redispatch_limit_state",
                            lambda *a, **k: limit or {"hit": False, "count": 0,
                                                      "limit": 3, "window": 1800})
        recorded = []
        monkeypatch.setattr(
            guard, "record_redispatch_skip",
            lambda repo, num, agent, reason, detail="": recorded.append(
                (reason, detail)) or detail)
        return recorded

    def test_terminal_skips_and_records_criteria(self, monkeypatch):
        recorded = self._patch(monkeypatch, terminal={
            "terminal": True, "reason": "qa_passed",
            "criteria_source": "pipeline.segment.seq=11",
            "external_block": False, "qa_passed": True, "matched_marker": "qa:completed"})
        out = guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding",
            {"redispatch_mode": "watchdog", "context": "x"})
        assert out["reason"] == guard.PIPELINE_TERMINAL_REASON
        assert "criteria=pipeline.segment.seq=11" in out["detail"]
        assert recorded and recorded[0][0] == guard.PIPELINE_TERMINAL_REASON

    def test_empty_context_without_original_is_blocked(self, monkeypatch):
        recorded = self._patch(monkeypatch, original="")
        out = guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding",
            {"compensated_by": "patrol:t:1", "context": ""})
        assert out["reason"] == guard.CONTEXT_EMPTY_REASON
        assert recorded and recorded[0][0] == guard.CONTEXT_EMPTY_REASON

    def test_empty_context_backfilled_from_original(self, monkeypatch):
        self._patch(monkeypatch, original="原任务结论：已实现并通过 QA")
        payload = {"compensated_by": "patrol:t:1", "context": ""}
        out = guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding", payload)
        assert out == {}
        assert payload["context"] == "原任务结论：已实现并通过 QA"

    def test_limit_hit_blocks_and_records(self, monkeypatch):
        recorded = self._patch(monkeypatch, limit={
            "hit": True, "count": 3, "limit": 3, "window": 1800})
        out = guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding",
            {"compensated_by": "patrol:t:1", "context": "ctx"})
        assert out["reason"] == guard.REDISPATCH_LIMIT_REASON
        assert "count=3 limit=3" in out["detail"]
        assert recorded and recorded[0][0] == guard.REDISPATCH_LIMIT_REASON

    def test_non_coding_target_not_guarded(self, monkeypatch):
        self._patch(monkeypatch, terminal={"terminal": True, "reason": "qa_passed"})
        assert guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "qa",
            {"redispatch_mode": "watchdog", "context": "x"}) == {}

    def test_clean_coding_redispatch_passes(self, monkeypatch):
        self._patch(monkeypatch)
        assert guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding",
            {"compensated_by": "patrol:t:1", "context": "ctx"}) == {}

    def test_normal_coding_dispatch_not_terminal_guarded(self, monkeypatch):
        # Spec 范围：终态判定只作用于 watchdog 自动重派，普通 coding 派发放行。
        recorded = self._patch(monkeypatch, terminal={
            "terminal": True, "reason": "qa_passed",
            "criteria_source": "pipeline.segment.seq=11"})
        out = guard.guard_watchdog_redispatch(
            "gitcode:owner/repo", "5", "coding",
            {"context": "Issue #5 开发完成，请审查 PR #6"})
        assert out == {}
        assert recorded == []


class TestRecordSkipPersistence:
    def test_skip_writes_single_hash_issue_id_and_detail(self):
        """留痕真实写入 event_skipped：issue_id 不得出现双井号 ##。"""
        from src.kernel.session_manager import db, db_lock
        rk = "gitcode:test/issue4334"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM event_skipped WHERE repo_key=?", (rk,))
            db.commit()
        try:
            guard.record_redispatch_skip(
                rk, "#4334", "coding", guard.PIPELINE_TERMINAL_REASON,
                "target=coding criteria=pipeline.segment.seq=11")
            with db_lock:
                c = db.cursor()
                row = c.execute(
                    "SELECT issue_id, event_type, reason, detail FROM event_skipped "
                    "WHERE repo_key=? ORDER BY id DESC LIMIT 1", (rk,)).fetchone()
            assert row is not None
            assert row[0] == "#4334", row
            assert row[1] == "a2a_event"
            assert row[2] == guard.PIPELINE_TERMINAL_REASON
            assert "criteria=pipeline.segment.seq=11" in row[3]
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM event_skipped WHERE repo_key=?", (rk,))
                db.commit()


class TestCountRecentRedispatches:
    def test_counts_only_redispatch_payloads_in_window(self, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (3, now, json.dumps({"context": "巡检器补偿交棒[compensated_by=patrol:a:1]"})),
            (2, now, json.dumps({"context": "normal handoff"})),
            (1, now, json.dumps({"context": "巡检器补偿交棒[compensated_by=patrol:b:1]"})),
        ]
        monkeypatch.setattr(guard, "_query_recent_dispatch_payloads",
                            lambda *a, **k: rows)
        assert guard.count_recent_redispatches("gitcode:owner/repo", "5", "coding") == 2

    def test_limit_state(self, monkeypatch):
        monkeypatch.setattr(guard, "count_recent_redispatches", lambda *a, **k: 3)
        state = guard.redispatch_limit_state("gitcode:owner/repo", "5", "coding", limit=3)
        assert state["hit"] is True
        assert state["count"] == 3
        monkeypatch.setattr(guard, "count_recent_redispatches", lambda *a, **k: 2)
        assert guard.redispatch_limit_state(
            "gitcode:owner/repo", "5", "coding", limit=3)["hit"] is False


class TestTerminalGuardIntegration:
    def test_pipeline_terminal_short_circuits_coding(self, monkeypatch):
        import src.api.a2a_terminal_guard as terminal_guard
        monkeypatch.setattr(terminal_guard, "resolve_terminal_state",
                            lambda *a, **k: {"terminal": False, "labels": ["bug"]})
        monkeypatch.setattr(
            guard, "resolve_pipeline_terminal_state",
            lambda *a, **k: {"terminal": True, "reason": "qa_passed",
                             "criteria_source": "pipeline.segment.seq=11",
                             "external_block": False, "qa_passed": True,
                             "matched_marker": "qa:completed"})
        out = terminal_guard.guard_terminal_dispatch(
            "gitcode:owner/repo", "5", "coding",
            {"redispatch_mode": "watchdog", "context": "x"})
        assert out["reason"] == guard.PIPELINE_TERMINAL_REASON
        assert out["triggers"]
        assert out["terminal"] is True