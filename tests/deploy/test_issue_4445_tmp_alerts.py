"""Issue #4445：/tmp 水位告警的数据通路门禁。

远端 worker 上没有 prometheus_client、agent 进程也没有 /metrics —— 节点序列只能由心跳
落库后由控制面采样（grape_node_tmp_*）。若告警继续只写 grape_tmpfs_used_ratio，
远端节点会「永远不触发」，因此这里钉住两条通路各自的规则。
"""
from pathlib import Path

import yaml

ALERTS = Path(__file__).resolve().parents[2] / "deploy" / "alerts.yml"


def _rules() -> dict:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    out = {}
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("alert"):
                out[rule["alert"]] = rule
    return out


def test_worker_alert_uses_heartbeat_derived_series():
    rules = _rules()
    assert "grape_node_tmp_used_ratio" in rules["GrapeWorkerTmpUsageWarning"]["expr"]
    assert "grape_node_tmp_used_ratio" in rules["GrapeWorkerTmpUsageCritical"]["expr"]
    assert rules["GrapeWorkerTmpUsageWarning"]["labels"]["severity"] == "warning"
    assert rules["GrapeWorkerTmpUsageCritical"]["labels"]["severity"] == "critical"


def test_host_alert_keeps_local_gauge():
    rules = _rules()
    assert 'grape_tmpfs_used_ratio{mount="/tmp"}' in rules["GrapeHostTmpUsageWarning"]["expr"]
    assert 'grape_tmpfs_used_ratio{mount="/tmp"}' in rules["GrapeHostTmpUsageCritical"]["expr"]


def test_thresholds_are_80_and_90_percent():
    rules = _rules()
    assert "0.8" in rules["GrapeWorkerTmpUsageWarning"]["expr"]
    assert "0.9" in rules["GrapeWorkerTmpUsageCritical"]["expr"]
    assert "0.8" in rules["GrapeHostTmpUsageWarning"]["expr"]
    assert "0.9" in rules["GrapeHostTmpUsageCritical"]["expr"]