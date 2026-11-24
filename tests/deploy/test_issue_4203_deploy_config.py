"""Issue #4203：部署配置断言——两条 worker 启动路径的内存/重启参数 + alerts.yml。

防止后续改单元/告警/启动脚本时把 Issue #4203 的显式化参数（Memory*/Restart*）或
OOM/内存告警规则无声删掉，或只改其中一条启动路径（QA 复审 Critical：
生产 worker 走 grape@worker.service → scripts/grape-systemd-run.sh，静态单元不一定生效）。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
UNIT = os.path.join(ROOT, "deploy", "systemd", "grape-worker-server.service")
DROPIN = os.path.join(ROOT, "deploy", "systemd", "grape-worker-oom.conf")
RUNNER = os.path.join(ROOT, "scripts", "grape-systemd-run.sh")
INSTALLER = os.path.join(ROOT, "scripts", "install-grape-systemd.sh")
ALERTS = os.path.join(ROOT, "deploy", "alerts.yml")


def _read(path):
    with open(path) as f:
        return f.read()


def _directives(text):
    """取形如 `MemoryMax=8G` 的赋值行（含 drop-in 的 [Service] 段）。"""
    return {m.group(1): m.group(2) for m in re.finditer(
        r"^(MemoryAccounting|MemoryHigh|MemoryMax|Restart|RestartSec|RestartSteps|"
        r"RestartMaxDelaySec)=(\S+)$", text, re.M)}


class TestWorkerUnit:
    def _unit(self):
        return _read(UNIT)

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


class TestGraptAtWorkerPath:
    """生产实际入口：grape@worker.service → grape-systemd-run.sh（QA 复审 Critical）。"""

    def _runner(self):
        return _read(RUNNER)

    def test_worker_role_execs_prebind_launcher(self):
        script = self._runner()
        worker_branch = script.split('if [ "$role" = "worker" ]', 1)
        assert len(worker_branch) == 2, "worker 角色分支缺失：预 bind 未覆盖 grape@ 路径"
        branch_body = worker_branch[1].split("\nfi", 1)[0]
        assert "start-worker.py" in branch_body
        # 其它角色仍走原 uvicorn exec（mgmt/event/insight 行为不变）
        assert '-m uvicorn' in script

    def test_worker_branch_passes_port_and_single_process(self):
        branch_body = self._runner().split('if [ "$role" = "worker" ]', 1)[1].split("\nfi", 1)[0]
        assert "GRAPE_WORKER_PORT" in branch_body
        # 生产既有语义是单进程：显式 1，避免换成多 worker 改变信号/生命周期行为
        assert "GRAPE_UVICORN_WORKERS" in branch_body and ':-1' in branch_body

    def test_dropin_params_match_static_unit(self):
        """两条路径 Memory*/Restart* 字面值必须相等（否则同一单在不同机器行为不同）。"""
        unit_dirs = _directives(_read(UNIT))
        dropin_dirs = _directives(_read(DROPIN))
        for key in ("MemoryAccounting", "MemoryHigh", "MemoryMax",
                    "RestartSec", "RestartSteps", "RestartMaxDelaySec"):
            assert key in dropin_dirs, f"drop-in 缺 {key}"
            assert dropin_dirs[key] == unit_dirs.get(key), (
                f"{key} 漂移：drop-in={dropin_dirs[key]} unit={unit_dirs.get(key)}")

    def test_installer_ships_dropin_for_worker(self):
        installer = _read(INSTALLER)
        assert "grape-worker-oom.conf" in installer
        assert "30-worker-oom.conf" in installer
        # 只装 worker 角色（mgmt/event/insight 不需要内存限额）
        assert 'if [ "$role" = "worker" ]' in installer

    def test_runbook_documents_both_paths(self):
        runbook = _read(os.path.join(ROOT, "docs", "weekly-11", "deployment-runbook.md"))
        assert "grape-systemd-run.sh" in runbook
        assert "grape-worker-oom.conf" in runbook
        assert "MemoryMax" in runbook


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
