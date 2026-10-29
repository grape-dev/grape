"""Issue #516 M3：超时失败（timed out after）按仓库聚合观测测试。

覆盖：
1. alerts._repo_metrics 新增 task_timeout 指标（失败窗口内超时签名计数，按仓库）
2. SEED_RULES 含 task_timeout 告警规则 + seed_rules 幂等落库
3. metric_rollup：task_timeout 在 SUM_METRICS；compute_bucket 按桶计数
4. service._recover_cutoff_for_agent：coding 回收阈值 ≥ 7200s（防双跑对齐）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.session_manager import db, db_lock
from src.runtime import service
from src.observability import alerts
from src.observability import metric_rollup as mr


@pytest.fixture(autouse=True)
def _clean_tasks():
    _like = "gitcode:test/to-%"
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?", (_like,))
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?", (_like,))
        db.commit()


def _insert_task(repo, seq, *, status="failed", result_text="", errors=""):
    tid = service.enqueue_task(
        agent_session_id=f"asess-{seq}", runtime_id=4, prompt="p", event_seq=seq,
        issue_id="#1", repo_key=repo, agent_type="coding", skill="s",
        issue_session_id=f"isess-{seq}")
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_tasks SET status=?, completed_at=?, started_at=?,"
            " result_text=?, errors=? WHERE id=?",
            (status, now, now, result_text, errors, tid))
        db.commit()
    return tid


class TestRepoMetricsTaskTimeout:
    def test_timeout_failure_counted(self):
        _insert_task("gitcode:test/to-a", 101,
                     result_text="task timed out after 3600s (attempts: 2, retries: 1)")
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/to-a", {}).get("task_timeout") == 1.0

    def test_timeout_signature_in_errors_counted(self):
        _insert_task("gitcode:test/to-b", 102, errors="task timed out after 7200s")
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/to-b", {}).get("task_timeout") == 1.0

    def test_non_timeout_failure_not_counted(self):
        _insert_task("gitcode:test/to-c", 103, result_text="engine exit_code=1, errors: boom")
        m = alerts._repo_metrics()
        assert not m.get("gitcode:test/to-c", {}).get("task_timeout")

    def test_aggregation_across_tasks(self):
        for i in range(3):
            _insert_task("gitcode:test/to-d", 110 + i, result_text="task timed out after 3600s")
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/to-d", {}).get("task_timeout") == 3.0


class TestSeedRule:
    def test_seed_rules_contains_task_timeout(self):
        rule = next((r for r in alerts.SEED_RULES if r["name"] == "task_timeout"), None)
        assert rule is not None
        assert rule["metric"] == "task_timeout"
        assert rule["level"] == "user"
        assert rule["threshold"] == 0
        assert rule["comparator"] == "gt"
        assert int(rule.get("enabled", 1)) == 1

    def test_seed_rules_idempotent_insert(self):
        with db_lock:
            db.cursor().execute("DELETE FROM obs_alert_rules WHERE name='task_timeout'")
            db.commit()
        n1 = alerts.seed_rules()
        assert n1 >= 1  # 至少插入了 task_timeout
        n2 = alerts.seed_rules()
        assert n2 == 0  # 幂等
        with db_lock:
            row = db.cursor().execute(
                "SELECT metric, level, enabled FROM obs_alert_rules WHERE name='task_timeout'"
            ).fetchone()
        assert row and row[0] == "task_timeout" and row[1] == "user" and row[2] == 1


class TestMetricRollup:
    def test_task_timeout_in_sum_metrics(self):
        assert "task_timeout" in mr.SUM_METRICS
        assert "task_timeout" in mr.ALL_METRICS

    def test_compute_bucket_counts_timeout(self):
        _insert_task("gitcode:test/to-e", 130, result_text="task timed out after 3600s")
        _insert_task("gitcode:test/to-e", 131, result_text="engine exit_code=1")
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=2)
        out = mr.compute_bucket(start, end)
        got = out.get("repo:gitcode:test/to-e", {}).get("task_timeout")
        assert got is not None and got[0] == 1.0 and got[1] == 1


class TestRecoverCutoffAlignment:
    """Issue #516 M1：服务端 running 回收阈值按 agent 超时对齐（防 7200s 双跑）。"""

    def test_coding_cutoff_at_least_7200(self, monkeypatch):
        monkeypatch.delenv("TASK_RECOVER_STALE_SECONDS", raising=False)
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        cut_iso = service._recover_cutoff_for_agent("coding")
        cut = datetime.fromisoformat(cut_iso)
        age = (datetime.now(timezone.utc) - cut).total_seconds()
        assert age >= 7200, f"coding 回收阈值应 ≥7200s，实际 {age}"

    def test_other_agent_at_least_3600(self, monkeypatch):
        monkeypatch.delenv("TASK_RECOVER_STALE_SECONDS", raising=False)
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
        cut_iso = service._recover_cutoff_for_agent("vod")
        cut = datetime.fromisoformat(cut_iso)
        age = (datetime.now(timezone.utc) - cut).total_seconds()
        assert 3600 <= age < 7200
