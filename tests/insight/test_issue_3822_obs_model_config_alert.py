"""Issue #3822 轨 C-1（监控面）：配置故障告警的仓库维度指标 + 种子规则。

口径（与既有 `task_timeout` 一致，不新增扫描形态）：
  - `status='failed'` 且 `completed_at >= cutoff`（24h）
  - `result_text` 或 `errors` 命中 `MODEL_CONFIG_ALERT` 签名
  - 按 `repo_key` 分组计数 → 指标 `task_model_config_alert`
  - 种子规则 `task_model_config_alert`：level=user、gt 0、severity=critical（配置故障 ≠ #1985 引擎离线熔断）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.insight.observability import alerts
from src.domains.runtime import public as rt_public

SIGNATURE = "MODEL_CONFIG_ALERT"
REPO_PREFIX = "gitcode:test/mc3822-%"


@pytest.fixture(autouse=True)
def _clean_tasks():
    def _wipe():
        with db_lock:
            db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?", (REPO_PREFIX,))
            db.commit()
    _wipe()
    yield
    _wipe()


def _insert(repo, seq, *, status="failed", result_text="", errors="", age_hours=0.0):
    tid = service.enqueue_task(
        agent_session_id=f"asess-{seq}", runtime_id=4, prompt="p", event_seq=seq,
        issue_id="#3822", repo_key=repo, agent_type="coding", skill="s",
        issue_session_id=f"isess-{seq}")
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_tasks SET status=?, completed_at=?, started_at=?, result_text=?, errors=? "
            "WHERE id=?", (status, ts, ts, result_text, errors, tid))
        db.commit()
    return tid


def _alert_errors(count=2):
    return ('[{"error_code": "%s", "error_class": "config_error", "count": %d, '
            '"escalated": true, "reason": "Not Found: Model not found"}]'
            % (SIGNATURE, count))


def _pending_alert_errors():
    """首次命中（count=1）写入的条目：签名相同但未升级。"""
    return ('[{"error_code": "%s", "error_class": "config_error", "count": 1, '
            '"escalated": false, "first_seen_at": "2026-11-23T00:00:00+00:00", '
            '"reason": "Not Found: Model not found"}]' % SIGNATURE)


class TestRepoMetric:
    def test_escalated_failure_counted(self):
        _insert("gitcode:test/mc3822-a", 9101, errors=_alert_errors())
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/mc3822-a", {}).get("task_model_config_alert") == 1.0

    def test_escalated_result_text_counted(self):
        _insert("gitcode:test/mc3822-b", 9102,
                result_text='原始错误: {"error_code": "MODEL_CONFIG_ALERT", "count": 2, '
                            '"escalated": true}')
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/mc3822-b", {}).get("task_model_config_alert") == 1.0

    def test_first_hit_without_escalation_not_counted(self):
        """首次 Model not found（count=1/escalated=false）是瞬时抖动，不算配置故障。"""
        _insert("gitcode:test/mc3822-h", 9108, errors=_pending_alert_errors())
        m = alerts._repo_metrics()
        assert not m.get("gitcode:test/mc3822-h", {}).get("task_model_config_alert")

    def test_plain_model_error_without_marker_not_counted(self):
        """只有一次 Model not found（未升级）不该进「配置故障」告警。"""
        _insert("gitcode:test/mc3822-c", 9103,
                errors='[{"error_code": "CONFIG_ERROR", "reason": "Not Found: Model not found"}]')
        m = alerts._repo_metrics()
        assert not m.get("gitcode:test/mc3822-c", {}).get("task_model_config_alert")

    def test_outside_24h_window_not_counted(self):
        _insert("gitcode:test/mc3822-d", 9104, errors=_alert_errors(), age_hours=30)
        m = alerts._repo_metrics()
        assert not m.get("gitcode:test/mc3822-d", {}).get("task_model_config_alert")

    def test_non_failed_status_not_counted(self):
        _insert("gitcode:test/mc3822-e", 9105, status="completed", errors=_alert_errors())
        m = alerts._repo_metrics()
        assert not m.get("gitcode:test/mc3822-e", {}).get("task_model_config_alert")

    def test_aggregation_across_tasks(self):
        for i in range(3):
            _insert("gitcode:test/mc3822-f", 9110 + i, errors=_alert_errors())
        m = alerts._repo_metrics()
        assert m.get("gitcode:test/mc3822-f", {}).get("task_model_config_alert") == 3.0


class TestPublicHelper:
    def test_counts_by_repo(self):
        _insert("gitcode:test/mc3822-g", 9120, errors=_alert_errors())
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = rt_public.alerts_task_model_config_alert(cutoff, SIGNATURE)
        assert ("gitcode:test/mc3822-g", 1) in [tuple(r) for r in rows]

    def test_first_hit_without_escalation_not_counted(self):
        """SQL 必须区分 count=1（未升级）：签名命中但 escalated=false 不计入告警。"""
        _insert("gitcode:test/mc3822-i", 9121, errors=_pending_alert_errors())
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = [tuple(r) for r in rt_public.alerts_task_model_config_alert(cutoff, SIGNATURE)]
        assert not [r for r in rows if r[0] == "gitcode:test/mc3822-i"]


class TestSeedRule:
    def test_seed_rule_declared(self):
        rule = next((r for r in alerts.SEED_RULES if r["name"] == "task_model_config_alert"), None)
        assert rule is not None
        assert rule["metric"] == "task_model_config_alert"
        assert rule["level"] == "user"
        assert rule["comparator"] == "gt"
        assert rule["threshold"] == 0
        assert rule["severity"] == "critical"      # 配置故障，非普通 warning
        assert int(rule.get("enabled", 1)) == 1
        assert int(rule.get("for_sec", 0)) == 0    # 二次出现即告警，不再沉默

    def test_seed_rule_inserted_idempotently(self):
        with db_lock:
            db.cursor().execute("DELETE FROM obs_alert_rules WHERE name='task_model_config_alert'")
            db.commit()
        assert alerts.seed_rules() >= 1
        assert alerts.seed_rules() == 0
        with db_lock:
            row = db.cursor().execute(
                "SELECT metric, level, severity FROM obs_alert_rules "
                "WHERE name='task_model_config_alert'").fetchone()
        assert row and row[0] == "task_model_config_alert" and row[1] == "user" and row[2] == "critical"