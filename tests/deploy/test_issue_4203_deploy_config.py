"""Issue #4203：部署配置断言——systemd 单元内存限额/重启参数 + alerts.yml 三条规则。

防止后续改单元/告警文件时把 Issue #4203 的显式化参数（Memory*/Restart*）或
OOM/内存告警规则无声删掉。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
UNIT = os.path.join(ROOT, "deploy", "systemd", "grape-worker-server.service")
ALERTS = os.path.join(ROOT, "deploy", "alerts.yml")


class TestWorkerUnit:
    def _unit(self):
        with open(UNIT) as f:
            return f.read()

    def test_memory_directives_explicit(self):
        unit = self._unit()
        assert "MemoryAccounting=yes" in unit
        assert "MemoryHigh=" in unit and "MemoryHigh=6G" in unit
        assert "MemoryMax=" in unit and "MemoryMax=8G" in unit

    def test_restart_convergence_params(self):
        unit = self._unit()
        assert "RestartSec=1" in unit
        assert "Restart=always" in unit
        assert "RestartSteps=5" in unit
        assert "RestartMaxDelaySec=30" in unit

    def test_execstart_uses_prebind_launcher(self):
        """ExecStart 走预 bind launcher（先监听再 lifespan），不再直接 uvicorn bind。"""
        unit = self._unit()
        exec_start = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        assert exec_start and "start-worker.py" in exec_start[0]
        assert "--port" not in exec_start[0]

    def test_existing_drain_and_ready_kept(self):
        """#3969 的摘流/readyiness 门禁不因本次改动被删。"""
        unit = self._unit()
        assert "ExecStop=/opt/grape/deploy/systemd/grape-worker-drain.sh" in unit
        assert "grape-worker-ready.sh" in unit
        assert "GRAPE_DRAIN_FILE=" in unit


class TestAlerts:
    def _alerts(self):
        pytest.importorskip("yaml")
        import yaml
        with open(ALERTS) as f:
            return yaml.safe_load(f)

    def test_oom_alert_is_critical_and_uses_node_vmstat(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeWorkerOOMKilled"]
        assert rule["labels"]["severity"] == "critical"
        assert "node_vmstat_oom_kill" in rule["expr"]

    def test_near_limit_alert(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeProcessMemoryNearLimit"]
        assert rule["labels"]["severity"] == "warning"
        assert "grape_process_memory_ratio" in rule["expr"]
        assert rule["for"] == "10m"

    def test_rss_growth_alert(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeWorkerRSSGrowth"]
        assert "grape_process_memory_rss_bytes" in rule["expr"]
        assert "deriv(" in rule["expr"] and "increase(" in rule["expr"]

    def test_alert_metric_names_match_module(self):
        """规则里的指标名与 process_memory.py 定义的 gauge 一致（防漂移）。"""
        from src.domains.insight.observability import process_memory as pm
        from prometheus_client import CollectorRegistry
        reg = CollectorRegistry()
        saved = (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
                 pm._unbounded_gauge, pm._engine_child_gauge)
        try:
            pm._rss_gauge = pm._limit_gauge = pm._ratio_gauge = None
            pm._unbounded_gauge = pm._engine_child_gauge = None
            pm.install(reg)
            names = set()
            for collector in reg._names_to_collectors.values() if hasattr(reg, "_names_to_collectors") else []:
                names.update(getattr(collector, "_names", set()))
            from prometheus_client import generate_latest
            body = generate_latest(reg).decode()
            for metric in ("grape_process_memory_rss_bytes",
                           "grape_process_memory_ratio",
                           "grape_process_memory_limit_bytes",
                           "grape_engine_child_rss_bytes"):
                assert metric in body
        finally:
            (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
             pm._unbounded_gauge, pm._engine_child_gauge) = saved
