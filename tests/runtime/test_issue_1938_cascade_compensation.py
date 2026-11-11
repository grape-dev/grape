"""Issue #1938 回归用例：级联收敛补偿不得伪造 Agent 结论。

覆盖验收（Issue 评论 199800704 / 199801303）：
- 三条补偿路径（update_task_status 兜底 / sweeper / converge-event 端点）各可归因（triggered_by）；
- 补偿结论结构化落库（cascade_reason / report_missing / triggered_by / 原始任务终态 / 最后已知事件状态）；
- 文案由字段生成，不再有「任务已完成：级联收敛」硬编码与「级联收敛：任务{status}」拼串兜底；
- `timeout_failed` 变体映射为事件 failed + 非零 exit_code（不得记成 completed/0），且补偿端点不再
  `task_not_terminal`；
- 能取到真实结论（agent_tasks.result_text）时优先回填，取不到才写补偿文案；
- 展示层标注/过滤：合成补偿不计入「Agent 答复结论」口径；
- 按 agent/场景的补偿计数指标 + 阈值告警规则；
- 幂等性与 event_active_guard 释放语义不退化。
"""
import json
from datetime import datetime, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.runtime.engine import cascade


REPO = "gitcode:test1938/repo"


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test1938/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test1938/%'")
        db.commit()


def _insert_event(seq, status="running", event_type="a2a_event", agent_type="coding",
                  issue_id="#1938", scene_id="scene-1938"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, scene_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, REPO, event_type, status, agent_type, issue_id, scene_id,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def _insert_task(task_id, event_seq, status="running", exit_code=None, result_text=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at, exit_code, result_text) "
            "VALUES (?, ?, 0, ?, '#1938', ?, 'coding', 'p', ?, ?, ?, ?)",
            (task_id, "sess-1938", event_seq, REPO, status,
             datetime.now(timezone.utc).isoformat(), exit_code, result_text),
        )
        db.commit()


def _event_row(seq):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT status, result, completed_at, exit_code, cascade_reason, report_missing, "
            "triggered_by, cascade_task_status, cascade_prev_event_status, structured_output "
            "FROM events WHERE seq=?", (seq,))
        return c.fetchone()


def _guard_count(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM event_active_guard WHERE seq=?", (seq,))
        return c.fetchone()[0]


class TestTriggerAttribution:
    """三条触发路径都能区分补偿来源（triggered_by）。"""

    def test_update_task_status_trigger(self):
        _insert_event(19381, status="running")
        _insert_task("task-1938-a", 19381, status="running")
        service.update_task_status("task-1938-a", "completed", exit_code=0)
        row = _event_row(19381)
        assert row[0] == "completed"
        assert row[6] == cascade.TRIGGER_UPDATE_TASK_STATUS
        assert not row[9], "合成补偿不得写 structured_output（无名副其实的 Agent 产出）"

    def test_sweeper_trigger(self):
        _insert_event(19382, status="running")
        _insert_task("task-1938-b", 19382, status="running")
        service.update_task_status("task-1938-b", "failed", exit_code=1,
                                   cascade_trigger=cascade.TRIGGER_SWEEPER)
        row = _event_row(19382)
        assert row[0] == "failed"
        assert row[6] == cascade.TRIGGER_SWEEPER

    def test_converge_event_trigger(self):
        _insert_event(19383, status="running")
        _insert_task("task-1938-c", 19383, status="cancelled", exit_code=None)
        res = service.converge_event_to_task("task-1938-c")
        assert res["status"] == "converged"
        assert res["triggered_by"] == cascade.TRIGGER_CONVERGE_EVENT
        row = _event_row(19383)
        assert row[0] == "cancelled"
        assert row[6] == cascade.TRIGGER_CONVERGE_EVENT

    def test_endpoint_reports_attribution(self):
        from src.domains.runtime.routes.runtime import converge_event
        _insert_event(19384, status="running")
        _insert_task("task-1938-d", 19384, status="failed", exit_code=2)
        res = converge_event("task-1938-d", user={"id": 1, "username": "ops", "auth_type": "session_token"})
        assert res["status"] == "converged"
        assert res["triggered_by"] == cascade.TRIGGER_CONVERGE_EVENT
        assert res["report_missing"] == 1
        assert res["event_status_after"] == "failed"


class TestStructuredAttribution:
    """补偿结论结构化落库：可归因、可统计，文案由字段渲染。"""

    def test_fields_written(self):
        _insert_event(19385, status="running")
        _insert_task("task-1938-e", 19385, status="running")
        service.update_task_status("task-1938-e", "completed", exit_code=0)
        row = _event_row(19385)
        assert row[4] == cascade.REASON_SYNTHESIZED
        assert int(row[5]) == 1
        assert row[7] == "completed"
        assert row[8] == "running", "须保留最后已知事件状态"

    def test_result_rendered_from_fields(self):
        _insert_event(19386, status="pending")
        _insert_task("task-1938-f", 19386, status="running")
        service.update_task_status("task-1938-f", "completed", exit_code=0)
        text = _event_row(19386)[1]
        assert text.startswith(cascade.CASCADE_NOTICE), "补偿文案须自述系统补偿/原始回报丢失"
        assert "补偿路径=update_task_status" in text
        assert "原始任务终态=completed" in text
        assert "最后已知事件状态=pending" in text

    def test_no_legacy_hardcoded_or_concatenated_text(self):
        """不得再出现「任务已完成：级联收敛」「级联收敛：任务{status}」这类伪装/拼串文案。"""
        for i, st in enumerate(("completed", "failed", "cancelled", "timeout_failed")):
            seq = 19390 + i
            _insert_event(seq, status="running")
            _insert_task(f"task-1938-legacy-{i}", seq, status="running")
            service.update_task_status(f"task-1938-legacy-{i}", st, exit_code=None)
            text = _event_row(seq)[1]
            assert "任务已完成：级联收敛" not in text
            assert "级联收敛：任务" not in text


class TestTimeoutFailedMapping:
    """`timeout_failed`：不得把超时失败记成 completed/0（数据失真纠正）。"""

    def test_event_status_failed_and_nonzero_exit(self):
        _insert_event(19395, status="running")
        _insert_task("task-1938-to", 19395, status="pending_self_report")
        service.update_task_status("task-1938-to", "timeout_failed", exit_code=-1)
        row = _event_row(19395)
        assert row[0] == "failed", f"超时失败须映射 failed，实际={row[0]}"
        assert int(row[3]) != 0, f"exit_code 不得为 0，实际={row[3]}"
        assert row[7] == "timeout_failed"
        assert "timeout_failed" in row[1]

    def test_exit_code_zero_input_still_nonzero(self):
        st, ec = cascade.cascade_event_terminal_state("timeout_failed", 0)
        assert (st, ec) == ("failed", -1)

    def test_converge_accepts_timeout_failed_task(self):
        _insert_event(19396, status="running")
        _insert_task("task-1938-to2", 19396, status="timeout_failed", exit_code=-1)
        res = service.converge_event_to_task("task-1938-to2")
        assert res["status"] == "converged", "timeout_failed 任务此前被判 task_not_terminal，无法补偿"
        assert res["event_status_after"] == "failed"
        assert res["cascade_task_status"] == "timeout_failed"


class TestRealResultBackfill:
    """能取到真实结论时优先回填，取不到才写补偿文案。"""

    def test_backfill_from_task_result_text(self):
        _insert_event(19400, status="running")
        _insert_task("task-1938-g", 19400, status="running",
                     result_text="真实结论：已完成迁移并回归通过")
        service.update_task_status("task-1938-g", "completed", exit_code=0)
        row = _event_row(19400)
        assert row[1] == "真实结论：已完成迁移并回归通过", "须优先回填真实结论"
        assert row[4] == cascade.REASON_BACKFILLED
        assert int(row[5]) == 1, "回报确实丢失（report_missing=1），只是结论已回填"

    def test_backfill_from_passed_result_text(self):
        _insert_event(19401, status="running")
        _insert_task("task-1938-h", 19401, status="running")
        service.update_task_status("task-1938-h", "completed", exit_code=0,
                                   result_text="kernel 内联结论")
        assert _event_row(19401)[1] == "kernel 内联结论"

    def test_structured_backfill_writes_structured_output(self):
        _insert_event(19402, status="running")
        _insert_task("task-1938-i", 19402, status="running")
        _structured = json.dumps({"conclusion": "已修复", "actions": ["a"], "artifacts": [], "next_step": "done"},
                                 ensure_ascii=False)
        service.update_task_status("task-1938-i", "completed", exit_code=0, result_text=_structured)
        row = _event_row(19402)
        assert row[1] == _structured
        assert row[9] == _structured, "结构化结论须同步写 structured_output（pipeline 展示主路径）"
        assert json.loads(row[9])["conclusion"] == "已修复"

    def test_cascade_text_not_backfilled_as_real_result(self):
        """任务是补偿文案时不得把它当作真实结论自我复制。"""
        _insert_event(19403, status="running")
        _insert_task("task-1938-j", 19403, status="running",
                     result_text=cascade.build_cascade_result(
                         task_status="completed", prev_event_status="running",
                         triggered_by="sweeper"))
        service.update_task_status("task-1938-j", "completed", exit_code=0)
        row = _event_row(19403)
        assert row[4] == cascade.REASON_SYNTHESIZED


class TestIdempotencyAndGuard:
    """幂等 + event_active_guard 释放语义不退化。"""

    def test_second_converge_is_noop(self):
        _insert_event(19410, status="running")
        _insert_task("task-1938-k", 19410, status="failed", exit_code=2)
        assert service.converge_event_to_task("task-1938-k")["status"] == "converged"
        first = _event_row(19410)
        second = service.converge_event_to_task("task-1938-k")
        assert second["status"] == "noop"
        assert _event_row(19410) == first, "重复补偿不得改写已落库结论与归因字段"

    def test_terminal_event_not_overwritten(self):
        _insert_event(19411, status="completed")
        _insert_task("task-1938-l", 19411, status="running")
        service.update_task_status("task-1938-l", "failed", exit_code=1)
        row = _event_row(19411)
        assert row[0] == "completed", "已终态事件不得被另一终态覆盖"
        assert row[4] in ("", None), "no-op 路径不得写入补偿归因"

    def test_guard_released_and_dedup_unblocked(self):
        from src.api.event_store import claim_event_guard, update_event_guard_seq
        _insert_event(19412, status="running")
        _insert_task("task-1938-m", 19412, status="dispatched")
        assert claim_event_guard(REPO, "#1938", "coding")
        update_event_guard_seq(REPO, "#1938", "coding", 19412)
        assert _guard_count(19412) == 1
        service.cancel_task("task-1938-m")
        assert _guard_count(19412) == 0, "级联收敛须即时释放守卫"
        assert _event_row(19412)[0] == "cancelled"


class TestDisplayLayer:
    """展示层标注/过滤：合成补偿不计入 Agent 答复结论口径。"""

    def test_display_meta_synthesized_excluded(self):
        m = cascade.display_meta(cascade_reason=cascade.REASON_SYNTHESIZED, report_missing=1,
                                 triggered_by="sweeper", task_status="timeout_failed",
                                 prev_event_status="running")
        assert m["system_cascade"] is True
        assert m["is_agent_conclusion"] is False
        assert m["result_kind"] == "system_compensation_synthesized"
        assert m["cascade_label"] == cascade.CASCADE_NOTICE
        assert m["cascade_triggered_by"] == "sweeper"

    def test_display_meta_backfilled_counted(self):
        m = cascade.display_meta(cascade_reason=cascade.REASON_BACKFILLED, report_missing=1)
        assert m["system_cascade"] is True
        assert m["is_agent_conclusion"] is True, "回填的是 Agent 真实结论，可计入答复"
        assert m["result_kind"] == "system_compensation_backfilled"

    def test_display_meta_plain_agent_result(self):
        m = cascade.display_meta(result_text="正常结论")
        assert m["system_cascade"] is False
        assert m["is_agent_conclusion"] is True

    def test_legacy_text_recognized(self):
        m = cascade.display_meta(result_text="任务已完成：级联收敛（回报丢失补偿）")
        assert m["system_cascade"] is True
        assert m["is_agent_conclusion"] is False
        assert "历史记录" in m["cascade_label"]
        assert cascade.is_cascade_result_text("级联收敛：任务timeout_failed") is True
        assert cascade.is_cascade_result_text("真实结论") is False

    def test_fetch_display_meta_batch(self):
        _insert_event(19420, status="running")
        _insert_task("task-1938-n", 19420, status="running")
        service.update_task_status("task-1938-n", "completed", exit_code=0)
        with db_lock:
            meta = cascade.fetch_display_meta(db.cursor(), [19420])
        assert meta[19420]["cascade_reason"] == cascade.REASON_SYNTHESIZED

    def test_list_events_annotates(self):
        from src.domains.runtime.routes.events import list_events
        _insert_event(19421, status="running")
        _insert_task("task-1938-o", 19421, status="running")
        service.update_task_status("task-1938-o", "completed", exit_code=0)
        res = list_events("test1938", "repo", page=1, page_size=50, status="",
                          user={"id": 1, "username": "t", "auth_type": "api_key"})
        item = next(e for e in res["events"] if e["seq"] == 19421)
        assert item["result_kind"] == "system_compensation_synthesized"
        assert item["is_agent_conclusion"] is False
        assert item["cascade"]["cascade_triggered_by"] == cascade.TRIGGER_UPDATE_TASK_STATUS

    def test_pipeline_segment_marked(self):
        from src.domains.runtime.routes.issues import _pipeline_from_events
        _insert_event(19422, status="running")
        _insert_task("task-1938-p", 19422, status="running")
        service.update_task_status("task-1938-p", "completed", exit_code=0)
        with db_lock:
            meta = cascade.fetch_display_meta(db.cursor(), [19422])
        pipe = _pipeline_from_events(REPO, "1938", [{
            "seq": 19422, "agent_type": "coding", "event_type": "a2a_event",
            "status": "completed", "summary": "任务已完成：级联收敛（回报丢失补偿）",
            "cascade": meta.get(19422),
        }])
        seg = pipe["segments"][0]
        assert seg["is_agent_conclusion"] is False
        assert seg["output"]["conclusion"] == cascade.CASCADE_NOTICE
        assert seg["cascade"]["cascade_task_status"] == "completed"


class TestObservability:
    """按 agent/场景的补偿计数指标 + 阈值告警。"""

    def test_counter_increments(self):
        from src.domains.insight import observability as obs
        try:
            from prometheus_client import generate_latest  # noqa: F401
        except Exception:  # pragma: no cover - worker 包无 prometheus
            pytest.skip("prometheus_client 不可用")
        _labels = {"agent_type": "coding", "scene_id": "scene-1938",
                   "trigger": cascade.TRIGGER_UPDATE_TASK_STATUS, "task_status": "completed"}
        before = obs.registry.get_sample_value("grape_event_cascade_compensation_total",
                                               labels=_labels) or 0.0
        _insert_event(19430, status="running", agent_type="coding", scene_id="scene-1938")
        _insert_task("task-1938-q", 19430, status="running")
        service.update_task_status("task-1938-q", "completed", exit_code=0)
        after = obs.registry.get_sample_value("grape_event_cascade_compensation_total",
                                              labels=_labels) or 0.0
        assert after == before + 1.0, f"按 agent/场景/路径的补偿计数须 +1（{before} → {after}）"

    def test_repo_metric_counts_compensation(self):
        from src.domains.insight.observability import alerts
        _insert_event(19431, status="running")
        _insert_task("task-1938-r", 19431, status="running")
        service.update_task_status("task-1938-r", "completed", exit_code=0)
        m = alerts._repo_metrics()
        assert m.get(REPO, {}).get("cascade_compensation", 0.0) >= 1.0

    def test_seed_rule_exists_and_idempotent(self):
        from src.domains.insight.observability import alerts
        rule = next((r for r in alerts.SEED_RULES if r["name"] == "cascade_compensation"), None)
        assert rule is not None
        assert rule["metric"] == "cascade_compensation"
        assert rule["level"] == "user"
        assert rule["comparator"] == "gt"
        assert rule["threshold"] == 3
        assert int(rule.get("enabled", 1)) == 1
        with db_lock:
            db.cursor().execute("DELETE FROM obs_alert_rules WHERE name='cascade_compensation'")
            db.commit()
        assert alerts.seed_rules() >= 1
        assert alerts.seed_rules() == 0, "种子规则须幂等"
        with db_lock:
            row = db.cursor().execute(
                "SELECT metric, level, enabled FROM obs_alert_rules WHERE name='cascade_compensation'"
            ).fetchone()
        assert row and row[0] == "cascade_compensation" and row[1] == "user" and row[2] == 1

    def test_rollup_metric_registered(self):
        from src.domains.insight.observability import metric_rollup as mr
        assert "cascade_compensation" in mr.SUM_METRICS
        assert "cascade_compensation" in mr.ALL_METRICS